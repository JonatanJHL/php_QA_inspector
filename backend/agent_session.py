"""
Sesión de agente multi-tenant (Sep 2026): variante de agent_loop.py que NO
lee filesystem directamente. Aquí el loop ReAct se pausa cada vez que el
LLM pide una tool y espera a que EL CLIENTE (client/local_agent_client.py,
corriendo en la máquina del usuario) resuelva esa tool y regrese el
resultado — el servidor nunca abre un archivo ajeno.

Diferencia clave con run_agent_analysis (agent_loop.py):
  - agent_loop.py: while-loop síncrono, dispatch de tools ES LOCAL AL SERVIDOR.
  - agent_session.py: máquina de estados explícita. Cada "paso" puede terminar
    en tres cosas: (a) necesita una tool -> se serializa el estado y se
    devuelve un tool_request al cliente; (b) terminó -> se devuelve el texto
    final + gate; (c) error.

La sesión completa (mensajes acumulados, tools ya llamadas, flags de
peligro) vive en un dict serializable, NO en variables de proceso — así
puede persistir entre requests HTTP sin depender de que el mismo worker
atienda ambas llamadas.
"""
import json

from config import DEFAULT_NUM_CTX
from code_segmentation import segment_php_code
from danger_flags import analyze_danger_flags_content
from schema_context import get_reglas_negocio
from test_matrix import parse_test_matrix, classify_severity
from ollama_client import call_ollama_chat


MAX_TURNS = 8
# Fallback de compatibilidad: si una sesión no trae "required_tools" en su
# estado (por ejemplo una sesión vieja persistida antes de este cambio), se
# usa este conjunto fijo. Las sesiones nuevas siempre traen su propio
# required_tools calculado en new_session_state según el archivo real.
REQUIRED_TOOLS_BEFORE_FINAL_DEFAULT = {"verificar_sintaxis", "leer_funcion"}

