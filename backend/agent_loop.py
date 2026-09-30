import os
import re
import json
import subprocess

from config import settings, DEFAULT_NUM_CTX
from code_segmentation import segment_php_code
from schema_context import get_table_schema, get_reglas_negocio
from dependency_graph import get_table_impact
from project_graph_cache import get_project_graph, get_file_context_summary
from danger_flags import analyze_danger_flags_content
from test_matrix import parse_test_matrix, classify_severity
from llm_client import call_llm_chat
from mermaid_sanitizer import sanitize_mermaid_fragment


MAX_TURNS = 8  # tope de turnos de tool-calling antes de forzar un veredicto final

# Herramientas que el agente debe haber llamado al menos una vez antes de que
# se le acepte una respuesta final. Confirmado en pruebas (2026-07-25) que el
# modelo local a veces concluye tras solo llamar listar_funciones (que da un
# mapa, no contenido) — instruirlo por prompt no bastó de forma confiable, así
# que esto se hace cumplir en código, no solo pidiéndoselo.
REQUIRED_TOOLS_BEFORE_FINAL = {"verificar_sintaxis", "leer_funcion"}


def _ensure_within_php_dir(filepath: str) -> str:
    """Resuelve filepath y confirma que está contenido en settings.php_dir
    usando os.path.commonpath — no el `startswith` débil que ya identificamos
    como riesgo en /api/file/content (un directorio hermano con nombre
    similar podría colarse con esa comprobación). Lanza ValueError si el
    archivo queda fuera."""
    real_target = os.path.realpath(filepath)
    real_base = os.path.realpath(settings.php_dir)
    try:
        common = os.path.commonpath([real_target, real_base])
    except ValueError:
        # p. ej. unidades de disco distintas en Windows
        raise ValueError(f"'{filepath}' está fuera del directorio configurado.")
    if common != real_base:
        raise ValueError(f"'{filepath}' está fuera del directorio configurado ({settings.php_dir}).")
    return real_target


