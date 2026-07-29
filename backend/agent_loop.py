import os
import re
import json
import subprocess

from config import settings
from code_segmentation import segment_php_code
from schema_context import get_table_schema
from dependency_graph import build_dependency_graph, get_table_impact
from test_matrix import parse_test_matrix, classify_severity
from llm_client import call_llm_chat


MAX_TURNS = 8  # tope de turnos de tool-calling antes de forzar un veredicto final

# Grupos de herramientas que el agente debe haber llamado al menos una vez
# (una cualquiera POR GRUPO) antes de que se le acepte una respuesta final.
# Confirmado en pruebas (2026-07-25) que el modelo local a veces concluye
# tras solo llamar listar_funciones (que da un mapa, no contenido) —
# instruirlo por prompt no bastó de forma confiable, así que esto se hace
# cumplir en código. El grupo de lectura de código es "leer_funcion O
# leer_bloque" (no ambas obligatorias): confirmado en pruebas (2026-07-28)
# que un archivo sin funciones con nombre (puro código de "nivel superior",
# muy común en este proyecto) deja a leer_funcion sin nada que encontrar
# nunca — exigirla siempre fuerza al modelo a fabricar contenido cuando llega
# al último turno sin haber podido leer nada real.
REQUIRED_TOOL_GROUPS = [
    {"verificar_sintaxis"},
    {"leer_funcion", "leer_bloque"},
]


def _missing_required_tool_groups(tools_called: set) -> list:
    """Devuelve los grupos de REQUIRED_TOOL_GROUPS de los que NINGUNA
    herramienta fue llamada todavía."""
    return [group for group in REQUIRED_TOOL_GROUPS if not (group & tools_called)]


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