# Mismo esquema que agent_loop.TOOLS_SCHEMA, MENOS consultar_schema_tabla:
# esa tool no depende del filesystem del cliente (es schema compartido de
# servidor), así que se queda del lado servidor — ver dispatch en main.py.
TOOLS_SCHEMA_CLIENT = [
    {
        "type": "function",
        "function": {
            "name": "listar_funciones",
            "description": "Lista las funciones definidas en un archivo, con sus líneas de inicio y fin. "
                            "Úsalo primero para orientarte antes de pedir código específico.",
            "parameters": {
                "type": "object", "required": ["filepath"],
                "properties": {"filepath": {"type": "string", "description": "Ruta relativa del archivo"}}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "leer_funcion",
            "description": "Devuelve el código fuente completo de una función específica dentro de un archivo.",
            "parameters": {
                "type": "object", "required": ["filepath", "nombre_funcion"],
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta relativa del archivo"},
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
                "type": "object", "required": ["filepath"],
                "properties": {"filepath": {"type": "string", "description": "Ruta relativa del archivo"}}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_definicion_funcion",
            "description": "Busca en TODO el proyecto dónde está definida una función por nombre. Úsala SIEMPRE "
                            "antes de afirmar que una función 'no está definida'.",
            "parameters": {
                "type": "object", "required": ["nombre_funcion"],
                "properties": {"nombre_funcion": {"type": "string"}}
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
                "type": "object", "required": ["filepath"],
                "properties": {"filepath": {"type": "string", "description": "Ruta relativa del archivo"}}
            }
        }
    }
]


def _build_system_prompt(rel_filepath: str, project_context: str, requires_leer_funcion: bool) -> str:
    project_context_block = f"\n\n{project_context}\n" if project_context else ""
    leer_funcion_req = (
        ", y `leer_funcion` al menos una vez sobre la función que te parezca más riesgosa"
        if requires_leer_funcion else
        " (este archivo no tiene funciones nombradas — todo es código de nivel superior, así que "
        "no necesitas llamar `leer_funcion`; ya tienes el código completo en el mensaje anterior)"
    )

    # Reglas de negocio documentadas del sistema (opcional — ver
    # schema_context.get_reglas_negocio). Igual que en agent_loop.py: sin
    # esto, el agente solo puede señalar violaciones de lógica de negocio
    # obvias leyendo el código, nunca las que dependen de una regla no
    # escrita en ningún lado del código mismo.
    reglas = get_reglas_negocio()
    reglas_section = ""
    if reglas:
        reglas_json = json.dumps(reglas, ensure_ascii=False, indent=2)
        reglas_section = (
            "\nReglas de negocio documentadas de este sistema (formato JSON) — verifica si el código las "
            "respeta o las viola; esto es tan importante como los riesgos técnicos, y suele ser más difícil "
            f"de detectar solo leyendo el código sin conocerlas:\n```json\n{reglas_json}\n```\n"
        )

    return (
        f"Eres un Agente de QA para PHP/JS. Tu tarea es analizar el archivo `{rel_filepath}` y producir "
        "un reporte de calidad, usando las herramientas disponibles para VERIFICAR hechos en vez de "
        f"adivinarlos.{project_context_block}\n"
        "En particular:\n"
        "- Antes de afirmar que una función 'no está definida', usa `buscar_definicion_funcion` para confirmarlo.\n"
        "- Usa `listar_funciones` para ver el mapa del archivo antes de pedir el código de una función "
        "específica con `leer_funcion`.\n"
        "- Usa `verificar_sintaxis` para confirmar errores de sintaxis reales, no supuestos.\n"
        f"- Usa `obtener_impacto` si quieres saber qué otros archivos podrían verse afectados.\n{reglas_section}\n"
        f"OBLIGATORIO antes de dar tu veredicto final: debes haber llamado `verificar_sintaxis` al menos "
        f"una vez{leer_funcion_req}. Si te faltan estas llamadas, NO concluyas todavía: sigue investigando.\n\n"
        "IMPORTANTE: en cuanto hayas cumplido lo obligatorio de arriba, DEBES dar tu veredicto final de "
        "inmediato — no sigas pidiendo herramientas adicionales 'por si acaso' ni repitas una tool que ya "
        "llamaste con los mismos argumentos. Más investigación innecesaria no mejora tu reporte, solo lo "
        "retrasa. `buscar_definicion_funcion` y `obtener_impacto` son opcionales: solo úsalas si de verdad "
        "tienes una duda concreta sin resolver, nunca como paso rutinario.\n\n"
        "IMPORTANTE: nunca menciones el resultado de una herramienta que no hayas llamado realmente en "
        "esta conversación.\n\n"
        "Cuando ya tengas suficiente información, responde SIN pedir más herramientas, en ESPAÑOL, con "
        "el siguiente formato Markdown:\n\n"
        "### 📋 Resumen del Componente\nQué hace el archivo.\n\n"
        "### ⚠️ Puntos Críticos y Errores Lógicos\nRiesgos verificados.\n\n"
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
        "Después de la tabla, para cada fila 'Falla hoy', añade causa raíz y sugerencia de corrección."
    )


def new_session_state(rel_filepath: str, content: str, ext: str, project_context: str = "") -> dict:
    """Construye el estado inicial de una sesión de análisis. Este dict es
    lo único que se persiste entre requests HTTP (ver main.py) — no hay
    estado en variables de proceso, así que una sesión sobrevive un reinicio
    del worker o un balanceo entre procesos si se guarda en algo persistente
    (hoy: en memoria del proceso vía un dict global en main.py; si se
    necesita escalar a varios workers, este mismo dict se guardaría en algo
    compartido como Redis sin cambiar nada de esta función)."""
    danger_flags, danger_score = analyze_danger_flags_content(content, ext, filepath=rel_filepath)

    # Si el archivo no tiene ninguna función nombrada (todo top_level, como
    # un script de un solo bloque), exigir `leer_funcion` es un requisito
    # imposible de cumplir — el modelo agotaría los 8 turnos pidiéndola sin
    # éxito (confirmado empíricamente con logout.php). El código completo ya
    # va en el mensaje de usuario inicial, así que el requisito se relaja
    # solo para ese caso.
    has_named_functions = False
    if ext == '.php':
        try:
            blocks = segment_php_code(content)
            has_named_functions = any(b["kind"] == "function" for b in blocks)
        except Exception:
            has_named_functions = True  # ante la duda, mantener el requisito estricto

    required_tools = {"verificar_sintaxis"}
    if has_named_functions:
        required_tools.add("leer_funcion")

    return {
        "rel_filepath": rel_filepath,
        "messages": [
            {"role": "system", "content": _build_system_prompt(rel_filepath, project_context, has_named_functions)},
            {"role": "user", "content": (
                f"Analiza el archivo `{rel_filepath}`. Empieza usando `listar_funciones`.\n\n"
                f"Código completo del archivo:\n```{ext.lstrip('.')}\n{content}\n```"
            )}
        ],
        "tools_called": [],
        "turn": 0,
        "danger_flags": danger_flags,
        "danger_score": danger_score,
        "required_tools": list(required_tools),
        "status": "running",  # running | needs_tool | done | error
    }


async def advance_session(state: dict, model_name: str, tool_result: dict = None, tool_name: str = None):
    """Avanza la sesión un paso. Async generator: yields texto/heartbeats de
    progreso (mismo protocolo __HB__ que el resto del proyecto), y al final
    yields exactamente UN dict con el resultado del paso:

      {"status": "needs_tool", "tool": "leer_funcion", "args": {...}, "state": {...}}
        -> el cliente debe ejecutar esa tool localmente y volver a llamar
           advance_session pasando tool_result + tool_name + este mismo state.

      {"status": "done", "final_text": "...", "gate": {...}}
        -> análisis terminado, nada más que hacer.

      {"status": "error", "error": "..."}
    """
    messages = state["messages"]
    tools_called = set(state["tools_called"])

    # Si venimos de resolver una tool pedida en el paso anterior, insertamos
    # su resultado en el historial antes de seguir pensando.
    if tool_result is not None and tool_name is not None:
        tools_called.add(tool_name)
        messages.append({
            "role": "tool", "tool_name": tool_name,
            "content": json.dumps(tool_result, ensure_ascii=False)
        })
        state["tools_called"] = list(tools_called)

    # Si el turno anterior pidió varias tools a la vez, quedan pendientes en
    # _pending_tool_calls: se resuelven una por una (mismo viaje cliente-
    # servidor) ANTES de volver a preguntarle algo nuevo al modelo — evita
    # que una segunda tool call del mismo turno se pierda silenciosamente.
    pending = state.get("_pending_tool_calls") or []
    if pending:
        next_call = pending[0]
        state["_pending_tool_calls"] = pending[1:]
        fn = next_call.get("function", {})
        fn_name = fn.get("name")
        fn_args = fn.get("arguments") or {}
        state["status"] = "needs_tool"
        yield f"__HB__:Solicitando herramienta `{fn_name}` (se ejecuta en tu máquina)...\n"
        yield {"status": "needs_tool", "tool": fn_name, "args": fn_args, "state": state}
        return

    state["turn"] += 1
    turn = state["turn"]

    if turn > MAX_TURNS:
        messages.append({
            "role": "user",
            "content": "Ya no puedes usar más herramientas. Da tu veredicto final ahora mismo con el "
                       "formato pedido, usando únicamente la información que ya obtuviste."
        })

    # Bucle en vez de recursión: cuando el modelo intenta concluir sin haber
    # cumplido los requisitos, se le empuja a seguir y se vuelve a llamar a
    # Ollama EN EL MISMO advance_session, incrementando turn cada vez — antes
    # esto se hacía con una llamada recursiva a advance_session(), lo que
    # complicaba razonar sobre cuántos turnos reales se habían consumido.
    # Ambas versiones cuentan el turno igual; esta es más fácil de seguir.
    while True:
        label = f"Turno {turn}/{MAX_TURNS}"
        use_tools = turn <= MAX_TURNS
        result = None
        async for item in call_ollama_chat(
            messages, model_name, label,
            tools=TOOLS_SCHEMA_CLIENT if use_tools else None,
            timeout=300.0, num_ctx=DEFAULT_NUM_CTX
        ):
            if isinstance(item, dict):
                result = item
            else:
                yield item

        if result is None or result.get("error"):
            err = result.get("error") if result else "sin respuesta"
            yield {"status": "error", "error": f"Error de comunicación con Ollama: {err}"}
            return

        message = result.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            required_tools = set(state.get("required_tools", REQUIRED_TOOLS_BEFORE_FINAL_DEFAULT))
            missing = required_tools - tools_called
            if missing and turn < MAX_TURNS:
                yield f"__HB__:Respuesta prematura — faltan herramientas obligatorias ({', '.join(sorted(missing))}), pidiendo que investigue más...\n"
                messages.append(message)
                messages.append({
                    "role": "user",
                    "content": (
                        f"Todavía no has usado estas herramientas obligatorias: {', '.join(sorted(missing))}. "
                        "No puedes dar tu veredicto final todavía. Úsalas ahora antes de continuar."
                    )
                })
                state["tools_called"] = list(tools_called)
                state["turn"] += 1
                turn = state["turn"]
                continue  # vuelve a llamar a Ollama con el empujón ya en messages

            final_text = message.get("content", "")
            matrix_rows = parse_test_matrix(final_text)
            gate = classify_severity(matrix_rows, danger_score=state["danger_score"], danger_flags=state["danger_flags"])
            state["status"] = "done"
            yield {"status": "done", "final_text": final_text, "gate": gate, "state": state}
            return

        break  # el modelo pidió tool_calls reales: sale del bucle a resolverlas abajo

    thinking = (message.get("content") or "").strip()
    if thinking:
        yield f"__HB__:{thinking[:100]}\n"

    messages.append(message)
    state["tools_called"] = list(tools_called)

    # Solo se resuelve la PRIMERA tool call de este turno hacia el cliente;
    # si el modelo pidió varias en el mismo turno, las restantes se procesan
    # en las siguientes vueltas de continue_session (una tool por viaje
    # cliente-servidor, para mantener el protocolo simple).
    first_call = tool_calls[0]
    fn = first_call.get("function", {})
    fn_name = fn.get("name")
    fn_args = fn.get("arguments") or {}

    if len(tool_calls) > 1:
        # Guardamos las tools adicionales pendientes para procesarlas antes
        # de volver a llamar al modelo (ver continue_session en main.py).
        state["_pending_tool_calls"] = tool_calls[1:]
    else:
        state["_pending_tool_calls"] = []

    state["status"] = "needs_tool"
    yield f"__HB__:Solicitando herramienta `{fn_name}` (se ejecuta en tu máquina)...\n"
    yield {"status": "needs_tool", "tool": fn_name, "args": fn_args, "state": state}