def _tool_listar_funciones(filepath: str) -> dict:
    try:
        real_path = _ensure_within_php_dir(filepath)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(real_path):
        return {"error": f"El archivo no existe: {filepath}"}
    try:
        with open(real_path, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception as e:
        return {"error": f"No se pudo leer el archivo: {e}"}

    blocks = segment_php_code(code)
    return {
        "bloques": [
            {
                "nombre": b["name"] or "(código de nivel superior)",
                "tipo": b["kind"],
                "linea_inicio": b["start_line"],
                "linea_fin": b["end_line"]
            }
            for b in blocks
        ]
    }


def _tool_leer_funcion(filepath: str, nombre_funcion: str) -> dict:
    try:
        real_path = _ensure_within_php_dir(filepath)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(real_path):
        return {"error": f"El archivo no existe: {filepath}"}
    try:
        with open(real_path, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception as e:
        return {"error": f"No se pudo leer el archivo: {e}"}

    blocks = segment_php_code(code)
    for b in blocks:
        if b["name"] == nombre_funcion:
            return {"codigo": b["code"], "linea_inicio": b["start_line"], "linea_fin": b["end_line"]}
    return {
        "error": f"No se encontró una función llamada '{nombre_funcion}' en este archivo. "
                 "Usa listar_funciones para ver los nombres disponibles."
    }


def _tool_verificar_sintaxis(filepath: str) -> dict:
    try:
        real_path = _ensure_within_php_dir(filepath)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(real_path):
        return {"error": f"El archivo no existe: {filepath}"}

    ext = os.path.splitext(real_path)[1].lower()
    try:
        if ext == '.php':
            result = subprocess.run(["php", "-l", real_path], capture_output=True, text=True, shell=True)
        elif ext == '.js':
            result = subprocess.run(["node", "--check", real_path], capture_output=True, text=True, shell=True)
        else:
            return {"sintaxis_ok": True, "detalle": f"Sin validador de sintaxis para {ext}."}
        output = result.stdout.strip() or result.stderr.strip()
        return {"sintaxis_ok": result.returncode == 0, "detalle": output or "Sin errores detectados."}
    except Exception as e:
        return {"error": f"Error ejecutando el linter: {e}"}


def _tool_buscar_definicion_funcion(nombre_funcion: str) -> dict:
    pattern = re.compile(r'function\s+' + re.escape(nombre_funcion) + r'\s*\(')
    matches = []
    for root, dirs, files in os.walk(settings.php_dir):
        dirs[:] = [d for d in dirs if d not in ('vendor', '.git', 'node_modules', 'uploads_equipos', 'archivos')]
        for file in files:
            if not file.endswith('.php'):
                continue
            full_path = os.path.join(root, file)
            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for lineno, line in enumerate(f, 1):
                        if pattern.search(line):
                            matches.append({
                                "archivo": os.path.relpath(full_path, settings.php_dir).replace("\\", "/"),
                                "linea": lineno
                            })
            except Exception:
                continue
    if not matches:
        return {"encontrada": False, "mensaje": f"No se encontró ninguna definición de '{nombre_funcion}' en el proyecto."}
    return {"encontrada": True, "ubicaciones": matches}


def _tool_consultar_schema_tabla(tabla: str) -> dict:
    info = get_table_schema(tabla)
    if info is None:
        return {"encontrada": False, "mensaje": f"No hay schema conocido para la tabla '{tabla}'."}
    return {"encontrada": True, "schema": info}


def _tool_obtener_impacto(filepath: str) -> dict:
    try:
        real_path = _ensure_within_php_dir(filepath)
    except ValueError as e:
        return {"error": str(e)}

    # Usa el grafo cacheado (project_graph_cache.py) en vez de recalcularlo
    # con build_dependency_graph() en cada llamada: el grafo de includes/
    # tablas no cambia entre corridas salvo que el código del proyecto se
    # modifique, y esta tool puede llamarse varias veces por análisis.
    graph = get_project_graph(settings.php_dir)
    rel_path = os.path.relpath(real_path, settings.php_dir).replace("\\", "/")
    if rel_path not in graph["adj_includes"]:
        return {"error": "El archivo no está dentro del directorio PHP configurado o no fue indexado."}

    # get_table_impact espera rutas absolutas y un dict {path: set(tablas)};
    # el cache guarda listas ordenadas por ruta relativa, así que se
    # reconstruye la forma esperada aquí en vez de cambiar la firma de una
    # función ya usada en otros lugares (dependency_graph.get_table_impact).
    file_tables_abs = {
        os.path.join(settings.php_dir, p): set(tbls)
        for p, tbls in graph["file_tables"].items()
    }
    my_tables, related = get_table_impact(real_path, file_tables_abs)

    def to_rel(p):
        return os.path.relpath(p, settings.php_dir).replace("\\", "/")

    return {
        "incluye_a": graph["adj_includes"].get(rel_path, []),
        "incluido_por": graph["adj_included_by"].get(rel_path, []),
        "tablas_usadas": sorted(my_tables),
        "archivos_que_comparten_tabla": {tbl: [to_rel(p) for p in files] for tbl, files in related.items()}
    }


TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "listar_funciones",
            "description": "Lista las funciones definidas en un archivo PHP, con sus líneas de inicio y fin. "
                            "Úsalo primero para orientarte antes de pedir código específico.",
            "parameters": {
                "type": "object",
                "required": ["filepath"],
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta completa del archivo a inspeccionar"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "leer_funcion",
            "description": "Devuelve el código fuente completo de una función específica dentro de un archivo PHP.",
            "parameters": {
                "type": "object",
                "required": ["filepath", "nombre_funcion"],
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta completa del archivo"},
                    "nombre_funcion": {"type": "string", "description": "Nombre exacto de la función a leer"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "verificar_sintaxis",
            "description": "Corre un linter real (php -l / node --check) sobre el archivo y confirma si tiene "
                            "errores de sintaxis de verdad.",
            "parameters": {
                "type": "object",
                "required": ["filepath"],
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta completa del archivo a verificar"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_definicion_funcion",
            "description": "Busca en TODO el proyecto dónde está definida una función por nombre. Úsala SIEMPRE "
                            "antes de afirmar que una función 'no está definida' — nunca lo asumas sin verificar.",
            "parameters": {
                "type": "object",
                "required": ["nombre_funcion"],
                "properties": {
                    "nombre_funcion": {"type": "string", "description": "Nombre de la función a buscar"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_schema_tabla",
            "description": "Devuelve la estructura real (columnas, tipos, notas) de una tabla de la base de "
                            "datos, si existe en el schema conocido.",
            "parameters": {
                "type": "object",
                "required": ["tabla"],
                "properties": {
                    "tabla": {"type": "string", "description": "Nombre de la tabla, ej. tblColaborador o modulos.tblColaborador"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "obtener_impacto",
            "description": "Devuelve qué otros archivos incluyen este archivo, a cuáles incluye, y con cuáles "
                            "comparte tablas de base de datos.",
            "parameters": {
                "type": "object",
                "required": ["filepath"],
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta completa del archivo"}
                }
            }
        }
    }
]

TOOL_DISPATCH = {
    "listar_funciones": lambda args: _tool_listar_funciones(args.get("filepath", "")),
    "leer_funcion": lambda args: _tool_leer_funcion(args.get("filepath", ""), args.get("nombre_funcion", "")),
    "verificar_sintaxis": lambda args: _tool_verificar_sintaxis(args.get("filepath", "")),
    "buscar_definicion_funcion": lambda args: _tool_buscar_definicion_funcion(args.get("nombre_funcion", "")),
    "consultar_schema_tabla": lambda args: _tool_consultar_schema_tabla(args.get("tabla", "")),
    "obtener_impacto": lambda args: _tool_obtener_impacto(args.get("filepath", "")),
}


def _dispatch_tool(name: str, args: dict) -> dict:
    handler = TOOL_DISPATCH.get(name)
    if handler is None:
        return {"error": f"Herramienta desconocida: {name}"}
    try:
        return handler(args or {})
    except Exception as e:
        return {"error": f"Error ejecutando '{name}': {e}"}


def _build_system_prompt(filepath: str) -> str:
    # Contexto de proyecto precalculado (grafo cacheado): se inyecta aquí, no
    # como resultado de una tool, para que el agente sepa desde el turno 1
    # qué tan conectado está el archivo (o si parece un script aislado) sin
    # gastar un turno completo solo para descubrirlo.
    try:
        rel_path = os.path.relpath(_ensure_within_php_dir(filepath), settings.php_dir).replace("\\", "/")
        project_context = get_file_context_summary(settings.php_dir, rel_path)
    except Exception:
        project_context = ""

    project_context_block = f"\n\n{project_context}\n" if project_context else ""

    # Reglas de negocio documentadas del sistema (opcional — knowledge/db_schema.json
    # no siempre existe, sobre todo en otros proyectos, así que esta sección
    # se omite por completo si no hay ninguna regla registrada, en vez de
    # forzar la dependencia). Sin esto, el agente solo puede señalar
    # violaciones de lógica de negocio que sean obvias con solo leer el
    # código — nunca las que dependen de una regla no escrita en ningún
    # lado del código mismo.
    reglas = get_reglas_negocio()
    reglas_section = ""
    if reglas:
        # La forma de `reglas` no está fijada — puede ser una lista simple de
        # strings en un proyecto, o un dict anidado con fórmulas y límites
        # en otro. Serializar como JSON en vez de asumir una forma evita
        # perder contenido real al intentar "bulletizarlo".
        reglas_json = json.dumps(reglas, ensure_ascii=False, indent=2)
        reglas_section = (
            "\nReglas de negocio documentadas de este sistema (formato JSON) — verifica si el código las "
            "respeta o las viola; esto es tan importante como los riesgos técnicos, y suele ser más difícil "
            f"de detectar solo leyendo el código sin conocerlas:\n```json\n{reglas_json}\n```\n"
        )

    return (
        f"Eres un Agente de QA para PHP. Tu tarea es analizar el archivo `{filepath}` y producir un "
        "reporte de calidad, usando las herramientas disponibles para VERIFICAR hechos en vez de "
        f"adivinarlos.{project_context_block}\n"
        "En particular:\n"
        "- Antes de afirmar que una función 'no está definida', usa `buscar_definicion_funcion` para confirmarlo.\n"
        "- Usa `listar_funciones` para ver el mapa del archivo antes de pedir el código de una función "
        "específica con `leer_funcion`.\n"
        "- Usa `verificar_sintaxis` para confirmar errores de sintaxis reales, no supuestos.\n"
        "- Usa `consultar_schema_tabla` si el código referencia una tabla y quieres saber sus columnas reales.\n"
        f"- Usa `obtener_impacto` si quieres saber qué otros archivos podrían verse afectados.\n{reglas_section}\n"
        "OBLIGATORIO antes de dar tu veredicto final: debes haber llamado `verificar_sintaxis` al menos "
        "una vez, y `leer_funcion` al menos una vez sobre la función que te parezca más riesgosa (la que "
        "interactúe con base de datos, entrada de usuario, o archivos externos) — ver solo el listado de "
        "`listar_funciones` NO es suficiente para dar un veredicto, esa herramienta solo te da un mapa, no "
        "el contenido. Si te faltan estas llamadas, NO concluyas todavía: sigue investigando.\n\n"
        "IMPORTANTE: nunca menciones el resultado de una herramienta que no hayas llamado realmente en "
        "esta conversación. Si no la has llamado, no inventes lo que 'habría dicho' — llámala primero, o "
        "no la menciones en absoluto.\n\n"
        "Cuando ya tengas suficiente información, responde SIN pedir más herramientas, en ESPAÑOL, con "
        "el siguiente formato Markdown:\n\n"
        "### 📋 Resumen del Componente\n"
        "Qué hace el archivo.\n\n"
        "### ⚠️ Puntos Críticos y Errores Lógicos\n"
        "Riesgos verificados con las herramientas o vistos directamente en el código.\n\n"
        "### 🔍 Categorías de Riesgo Lógico Evaluadas Explícitamente\n"
        "OBLIGATORIO: responde CADA una de estas categorías, aunque tu respuesta sea 'no aplica' o 'no "
        "encontré nada' — el objetivo es dejar constancia de que las consideraste, no solo reportar lo "
        "obvio (inyección SQL, funciones no definidas), que ya sale casi siempre. Para cada una, di si "
        "aplica al archivo y por qué:\n"
        "- **Autorización/pertenencia**: ¿el código verifica que el recurso (equipo, solicitud, registro) "
        "realmente pertenece al usuario/colaborador que lo solicita, o confía en un ID que llega de fuera "
        "sin validar esa relación?\n"
        "- **Concurrencia / doble-envío**: si dos requests llegan casi al mismo tiempo (doble clic, doble "
        "submit, dos usuarios), ¿podría duplicarse una asignación, aprobarse dos veces lo mismo, o "
        "corromperse un conteo/stock?\n"
        "- **Transición de estado**: si el código cambia un estado (aprobado/rechazado/pendiente/"
        "asignado/etc.), ¿valida que la transición sea válida desde el estado actual, o podría saltarse "
        "pasos (ej. aprobar algo ya rechazado)?\n"
        "- **Cálculo numérico/fecha**: si hay una fórmula o cálculo de fechas (antigüedad, vencimiento, "
        "mantenimiento, garantía), ¿la lógica es correcta en casos límite (fechas futuras, cero, "
        "negativos)?\n\n"
        "### 🧪 Matriz de Casos de Prueba (Pasa / Falla)\n"
        "Tabla con al menos 5 filas (mínimo 1 caso feliz, 2 límite, 2 que rompan el código) — si alguna de "
        "las categorías de arriba sí aplica y encontraste un problema real, debe aparecer como fila aquí, "
        "no solo mencionarse arriba:\n"
        "| # | Tipo (Feliz/Límite/Rompe) | Entrada / Escenario | Línea o función afectada | Resultado Esperado | ¿Pasa o Falla hoy? |\n"
        "| --- | --- | --- | --- | --- | --- |\n\n"
        "Después de la tabla, para cada fila 'Falla hoy', añade una sub-sección breve con causa raíz y "
        "sugerencia de corrección. No inventes fallas que no hayas verificado con las herramientas o "
        "visto directamente en el código."
    )


async def _generate_flow_diagram_section(filepath: str, model_name: str, provider: str = "ollama"):
    """Segunda llamada corta y dedicada SOLO a producir un diagrama de flujo
    Mermaid — pedirlo junto con el veredicto principal (que ya tiene 3
    secciones y una matriz de casos) es menos confiable: incluso modelos
    grandes tienden a saltarse instrucciones al final de un prompt largo.
    Un prompt corto de un solo propósito da sintaxis Mermaid válida con
    mucha más consistencia. Es un generador async: puede yield heartbeats
    mientras espera, y al final el bloque de texto del diagrama (o nada, si
    la llamada falla o el archivo no se pudo releer — el diagrama es un
    extra, nunca debe tumbar el veredicto principal que el usuario ya
    recibió)."""
    try:
        real_path = _ensure_within_php_dir(filepath)
        with open(real_path, "r", encoding="utf-8", errors="replace") as f:
            code_str = f.read()
    except Exception:
        return

    ext = (os.path.splitext(filepath)[1] or ".php").lstrip(".")
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
        f"Código ({ext}):\n```{ext}\n{code_str}\n```"
    )

    result = None
    try:
        async for item in call_llm_chat(
            [{"role": "user", "content": diagram_prompt}], model_name, "Diagrama de flujo",
            provider=provider, timeout=180.0, num_ctx=DEFAULT_NUM_CTX
        ):
            if isinstance(item, dict):
                result = item
            else:
                yield item
    except Exception:
        return

    if not (result and result.get("message")):
        return
    content = result["message"].get("content") or ""
    match = re.search(r'```mermaid\s*\n?([\s\S]*?)```', content)
    if match:
        sanitized = sanitize_mermaid_fragment(match.group(1).strip())
        yield "\n\n### 🗺️ Diagrama de Flujo\n```mermaid\n" + sanitized + "\n```\n"


async def run_agent_analysis(filepath: str, model_name: str, provider: str = "ollama", max_turns: int = MAX_TURNS):
    """Loop de agente secuencial (ReAct: pensar -> llamar herramienta -> observar
    -> repetir) para analizar un archivo PHP. A diferencia del pipeline fijo en
    main.py (analyze_php_in_blocks), aquí el MODELO decide qué información
    necesita en vez de que Python se la sirva de antemano. Async generator:
    yields heartbeats/progreso (mismo protocolo `__HB__:` que el resto del
    proyecto), y al final el texto de la respuesta + `__GATE__:{json}` +
    marcador de completado — mismo contrato que /api/qa/desktop-test, así que
    el frontend no necesita cambios para renderizarlo.

    `provider` selecciona el backend ("ollama" local o "nvidia" en la nube,
    vía llm_client.call_llm_chat) sin cambiar nada de la lógica del loop. El
    formato de tool_calls difiere entre ambos (Ollama ya lo da parseado;
    OpenAI/NVIDIA exige function.arguments como string JSON en el historial
    y correlaciona resultados por tool_call_id, no por nombre) — ese detalle
    se absorbe en los dos bloques marcados "NVIDIA" más abajo, el resto del
    loop es idéntico para ambos proveedores."""
    messages = [
        {"role": "system", "content": _build_system_prompt(filepath)},
        {"role": "user", "content": f"Analiza el archivo `{filepath}`. Empieza usando `listar_funciones` para orientarte."}
    ]
    tools_called = set()

    # Análisis determinístico del código completo, independiente de lo que el
    # LLM decida investigar o mencionar: alimenta el gate final aunque el
    # modelo nunca llame una tool que revele un DELETE/UPDATE sin WHERE, o no
    # lo describa con las palabras exactas que classify_severity reconoce.
    try:
        real_path = _ensure_within_php_dir(filepath)
        with open(real_path, "r", encoding="utf-8", errors="replace") as _f:
            _full_code = _f.read()
        _ext = os.path.splitext(real_path)[1].lower()
        code_danger_flags, code_danger_score = analyze_danger_flags_content(_full_code, _ext, filepath=real_path)
    except Exception:
        code_danger_flags, code_danger_score = [], 0

    for turn in range(1, max_turns + 1):
        label = f"Turno {turn}/{max_turns}"
        result = None
        async for item in call_llm_chat(messages, model_name, label, provider=provider, tools=TOOLS_SCHEMA, timeout=300.0, num_ctx=DEFAULT_NUM_CTX):
            if isinstance(item, dict):
                result = item
            else:
                yield item

        if result is None or result.get("error"):
            err = result.get("error") if result else "sin respuesta"
            yield f"\n\n❌ **Error de comunicación con {provider} en el turno {turn}**: {err}\n"
            yield f"\n\n__GATE__:{json.dumps({'gate_status': 'error_analisis', 'fallas_criticas': [], 'fallas_medias': [], 'total_casos': 0}, ensure_ascii=False)}\n"
            yield "\n\n⚠️ **Análisis incompleto.**\n"
            return

        message = result.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            missing = REQUIRED_TOOLS_BEFORE_FINAL - tools_called
            if missing and turn < max_turns:
                # Respuesta prematura: no la aceptamos todavía. En vez de
                # confiar en que el modelo obedezca la instrucción del prompt
                # (ya vimos que no siempre lo hace), lo forzamos en código a
                # seguir investigando antes de darle otra oportunidad de concluir.
                yield f"__HB__:Respuesta prematura — faltan herramientas obligatorias ({', '.join(sorted(missing))}), pidiendo que investigue más...\n"
                messages.append(message)
                messages.append({
                    "role": "user",
                    "content": (
                        f"Todavía no has usado estas herramientas obligatorias: {', '.join(sorted(missing))}. "
                        "No puedes dar tu veredicto final todavía. Úsalas ahora antes de continuar."
                    )
                })
                continue

            # Sin más llamadas a herramientas y ya se cumplió el mínimo (o se
            # acabaron los turnos): esta es la respuesta final.
            final_text = message.get("content", "")
            yield final_text + "\n\n"
            matrix_rows = parse_test_matrix(final_text)
            gate = classify_severity(matrix_rows, danger_score=code_danger_score, danger_flags=code_danger_flags)
            async for item in _generate_flow_diagram_section(filepath, model_name, provider):
                yield item
            yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"
            yield "\n\n✅ **Análisis completo.**\n"
            return

        # El modelo a veces piensa en voz alta además de pedir herramientas —
        # eso se muestra como progreso, no como parte del cuerpo final, para
        # no ensuciar el markdown que se está construyendo.
        thinking = (message.get("content") or "").strip()
        if thinking:
            yield f"__HB__:{thinking[:100]}\n"

        if provider == "nvidia" and tool_calls:
            # NVIDIA: el formato OpenAI exige que function.arguments viaje
            # como string JSON en el historial de mensajes — nvidia_client
            # ya lo parseó a dict para que _dispatch_tool lo use cómodo, así
            # que hay que re-serializarlo antes de reenviar este turno como
            # historial (confirmado con un 400 real: "invalid type: map,
            # expected a string" cuando se manda el dict tal cual).
            message_for_history = dict(message)
            message_for_history["tool_calls"] = [
                {**tc, "function": {**tc["function"], "arguments": json.dumps(tc["function"].get("arguments", {}), ensure_ascii=False)}}
                for tc in tool_calls
            ]
            messages.append(message_for_history)
        else:
            messages.append(message)

        for call in tool_calls:
            fn = call.get("function", {})
            fn_name = fn.get("name")
            fn_args = fn.get("arguments") or {}
            tools_called.add(fn_name)
            yield f"__HB__:Ejecutando herramienta `{fn_name}`...\n"
            tool_result = _dispatch_tool(fn_name, fn_args)
            tool_content = json.dumps(tool_result, ensure_ascii=False)
            if provider == "nvidia":
                # NVIDIA/OpenAI: se correlaciona por tool_call_id, no por
                # nombre — necesario si el modelo pide varias herramientas
                # en el mismo turno.
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": tool_content})
            else:
                messages.append({"role": "tool", "tool_name": fn_name, "content": tool_content})

    # Se agotaron los turnos sin una respuesta final: forzar un último
    # llamado sin herramientas para no dejar la corrida colgada.
    yield "__HB__:Se agotaron los turnos de herramientas, pidiendo veredicto final...\n"
    messages.append({
        "role": "user",
        "content": "Ya no puedes usar más herramientas. Da tu veredicto final ahora mismo con el formato "
                    "pedido, usando únicamente la información que ya obtuviste."
    })
    result = None
    async for item in call_llm_chat(messages, model_name, "Veredicto final", provider=provider, timeout=300.0, num_ctx=DEFAULT_NUM_CTX):
        if isinstance(item, dict):
            result = item
        else:
            yield item

    final_text = ""
    if result and result.get("message"):
        final_text = result["message"].get("content", "")
        yield final_text + "\n\n"
    elif result and result.get("error"):
        yield f"\n\n❌ No se pudo obtener veredicto final: {result['error']}\n"

    matrix_rows = parse_test_matrix(final_text)
    gate = classify_severity(matrix_rows, danger_score=code_danger_score, danger_flags=code_danger_flags)
    async for item in _generate_flow_diagram_section(filepath, model_name, provider):
        yield item
    yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"
    yield "\n\n✅ **Análisis completo.**\n"
