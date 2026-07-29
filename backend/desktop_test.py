import os
import re
import json
import httpx

from config import settings
from schema_context import build_schema_context
from qa_history import load_desktop_test_history, save_desktop_test_result
from ollama_client import call_ollama_chat
from code_segmentation import segment_php_code, LOOP_ENGINEERING_THRESHOLD_CHARS
from test_matrix import parse_test_matrix, classify_severity

# Recursive-retry splitting for blocks that fail even after call_ollama_chat's
# own internal retry: below this size, splitting further stops paying off
# (too little content per call to be worth another round-trip), so a failure
# here is accepted as final. Above it, a failed block gets halved by line
# count and each half is retried independently -- smaller prompts are both
# less likely to hit the 300s timeout and, if one half still fails, at least
# the other half's real findings aren't lost with it.
BLOCK_RETRY_MIN_SPLIT_CHARS = 800
BLOCK_RETRY_MAX_SPLIT_DEPTH = 2  # tope: como mucho 4 sub-bloques hoja por bloque original


def extract_block_summary(text: str, max_chars: int = 500) -> str:
    """Compact a single block's raw analyze_block response for running_summary.
    analyze_block's prompt asks for 4 numbered items (qué hace / variables /
    riesgo que rompe la ejecución / diagrama mermaid); a blind head-truncation
    at a few hundred chars only ever kept item 1 (and part of 2), silently
    dropping item 3 — the risk/breaking-case content the consolidation call
    and the final test matrix most depend on. This extracts item 3 explicitly
    so it survives truncation even when the block's full response is long."""
    risk_match = re.search(r'(?:^|\n)\s*3[\.\)]\s*(.*?)(?=\n\s*4[\.\)]|\Z)', text, re.DOTALL)
    if not risk_match:
        return text[:max_chars]
    risk_text = risk_match.group(1).strip()
    head = text[:150].strip()
    return f"{head} [...] Riesgo: {risk_text}"[:max_chars]