def _tool_leer_bloque(filepath: str, linea_inicio: int, linea_fin: int) -> dict:
    """Lee un rango de líneas directo del archivo, sin necesitar nombre de
    función — para código de 'nivel superior' (sin función con nombre), que
    listar_funciones ya reporta con su rango de líneas. Confirmado en
    pruebas (2026-07-28) que sin esta herramienta, un archivo sin funciones
    con nombre deja al agente sin ninguna forma de leer contenido real."""
    try:
        real_path = _ensure_within_php_dir(filepath)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(real_path):
        return {"error": f"El archivo no existe: {filepath}"}
    try:
        with open(real_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return {"error": f"No se pudo leer el archivo: {e}"}

    try:
        start = max(1, int(linea_inicio))
        end = min(len(lines), int(linea_fin))
    except (TypeError, ValueError):
        return {"error": f"Rango de líneas inválido: {linea_inicio}-{linea_fin}"}
    if start > end:
        return {"error": f"Rango de líneas inválido: {linea_inicio}-{linea_fin} (inicio > fin, o fuera del archivo)"}

    snippet = "".join(lines[start - 1:end])
    return {"codigo": snippet, "linea_inicio": start, "linea_fin": end}


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
            result = subprocess.run(["php", "-l", real_path], capture_output=True, text=True)
        elif ext == '.js':
            result = subprocess.run(["node", "--check", real_path], capture_output=True, text=True)
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

    adj_includes, adj_included_by, file_tables, _ = build_dependency_graph(settings.php_dir)
    if real_path not in adj_includes:
        return {"error": "El archivo no está dentro del directorio PHP configurado o no fue indexado."}

    def to_rel(p):
        return os.path.relpath(p, settings.php_dir).replace("\\", "/")

    my_tables, related = get_table_impact(real_path, file_tables)
    return {
        "incluye_a": [to_rel(p) for p in adj_includes.get(real_path, [])],
        "incluido_por": [to_rel(p) for p in adj_included_by.get(real_path, [])],
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
            "name": "leer_bloque",
            "description": "Devuelve el código fuente de un rango de líneas exacto de un archivo. Úsala para "
                            "leer código de 'nivel superior' (sin función con nombre) — listar_funciones ya te da "
                            "el rango de líneas de esos bloques. También sirve como alternativa a leer_funcion "
                            "para cualquier rango que necesites ver.",
            "parameters": {
                "type": "object",
                "required": ["filepath", "linea_inicio", "linea_fin"],
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta completa del archivo"},
                    "linea_inicio": {"type": "integer", "description": "Número de línea donde empieza el bloque (1-indexado)"},
                    "linea_fin": {"type": "integer", "description": "Número de línea donde termina el bloque"}
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
    "leer_bloque": lambda args: _tool_leer_bloque(args.get("filepath", ""), args.get("linea_inicio", 0), args.get("linea_fin", 0)),
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


async def _generate_flow_diagram_section(filepath: str, model_name: str, provider: str):
    """Segunda llamada corta y dedicada SOLO a producir un diagrama de flujo
    Mermaid — pedirlo junto con el veredicto principal (que ya tiene 3
    secciones y una matriz de casos) es menos confiable: incluso modelos
    grandes tienden a saltarse instrucciones al final de un prompt largo.
    Un prompt corto de un solo propósito da sintaxis Mermaid válida con
    mucha más consistencia (mismo enfoque que usaba el pipeline fijo
    anterior en desktop_test.py). Es un generador async: puede yield
    heartbeats mientras espera, y al final el bloque de texto del diagrama
    (o nada, si la llamada falla o el archivo no se pudo releer — el
    diagrama es un extra, nunca debe tumbar el veredicto principal que el
    usuario ya recibió)."""
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
            provider=provider, timeout=180.0
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
        yield "\n\n### 🗺️ Diagrama de Flujo\n```mermaid\n" + match.group(1).strip() + "\n```\n"


async def _finalize_analysis(final_text: str, filepath: str, model_name: str, provider: str):
    """Cola común a ambas salidas de run_agent_analysis (respuesta final
    normal, o veredicto forzado tras agotar los turnos): yield el texto
    final, agrega el diagrama de flujo, y cierra con el gate + marcador de
    completado. El gate se calcula sobre `final_text` SIN el diagrama para
    no confundir al parser de la matriz de casos con contenido Mermaid."""
    yield final_text + "\n\n"
    matrix_rows = parse_test_matrix(final_text)
    gate = classify_severity(matrix_rows)
    async for item in _generate_flow_diagram_section(filepath, model_name, provider):
        yield item
    yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"
    yield "\n\n✅ **Análisis completo.**\n"


def _build_system_prompt(filepath: str) -> str:
    return (
        f"Eres un Agente de QA para PHP. Tu tarea es analizar el archivo `{filepath}` y producir un "
        "reporte de calidad, usando las herramientas disponibles para VERIFICAR hechos en vez de "
        "adivinarlos. En particular:\n"
        "- Antes de afirmar que una función 'no está definida', usa `buscar_definicion_funcion` para confirmarlo.\n"
        "- Usa `listar_funciones` para ver el mapa del archivo antes de pedir el código de una función "
        "específica con `leer_funcion`.\n"
        "- Usa `verificar_sintaxis` para confirmar errores de sintaxis reales, no supuestos.\n"
        "- Usa `consultar_schema_tabla` si el código referencia una tabla y quieres saber sus columnas reales.\n"
        "- Usa `obtener_impacto` si quieres saber qué otros archivos podrían verse afectados.\n"
        "- Usa `leer_bloque` para leer un rango de líneas exacto — es la ÚNICA forma de ver código de "
        "'nivel superior' (código que no está dentro de ninguna función con nombre), ya que `leer_funcion` "
        "requiere un nombre de función y no puede recuperar ese código. `listar_funciones` te da el rango "
        "de líneas de esos bloques de nivel superior para que se lo pases a `leer_bloque`.\n\n"
        "OBLIGATORIO antes de dar tu veredicto final: debes haber llamado `verificar_sintaxis` al menos "
        "una vez, y `leer_funcion` o `leer_bloque` al menos una vez sobre el código que te parezca más "
        "riesgoso (el que interactúe con base de datos, entrada de usuario, o archivos externos) — si el "
        "archivo no tiene funciones con nombre, usa `leer_bloque` en su lugar. Ver solo el listado de "
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
        "### 🧪 Matriz de Casos de Prueba (Pasa / Falla)\n"
        "Tabla con al menos 5 filas (mínimo 1 caso feliz, 2 límite, 2 que rompan el código):\n"
        "| # | Tipo (Feliz/Límite/Rompe) | Entrada / Escenario | Línea o función afectada | Resultado Esperado | ¿Pasa o Falla hoy? |\n"
        "| --- | --- | --- | --- | --- | --- |\n\n"
        "Después de la tabla, para cada fila 'Falla hoy', añade una sub-sección breve con causa raíz y "
        "sugerencia de corrección. No inventes fallas que no hayas verificado con las herramientas o "
        "visto directamente en el código."
    )


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
    vía llm_client.call_llm_chat) sin cambiar nada de la lógica del loop en
    sí — mismas herramientas, mismos REQUIRED_TOOL_GROUPS."""
    messages = [
        {"role": "system", "content": _build_system_prompt(filepath)},
        {"role": "user", "content": f"Analiza el archivo `{filepath}`. Empieza usando `listar_funciones` para orientarte."}
    ]
    tools_called = set()

    for turn in range(1, max_turns + 1):
        label = f"Turno {turn}/{max_turns}"
        result = None
        async for item in call_llm_chat(messages, model_name, label, provider=provider,
                                          tools=TOOLS_SCHEMA, timeout=300.0, num_ctx=8192):
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
            missing_groups = _missing_required_tool_groups(tools_called)
            is_blank = not (message.get("content") or "").strip()
            if (missing_groups or is_blank) and turn < max_turns:
                # Respuesta prematura: no la aceptamos todavía. En vez de
                # confiar en que el modelo obedezca la instrucción del prompt
                # (ya vimos que no siempre lo hace), lo forzamos en código a
                # seguir investigando antes de darle otra oportunidad de concluir.
                # Una respuesta en blanco (sin texto Y sin tool_calls — visto
                # en la práctica cuando el modelo intenta pedir una herramienta
                # escribiendo un tag de texto tipo <TOOLCALL> en vez de usar el
                # mecanismo estructurado de la API) tampoco se acepta nunca,
                # incluso si ya se cumplieron los grupos obligatorios.
                if missing_groups:
                    missing_desc = ", ".join("/".join(sorted(group)) for group in missing_groups)
                    yield f"__HB__:Respuesta prematura — faltan herramientas obligatorias ({missing_desc}), pidiendo que investigue más...\n"
                    correction = (
                        f"Todavía no has usado (ninguna herramienta de) estos grupos obligatorios: {missing_desc}. "
                        "No puedes dar tu veredicto final todavía. Úsalas ahora antes de continuar."
                    )
                else:
                    yield "__HB__:Respuesta en blanco (sin texto ni herramienta), pidiendo que continúe...\n"
                    correction = (
                        "Tu respuesta llegó vacía, sin texto y sin pedir ninguna herramienta. "
                        "Si querías usar una herramienta, hazlo con el mecanismo de function-calling, no como texto. "
                        "Continúa el análisis ahora."
                    )
                messages.append(message)
                messages.append({"role": "user", "content": correction})
                continue

            # Sin más llamadas a herramientas y ya se cumplió el mínimo (o se
            # acabaron los turnos): esta es la respuesta final.
            final_text = message.get("content") or ""
            async for item in _finalize_analysis(final_text, filepath, model_name, provider):
                yield item
            return

        # El modelo a veces piensa en voz alta además de pedir herramientas —
        # eso se muestra como progreso, no como parte del cuerpo final, para
        # no ensuciar el markdown que se está construyendo.
        thinking = (message.get("content") or "").strip()
        if thinking:
            yield f"__HB__:{thinking[:100]}\n"

        if provider == "nvidia" and tool_calls:
            # El formato OpenAI exige que function.arguments viaje como
            # string JSON en el historial de mensajes — nvidia_client ya lo
            # parseo a dict para que _dispatch_tool lo use comodo, asi que
            # hay que re-serializarlo antes de reenviar este turno como
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
                # Formato OpenAI: se correlaciona por tool_call_id, no por
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
    async for item in call_llm_chat(messages, model_name, "Veredicto final", provider=provider,
                                      timeout=300.0, num_ctx=8192):
        if isinstance(item, dict):
            result = item
        else:
            yield item

    final_text = ""
    if result and result.get("message"):
        final_text = result["message"].get("content") or ""
    elif result and result.get("error"):
        yield f"\n\n❌ No se pudo obtener veredicto final: {result['error']}\n"

    async for item in _finalize_analysis(final_text, filepath, model_name, provider):
        yield item