async def run_desktop_test_stream(filepath: str, model_name: str):
    """Analiza un archivo PHP/JS/TPL/SQL con Ollama, transmitiendo el
    resultado como texto plano (mismo protocolo __HB__/__GATE__ que el resto
    del proyecto). Generador async: se puede envolver directamente en
    StreamingResponse desde main.py."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception as e:
        yield f"Error: No se pudo leer el archivo: {str(e)}"
        return

    ext = os.path.splitext(filepath)[1].lower()
    
    if ext == '.js':
        role_description = "Eres un Agente Ingeniero de QA y Especialista en Pruebas de Escritorio para JavaScript y Node.js."
        instructions = (
            "Tu tarea es analizar el código JavaScript provisto y simular paso a paso su ejecución (prueba de escritorio) de forma lógica.\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Componente\n"
            "Explica brevemente qué hace este script, qué parámetros recibe (argumentos, variables de entorno, peticiones, etc.) y qué librerías o dependencias tiene.\n\n"
            "### 🔍 Simulación paso a paso (Prueba de Escritorio)\n"
            "Simula un flujo de ejecución típico. Describe cronológicamente qué pasa línea por línea o función por función, explicando el comportamiento lógico, callbacks, promesas o flujo asíncrono.\n\n"
            "### 📊 Tabla de Variables e Hilos de Estado\n"
            "Muestra una tabla de Markdown que rastree los valores y tipos de las variables clave en los puntos críticos de la ejecución.\n\n"
            "### ⚠️ Puntos Críticos y Errores Lógicos\n"
            "Identifica posibles fallos: variables no declaradas (global scope leaks), llamadas asíncronas no manejadas (unhandled promise rejections), callbacks faltantes, comparación insegura (`==` vs `===`), o variables undefined/null.\n\n"
            "### 🧪 Casos de Prueba Recomendados\n"
            "Sugiere de 2 a 3 escenarios de prueba concretos detallando entradas y comportamiento esperado."
        )
    elif ext == '.tpl':
        role_description = "Eres un Agente Ingeniero de QA y Especialista en Plantillas y Vistas (Templates) para Smarty/HTML."
        instructions = (
            "Tu tarea es analizar la plantilla de Smarty/Template provista y verificar su lógica y mapeo de variables.\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Template\n"
            "Explica brevemente qué sección de la interfaz representa esta plantilla y qué variables dinámicas de PHP espera recibir para renderizar.\n\n"
            "### 🔍 Simulación paso a paso (Flujo de Renderizado)\n"
            "Simula el renderizado de la plantilla. Explica cómo se procesan las estructuras condicionales (ej. `{if}`, `{else}`) y los bucles (ej. `{foreach}`, `{section}`).\n\n"
            "### ⚠️ Variables Requeridas del Controlador\n"
            "Lista todas las variables del template que provienen de la lógica de negocio (PHP) para que el desarrollador pueda validar que se están pasando correctamente.\n\n"
            "### ⚠️ Errores Lógicos y Estructurales\n"
            "Identifica posibles bugs: tags sin cerrar, llamadas a variables inexistentes, lógica condicional contradictoria o problemas con el escapado de datos (riesgos de XSS en layouts).\n\n"
            "### 🧪 Casos de Prueba Recomendados\n"
            "Sugiere escenarios de prueba (ej. arrays vacíos para los foreach, booleanos falsos en condicionales) para probar cómo reacciona la vista."
        )
    elif ext == '.sql':
        role_description = "Eres un Agente de Base de Datos y Especialista en QA SQL."
        instructions = (
            "Tu tarea es analizar el script de base de datos SQL provisto (DDL, DML, triggers, procedimientos, etc.).\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Script SQL\n"
            "Explica qué tablas se crean/modifican, qué relaciones tienen o qué datos se insertan.\n\n"
            "### 🔍 Análisis de Relaciones y Claves\n"
            "Detalla la estructura de claves primarias, claves foráneas, índices, restricciones y tipos de datos.\n\n"
            "### ⚠️ Puntos Críticos y Riesgos del Script\n"
            "Identifica riesgos: claves foráneas sin cascada o restricciones, tipos de datos inadecuados, falta de índices en columnas de búsqueda frecuentes, riesgo de pérdida de datos por ALTER TABLE destructivos o falta de transacciones.\n\n"
            "### 🧪 Pruebas de Inserción / Casos Límite\n"
            "Sugiere escenarios de inserción (ej. valores nulos, valores duplicados en campos únicos, desbordamientos de longitud) y qué restricciones deberían activarse."
        )
    else:
        # Default PHP template with SQL Mock logic
        role_description = "Eres un Agente Ingeniero de QA y Especialista en Pruebas de Escritorio para PHP."
        instructions = (
            "Tu tarea es analizar el código PHP provisto y simular paso a paso su ejecución (prueba de escritorio) de forma lógica, sin ejecutarlo realmente en un servidor.\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Componente\n"
            "Explica brevemente qué hace este script, qué parámetros recibe (GET, POST, $_SESSION, etc.) y qué dependencias tiene (includes, base de datos).\n\n"
            "### 🗄️ Simulación de Base de Datos y Datos Mock\n"
            "Si el código contiene consultas SQL (SELECT, INSERT, UPDATE, etc.):\n"
            "1. Identifica y extrae las consultas SQL detectadas.\n"
            "2. Genera una lista de valores simulados (Mock Data) de 1 o 2 filas, estructuradas con las columnas del SELECT.\n"
            "3. Emula cómo el script PHP recuperará estos registros (por ejemplo, en bucles `while` o mapeos de arrays) y explica qué variables se poblarán y cómo.\n\n"
            "### 🔍 Simulación paso a paso (Prueba de Escritorio)\n"
            "Simula un flujo de ejecución típico utilizando los datos de entrada o los registros mock generados arriba. Describe cronológicamente qué pasa línea por línea o sección por sección, explicando cómo cambia el flujo lógico.\n\n"
            "### 📊 Tabla de Variables e Hilos de Estado\n"
            "Muestra una tabla de Markdown que rastree los valores teóricos de las variables más importantes en los puntos clave de la simulación.\n"
            "Ejemplo de formato:\n"
            "| Variable | Momento / Línea | Tipo | Valor Estimado / Estado | Descripción |\n"
            "| --- | --- | --- | --- | --- |\n\n"
            "### ⚠️ Puntos Críticos y Errores Lógicos\n"
            "Identifica posibles bugs que no detecta el compilador de sintaxis simple. Ejemplos:\n"
            "- Variables que podrían estar indefinidas bajo ciertas condiciones.\n"
            "- Falta de validación en parámetros de entrada (riesgos de SQL Injection o XSS en PHP puro).\n"
            "- Errores de lógica en bucles o condiciones.\n"
            "- Dependencias de variables globales (`$dao`, `$conn`, `$_SESSION`) no verificadas antes de su uso.\n\n"
            "### 🧪 Matriz de Casos de Prueba (Pasa / Falla)\n"
            "Esta es la sección MAS IMPORTANTE. No basta con describir escenarios en prosa: construye una TABLA "
            "de Markdown con AL MENOS 5 filas que cubra estos tres tipos de caso, mezclados:\n"
            "1. **Caso feliz** (mínimo 1): entrada válida típica, todo funciona como se espera.\n"
            "2. **Caso límite/borde** (mínimo 2): entrada vacía, cero, id inexistente, array vacío, string muy largo, "
            "caracteres especiales o UTF-8, valores nulos donde se esperaba un tipo concreto.\n"
            "3. **Caso que ROMPE el código intencionalmente** (mínimo 2, obligatorio): identifica una entrada "
            "específica que, dado el código real que tienes enfrente, HARÍA FALLAR el script (excepción no "
            "capturada, división por cero, acceso a índice de array inexistente, llamada a método sobre null, "
            "query SQL malformada por un valor no escapado, condición de carrera si dos requests llegan casi "
            "simultáneas, etc). No inventes fallas genéricas: señala la línea o función real del código donde "
            "ese caso específico rompería, y por qué.\n\n"
            "Formato obligatorio de la tabla:\n"
            "| # | Tipo (Feliz/Límite/Rompe) | Entrada / Escenario | Línea o función afectada | Resultado Esperado | ¿Pasa o Falla hoy? |\n"
            "| --- | --- | --- | --- | --- | --- |\n\n"
            "Después de la tabla, para cada fila marcada como 'Falla hoy', añade una sub-sección breve "
            "explicando la causa raíz y una sugerencia concreta de corrección (código o validación faltante).\n"
        )
        
    # El diagrama de flujo Mermaid ya NO se pide en este prompt principal —
    # se genera con una segunda llamada corta y dedicada (ver generate_mermaid_diagram
    # más abajo), porque Hermes3 tiende a ignorar instrucciones al final de
    # prompts largos con muchas secciones obligatorias.
    schema_context = build_schema_context(code)
    prompt_system = (
        f"{role_description}\n{instructions}"
        + (f"\n\n{schema_context}" if schema_context else "")
    )

    # --- Inyectar historial como memoria simulada ---
    # Hermes3/Ollama no recuerda nada entre llamadas por si mismo; esto le da
    # continuidad artificial mostrandole (resumido) que dijo la ultima vez
    # sobre este mismo archivo, y si el codigo cambio desde entonces.
    import hashlib
    current_code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
    history_entries = load_desktop_test_history(filepath, max_entries=2)

    history_context = ""
    if history_entries:
        last = history_entries[0]
        changed_note = (
            "El código NO ha cambiado desde ese análisis anterior."
            if last.get("code_hash") == current_code_hash
            else "El código SI cambió desde ese análisis anterior — verifica si los hallazgos previos ya se corrigieron."
        )
        history_context = (
            "\n\n---\n"
            "## 🧠 Contexto de análisis previos sobre este mismo archivo\n"
            f"({changed_note})\n\n"
        )
        for i, entry in enumerate(history_entries, 1):
            history_context += (
                f"**Análisis anterior #{i}** ({entry.get('timestamp', '?')}, modelo: {entry.get('model', '?')}):\n"
                f"{entry.get('summary', '')}\n\n"
            )
        history_context += (
            "Ten en cuenta lo anterior: si los mismos problemas siguen presentes, señálalo explícitamente "
            "('Este problema ya se detectó antes y sigue sin corregirse'). Si el código cambió y un problema "
            "previo ya no aplica, dilo también ('Este caso ya no falla, fue corregido').\n---\n"
        )

    prompt_user = (
        f"Realiza la prueba de escritorio para el siguiente archivo:\n"
        f"Ruta: `{filepath}`\n\n"
        f"Código:\n```{ext[1:]}\n{code}\n```"
        f"{history_context}"
    )

    url = f"{settings.ollama_url}/api/chat"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user}
        ],
        "stream": True,
        # keep_alive="10m": evita que Ollama descarde el modelo de RAM entre
        # esta llamada y las siguientes (confirmado con `ollama ps` que el
        # default expira en ~1 minuto de inactividad). Sin esto, cada bloque
        # del loop engineering podría pagar el costo de recargar 6GB en RAM
        # antes de siquiera empezar a generar texto.
        "keep_alive": "10m",
        "options": {
            "temperature": 0.2,
            # El default de Ollama para hermes3 es num_ctx=4096, confirmado con
            # `ollama ps`. Archivos PHP reales de tamaño mediano-grande (500-900
            # líneas) generan un prompt_user de ~7000-8000 tokens, que por sí
            # solo ya rebasa ese límite — Ollama entonces trunca/descarta
            # contenido (empezando por el system prompt, que va primero), y
            # el modelo termina ignorando la estructura pedida por completo.
            # Subir num_ctx aquí es lo que corrige esto de raíz. 8192 es un
            # punto de partida conservador para CPU-only; si archivos aún
            # más grandes siguen fallando, subir a 16384 (más lento, más RAM).
            # NOTA: se probó bajar a 3276 buscando velocidad, pero esto
            # empeoró las cosas — con menos contexto Ollama necesita más
            # trabajo por token en CPU, y varios bloques terminaron en
            # ReadTimeout (180s) en vez de responder más rápido. 8192 es el
            # valor correcto confirmado empíricamente para este hardware.
            "num_ctx": 8192
        }
    }

    # --- DEBUG TEMPORAL: confirmar en consola exactamente qué se envía a Ollama ---
    print("=" * 60)
    print(f"[DEBUG desktop-test] modelo={model_name} ext={ext} len(system)={len(prompt_system)} len(user)={len(prompt_user)}")
    print(f"[DEBUG desktop-test] system_prompt (primeros 500 chars):\n{prompt_system[:500]}")
    print("=" * 60)

    async def call_ollama_with_heartbeat(prompt: str, model_name: str, label: str,
                                           timeout: float = 300.0, num_ctx: int = 8192,
                                           heartbeat_interval: float = 4.0):
        """Thin wrapper around ollama_client.call_ollama_chat preserving the
        {"content": str, "error": str|None} shape this file's callers expect."""
        result = None
        async for item in call_ollama_chat(
            [{"role": "user", "content": prompt}], model_name, label,
            timeout=timeout, num_ctx=num_ctx, heartbeat_interval=heartbeat_interval
        ):
            if isinstance(item, dict):
                message = item.get("message")
                result = {"content": message.get("content", "") if message else "", "error": item.get("error")}
            else:
                yield item
        yield result

    async def generate_mermaid_diagram(code_str: str, extension: str, model_name: str) -> str:
        """Second, focused Ollama call dedicated ONLY to producing a Mermaid
        flowchart. Kept deliberately short: Hermes3 (a small local model)
        tends to drop instructions from the tail of a long, multi-section
        prompt — asking for 7 things at once meant the diagram request was
        the one most likely to get skipped. A short, single-purpose prompt
        is far more reliable for actually getting valid Mermaid syntax back.
        Non-streaming (simpler to parse a single response), with a short
        timeout since this is a small, bounded generation task.
        """
        diagram_prompt = (
            "Genera ÚNICAMENTE un diagrama de flujo en formato Mermaid (`flowchart TD`) que represente "
            "el flujo principal del siguiente código. No agregues explicación, resumen, ni texto fuera "
            "del bloque de código. Solo el bloque ```mermaid ... ```.\n\n"
            "Reglas del diagrama:\n"
            "- IDs de nodo cortos (A, B, C...), texto breve dentro de cada nodo (máximo 6 palabras).\n"
            "- Incluye puntos de entrada, decisiones condicionales (nodos de rombo), accesos a base de "
            "datos/servicios externos, y puntos de salida.\n"
            "- Si detectas un punto que claramente rompería la ejecución (excepción no capturada, "
            "división por cero, acceso a índice inexistente, etc.), márcalo con la clase `errorNode`: "
            "`classDef errorNode fill:#7f1d1d,stroke:#ef4444,color:#fff;`\n\n"
            f"Código ({extension[1:]}):\n```{extension[1:]}\n{code_str}\n```"
        )
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(
                    f"{settings.ollama_url}/api/chat",
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": diagram_prompt}],
                        "stream": False,
                        "keep_alive": "10m",
                        "options": {"temperature": 0.1, "num_ctx": 8192}
                    }
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                return data.get("message", {}).get("content", "")
        except Exception:
            # The diagram is a nice-to-have on top of the main analysis; if
            # this second call fails for any reason, the main response the
            # user already saw is unaffected — just no diagram this time.
            return ""

    async def analyze_block(block: dict, accumulated_summary: str, model_name: str):
        """Analyze a single code block (one function, or a merged chunk of
        top-level code) with a short, focused prompt. Async GENERATOR: while
        Ollama is thinking (which can take minutes on CPU-only hardware),
        yields a heartbeat line every few seconds so the frontend has visible
        proof the process hasn't stalled — a single blocking await here would
        leave the stream silent for the entire duration of each block. The
        final yielded item is a dict {"text":..., "mermaid":...}; every
        earlier yielded item is a plain string heartbeat line."""
        block_label = f"función `{block['name']}`" if block['kind'] == 'function' else "código de nivel superior"
        prior_context = (
            f"Resumen de lo detectado en bloques anteriores del mismo archivo:\n{accumulated_summary}\n\n"
            "Nota: si algún bloque de arriba aparece como [NO ANALIZADO], fue por un error técnico "
            "(timeout) al procesarlo — NO es evidencia de que ese código tenga un problema, falte, o esté "
            "mal definido. No reportes una función como 'no definida' ni afirmes ningún riesgo basado "
            "únicamente en esa marca; repórtalo solo si lo ves directamente en el código de ESTE bloque.\n\n"
            if accumulated_summary.strip() else
            "Este es el primer bloque analizado de este archivo, no hay contexto previo.\n\n"
        )
        schema_ctx = build_schema_context(block["code"])
        schema_section = f"\n{schema_ctx}\n\n" if schema_ctx else ""
        block_prompt = (
            f"Estás analizando el archivo PHP `{filepath}` en partes, porque es demasiado grande para "
            f"analizarlo de una sola vez. Este es el bloque correspondiente a {block_label} "
            f"(líneas {block['start_line']}-{block['end_line']}).\n\n"
            f"{prior_context}"
            f"{schema_section}"
            "Para ESTE bloque únicamente, responde en español y de forma breve con:\n"
            "1. Qué hace este bloque (1-2 líneas).\n"
            "2. Variables clave que recibe, produce, o modifica.\n"
            "3. Cualquier riesgo o caso que ROMPERÍA la ejecución (sé específico: qué entrada, qué línea).\n"
            "4. Un fragmento de diagrama Mermaid (solo los nodos de ESTE bloque, con IDs que no se repitan "
            "entre bloques — usa el prefijo indicado abajo). SIEMPRE encierra el diagrama entre tres "
            "backticks y la palabra mermaid, así: ```mermaid ... ``` — nunca lo escribas sin ese cercado, "
            "aunque uses sintaxis 'graph LR' o 'graph TD' en vez de 'flowchart'.\n\n"
            f"Prefijo de IDs para este bloque: usa IDs como {block['name'] or 'TOP'}_A, {block['name'] or 'TOP'}_B, etc.\n\n"
            f"Código de este bloque:\n```php\n{block['code']}\n```"
        )

        # call_ollama_chat already retries once internally on failure (confirmed
        # necessary: this hardware's Ollama backend can crash mid-request and
        # auto-restarts within seconds), so no separate outer retry is needed here.
        result = None
        async for item in call_ollama_chat(
            [{"role": "user", "content": block_prompt}], model_name, "Analizando bloque",
            timeout=300.0, num_ctx=8192
        ):
            if isinstance(item, dict):
                message = item.get("message")
                error = item.get("error")
                if error or not message:
                    result = {
                        "text": f"(No se pudo analizar el bloque '{block['name']}': {error or 'sin respuesta'})",
                        "mermaid": "", "failed": True
                    }
                else:
                    content = message.get("content", "")
                    # Accept both ```mermaid fenced blocks AND bare "graph TD/LR"
                    # or "flowchart TD/LR" lines the model sometimes emits
                    # unfenced despite instructions, so a missed fence doesn't
                    # silently lose the diagram.
                    mermaid_match = re.search(r'```mermaid\s*\n?([\s\S]*?)```', content)
                    if mermaid_match:
                        mermaid_fragment = mermaid_match.group(1).strip()
                    else:
                        bare_match = re.search(r'((?:graph|flowchart)\s+(?:TD|LR|TB|RL)[\s\S]*?)(?:\n\n|\Z)', content)
                        mermaid_fragment = bare_match.group(1).strip() if bare_match else ""
                    result = {"text": content, "mermaid": mermaid_fragment, "failed": False}
            else:
                yield item

        yield result

    async def analyze_block_with_recursive_retry(block: dict, accumulated_summary: str,
                                                     model_name: str, depth: int = 0):
        """Wraps analyze_block with recursive splitting on failure: if a block
        still fails after call_ollama_chat's own internal retry, and the block
        is large enough to be worth splitting, halve it by line count and
        retry each half independently (recursing up to BLOCK_RETRY_MAX_SPLIT_DEPTH
        times). Smaller prompts are less likely to hit the 300s timeout, and if
        only one half fails, the other half's real findings still survive
        instead of the whole block being lost. Same generator contract as
        analyze_block: yields heartbeat/progress strings, then exactly one
        final dict {"text", "mermaid", "failed"}."""
        result = None
        async for item in analyze_block(block, accumulated_summary, model_name):
            if isinstance(item, dict):
                result = item
            else:
                yield item

        if not result.get("failed"):
            yield result
            return
        if depth >= BLOCK_RETRY_MAX_SPLIT_DEPTH or len(block["code"]) <= BLOCK_RETRY_MIN_SPLIT_CHARS:
            yield result
            return

        lines = block["code"].split("\n")
        mid = len(lines) // 2
        if mid == 0:
            # No se puede partir mas (una sola linea gigante) — aceptar el fallo.
            yield result
            return

        total_lines = block["end_line"] - block["start_line"] + 1
        mid_line = block["start_line"] + (total_lines // 2)
        half_label = f"función `{block['name']}`" if block['kind'] == 'function' else "código de nivel superior"

        half1 = {"kind": block["kind"], "name": block["name"], "code": "\n".join(lines[:mid]),
                 "start_line": block["start_line"], "end_line": mid_line}
        half2 = {"kind": block["kind"], "name": block["name"], "code": "\n".join(lines[mid:]),
                 "start_line": mid_line + 1, "end_line": block["end_line"]}

        yield (f"__HB__:Bloque de {half_label} demasiado grande para reintentar entero, "
               f"dividiendo en 2 mitades ({len(half1['code'])}+{len(half2['code'])} chars)...\n")

        result1 = None
        async for item in analyze_block_with_recursive_retry(half1, accumulated_summary, model_name, depth + 1):
            if isinstance(item, dict):
                result1 = item
            else:
                yield item

        result2 = None
        async for item in analyze_block_with_recursive_retry(half2, accumulated_summary, model_name, depth + 1):
            if isinstance(item, dict):
                result2 = item
            else:
                yield item

        # Mismo criterio que el marcador [NO ANALIZADO] del resto del pipeline:
        # una mitad fallida se anota como "sin hallazgos reales", nunca como
        # texto de error crudo que el modelo podria malinterpretar como una
        # señal de riesgo real (bug ya confirmado hoy con [NO ANALIZADO]).
        text1 = result1["text"] if not result1.get("failed") else "[NO ANALIZADO — mitad 1, error técnico, sin hallazgos reales]"
        text2 = result2["text"] if not result2.get("failed") else "[NO ANALIZADO — mitad 2, error técnico, sin hallazgos reales]"
        combined_mermaid = "\n".join(m for m in [result1.get("mermaid", ""), result2.get("mermaid", "")] if m)

        yield {
            "text": f"[Mitad 1/2 de este bloque, líneas {half1['start_line']}-{half1['end_line']}]\n{text1}\n\n"
                    f"[Mitad 2/2 de este bloque, líneas {half2['start_line']}-{half2['end_line']}]\n{text2}",
            "mermaid": combined_mermaid,
            # Solo se considera realmente perdido si AMBAS mitades fallaron —
            # si una sobrevivio, sus hallazgos reales siguen siendo utiles.
            "failed": result1.get("failed", False) and result2.get("failed", False)
        }

    async def analyze_php_in_blocks(model_name: str):
        """Loop-engineering path for large PHP files: segment into logical
        blocks, analyze each with a short focused call (carrying forward a
        compact summary as context), then make one final consolidating call
        with all partial findings — never the raw code again — to produce
        the Resumen/Matriz/Veredicto in our standard format, plus a merged
        Mermaid diagram stitched from each block's fragment."""
        blocks = segment_php_code(code, max_block_chars=6000)
        yield f"_Archivo grande detectado ({len(code)} caracteres). Analizando en {len(blocks)} bloque(s) por separado para no saturar el contexto del modelo local..._\n\n---\n\n"

        running_summary = ""
        mermaid_fragments = []
        failed_labels = []

        for i, block in enumerate(blocks, 1):
            label = block['name'] if block['kind'] == 'function' else f"nivel superior (líneas {block['start_line']}-{block['end_line']})"
            yield f"**[Bloque {i}/{len(blocks)}] Analizando: `{label}`...**\n\n"

            # analyze_block_with_recursive_retry is itself an async generator:
            # it yields heartbeat strings while Ollama is still thinking, and
            # yields exactly one dict (the real result) as its last item —
            # recursively splitting the block into smaller retries on failure
            # before giving up. Forward every heartbeat straight to the
            # frontend for real-time "still working" feedback; capture the
            # dict when it arrives.
            result = None
            async for item in analyze_block_with_recursive_retry(block, running_summary, model_name):
                if isinstance(item, dict):
                    result = item
                else:
                    yield item
            if result is None:
                result = {"text": f"(El bloque '{block['name']}' no produjo resultado.)", "mermaid": "", "failed": True}

            yield result["text"] + "\n\n---\n\n"

            if result.get("failed"):
                failed_labels.append(label)
                # Distinct marker (not the raw error text) so the consolidation
                # model can't mistake "ReadTimeout" prose for an actual code
                # finding and build a plausible-sounding story around it —
                # this is what caused fabricated function names in testing
                # when a block timed out and running_summary held only the
                # error message.
                running_summary += f"- Bloque '{label}': [NO ANALIZADO — error técnico, sin hallazgos reales]\n"
            else:
                # Feed forward a compact summary, not the full response, so the
                # context for later blocks doesn't itself grow unbounded.
                running_summary += f"- Bloque '{label}': {extract_block_summary(result['text'])}\n"
            if result["mermaid"]:
                mermaid_fragments.append(result["mermaid"])

        if failed_labels and len(failed_labels) == len(blocks):
            # Every block failed: there is zero real information to consolidate.
            # Do NOT call the consolidation model in this state — tested
            # empirically that it will confidently fabricate a full summary,
            # test matrix and gate verdict (including function names that
            # don't exist in the file) from nothing but an error string.
            yield (
                f"\n\n❌ **No se pudo completar el análisis**: los {len(blocks)} bloque(s) del archivo "
                "fallaron por timeout o error de conexión con Ollama. No se generará un resumen ni una "
                "matriz de pruebas, ya que no hay ningún hallazgo real del código para basarlos.\n\n"
                "Reintenta cuando el servidor de Ollama esté menos ocupado, o con un modelo/hardware más rápido.\n"
            )
            gate = {"gate_status": "error_analisis", "fallas_criticas": [], "fallas_medias": [], "total_casos": 0}
            yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"
            yield "\n\n⚠️ **Análisis incompleto.**\n"
            return

        # --- Final consolidation call: no raw code, only the accumulated
        # findings, so this call is always small regardless of file size. ---
        yield "**Consolidando hallazgos de todos los bloques...**\n\n"
        failed_note = ""
        if failed_labels:
            failed_note = (
                f"\n\nADVERTENCIA: {len(failed_labels)} de {len(blocks)} bloque(s) no se pudieron analizar "
                f"(marcados como [NO ANALIZADO] abajo): {', '.join(failed_labels)}. NO inventes hallazgos "
                "para esos bloques ni los uses como base de ninguna fila de la matriz — menciona explícitamente "
                "en el Resumen que esa parte del archivo quedó sin analizar por un error técnico.\n"
            )
        consolidation_prompt = (
            f"Analizaste un archivo PHP grande en {len(blocks)} bloque(s) separado(s) (una función a la vez). "
            "A continuación tienes el resumen de lo que se encontró en cada bloque — y SOLO esos bloques "
            f"existen en este archivo, no hay más. Menciona ÚNICAMENTE los {len(blocks)} bloque(s) que "
            "aparecen en 'Hallazgos por bloque' más abajo; no inventes bloques, funciones ni líneas "
            "adicionales que no fueron listados ahí.\n"
            "Con base ÚNICAMENTE en esa información (no tienes el código completo a la vista), redacta "
            "en español y en Markdown:"
            f"{failed_note}\n\n"
            "### 📋 Resumen del Componente\n"
            "Qué hace el archivo en general, integrando lo que hace cada bloque.\n\n"
            "### ⚠️ Puntos Críticos y Errores Lógicos\n"
            "Consolida los riesgos detectados en los bloques individuales.\n\n"
            "### 🧪 Matriz de Casos de Prueba (Pasa / Falla)\n"
            "Una tabla con al menos 5 filas (mínimo 1 caso feliz, 2 límite, 2 que rompan el código), "
            "usando los hallazgos de los bloques como base. No inventes fallas genéricas que no estén "
            "respaldadas por los hallazgos recibidos: cada fila 'Rompe' debe señalar el bloque o función "
            "real (según los hallazgos de abajo) donde ese caso ocurriría, y por qué.\n"
            "| # | Tipo (Feliz/Límite/Rompe) | Entrada / Escenario | Bloque afectado | Resultado Esperado | ¿Pasa o Falla hoy? |\n"
            "| --- | --- | --- | --- | --- | --- |\n\n"
            "Después de la tabla, para cada fila marcada como 'Falla hoy', añade una sub-sección breve "
            "explicando la causa raíz (según el hallazgo del bloque correspondiente) y una sugerencia "
            "concreta de corrección.\n\n"
            f"Hallazgos por bloque:\n{running_summary}"
        )
        consolidation_result = None
        async for item in call_ollama_with_heartbeat(
            consolidation_prompt, model_name, "Consolidando hallazgos", timeout=300.0
        ):
            if isinstance(item, dict):
                consolidation_result = item
            else:
                yield item

        consolidation_text = ""
        if consolidation_result and consolidation_result.get("content"):
            consolidation_text = consolidation_result["content"]
            yield consolidation_text + "\n\n"
        elif consolidation_result and consolidation_result.get("error"):
            yield f"(No se pudo generar el resumen consolidado: {consolidation_result['error']})\n\n"

        # --- Stitch all per-block Mermaid fragments into one flowchart ---
        if mermaid_fragments:
            merged_mermaid = "```mermaid\nflowchart TD\n" + "\n".join(mermaid_fragments) + "\n```"
            yield "\n\n### 🗺️ Diagrama de Flujo (Consolidado)\n" + merged_mermaid + "\n"

        # --- Cobertura del análisis: siempre visible, no solo cuando algo
        # falló. Un reporte que se ve "completo" pero en realidad se saltó
        # bloques sin decirlo es exactamente el tipo de cosa que erosiona la
        # confianza en la herramienta — así que esto se muestra siempre,
        # incluso cuando la respuesta es "0 bloques fallaron". ---
        exitosos = len(blocks) - len(failed_labels)
        cobertura = f"\n\n### 📊 Cobertura del Análisis\n{exitosos}/{len(blocks)} bloques analizados exitosamente."
        if failed_labels:
            cobertura += f" **{len(failed_labels)} bloque(s) no se pudieron analizar** (error técnico, no evidencia de que el código tenga un problema): {', '.join(failed_labels)}."
        yield cobertura + "\n"

        # --- Risk gate: the test matrix lives in the consolidation response
        # (individual blocks only produce partial findings), so parse it
        # from there specifically. ---
        matrix_rows = parse_test_matrix(consolidation_text)
        gate = classify_severity(matrix_rows)
        yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"

        # Explicit end-of-stream marker: the frontend uses this exact string
        # to flip the running/success indicator, since with heartbeat lines
        # now present it's otherwise ambiguous whether the stream is still
        # active or truly finished right after the last content chunk.
        yield "\n\n✅ **Análisis completo.**\n"

    async def event_generator():
        accumulated = []  # capture the full response so we can save it to history after streaming

        # --- Loop engineering path: large PHP files get segmented and
        # analyzed block-by-block instead of one oversized call. ---
        if ext == '.php' and len(code) > LOOP_ENGINEERING_THRESHOLD_CHARS:
            try:
                async for chunk in analyze_php_in_blocks(model_name):
                    accumulated.append(chunk)
                    yield chunk
                full_text = "".join(accumulated)
                if full_text.strip():
                    save_desktop_test_result(filepath, model_name, code, full_text)
            except Exception as e:
                yield f"Error inesperado durante el análisis por bloques: {str(e)}"
            return

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        err_body = await response.aread()
                        yield f"Error: Ollama devolvió código {response.status_code}. Detalles: {err_body.decode('utf-8')}"
                        return
                    
                    async for line in response.aiter_lines():
                        if line.strip():
                            try:
                                data = json.loads(line)
                                chunk = data.get("message", {}).get("content", "")
                                if chunk:
                                    accumulated.append(chunk)
                                    yield chunk
                            except Exception:
                                pass

            # Main analysis finished. Now make the dedicated, short call for
            # the Mermaid diagram and stream it as an additional chunk. The
            # frontend just looks for a ```mermaid block anywhere in the full
            # accumulated text, so appending it here works with zero frontend changes.
            diagram_response = await generate_mermaid_diagram(code, ext, model_name)
            if diagram_response.strip():
                diagram_chunk = "\n\n---\n" + diagram_response
                accumulated.append(diagram_chunk)
                yield diagram_chunk

            # --- Risk gate: parse the test matrix from the accumulated text
            # and classify severity, so the frontend can show a structured
            # blocked/warning/approved verdict instead of relying on the
            # person to manually read the whole table and decide. ---
            full_text_so_far = "".join(accumulated)
            matrix_rows = parse_test_matrix(full_text_so_far)
            gate = classify_severity(matrix_rows)
            yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"

            # Explicit end-of-stream marker, matching the loop-engineering
            # path, so the frontend has one reliable string to detect
            # completion regardless of which path produced the response.
            yield "\n\n✅ **Análisis completo.**\n"

            # Stream finished without raising: persist this run so the next
            # call on this same file can be told "here's what was found before".
            full_text = "".join(accumulated)
            if full_text.strip():
                save_desktop_test_result(filepath, model_name, code, full_text)
        except httpx.ConnectError:
            yield f"Error: No se pudo conectar a Ollama en {settings.ollama_url}. ¿Está encendido?"
        except httpx.TimeoutException:
            yield "Error: Tiempo de espera agotado al comunicarse con Ollama."
        except Exception as e:
            yield f"Error inesperado: {str(e)}"

    async for chunk in event_generator():
        yield chunk



