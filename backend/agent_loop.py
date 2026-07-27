import os
import re
import json
import subprocess

from config import settings
from code_segmentation import segment_php_code
from schema_context import get_table_schema
from dependency_graph import build_dependency_graph, get_table_impact
from test_matrix import parse_test_matrix, classify_severity
from ollama_client import call_ollama_chat


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
    return (
        f"Eres un Agente de QA para PHP. Tu tarea es analizar el archivo `{filepath}` y producir un "
        "reporte de calidad, usando las herramientas disponibles para VERIFICAR hechos en vez de "
        "adivinarlos. En particular:\n"
        "- Antes de afirmar que una función 'no está definida', usa `buscar_definicion_funcion` para confirmarlo.\n"
        "- Usa `listar_funciones` para ver el mapa del archivo antes de pedir el código de una función "
        "específica con `leer_funcion`.\n"
        "- Usa `verificar_sintaxis` para confirmar errores de sintaxis reales, no supuestos.\n"
        "- Usa `consultar_schema_tabla` si el código referencia una tabla y quieres saber sus columnas reales.\n"
        "- Usa `obtener_impacto` si quieres saber qué otros archivos podrían verse afectados.\n\n"
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
        "### 🧪 Matriz de Casos de Prueba (Pasa / Falla)\n"
        "Tabla con al menos 5 filas (mínimo 1 caso feliz, 2 límite, 2 que rompan el código):\n"
        "| # | Tipo (Feliz/Límite/Rompe) | Entrada / Escenario | Línea o función afectada | Resultado Esperado | ¿Pasa o Falla hoy? |\n"
        "| --- | --- | --- | --- | --- | --- |\n\n"
        "Después de la tabla, para cada fila 'Falla hoy', añade una sub-sección breve con causa raíz y "
        "sugerencia de corrección. No inventes fallas que no hayas verificado con las herramientas o "
        "visto directamente en el código."
    )


async def run_agent_analysis(filepath: str, model_name: str, max_turns: int = MAX_TURNS):
    """Loop de agente secuencial (ReAct: pensar -> llamar herramienta -> observar
    -> repetir) para analizar un archivo PHP. A diferencia del pipeline fijo en
    main.py (analyze_php_in_blocks), aquí el MODELO decide qué información
    necesita en vez de que Python se la sirva de antemano. Async generator:
    yields heartbeats/progreso (mismo protocolo `__HB__:` que el resto del
    proyecto), y al final el texto de la respuesta + `__GATE__:{json}` +
    marcador de completado — mismo contrato que /api/qa/desktop-test, así que
    el frontend no necesita cambios para renderizarlo."""
    messages = [
        {"role": "system", "content": _build_system_prompt(filepath)},
        {"role": "user", "content": f"Analiza el archivo `{filepath}`. Empieza usando `listar_funciones` para orientarte."}
    ]
    tools_called = set()

    for turn in range(1, max_turns + 1):
        label = f"Turno {turn}/{max_turns}"
        result = None
        async for item in call_ollama_chat(messages, model_name, label, tools=TOOLS_SCHEMA, timeout=300.0, num_ctx=8192):
            if isinstance(item, dict):
                result = item
            else:
                yield item

        if result is None or result.get("error"):
            err = result.get("error") if result else "sin respuesta"
            yield f"\n\n❌ **Error de comunicación con Ollama en el turno {turn}**: {err}\n"
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
            gate = classify_severity(matrix_rows)
            yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"
            yield "\n\n✅ **Análisis completo.**\n"
            return

        # El modelo a veces piensa en voz alta además de pedir herramientas —
        # eso se muestra como progreso, no como parte del cuerpo final, para
        # no ensuciar el markdown que se está construyendo.
        thinking = (message.get("content") or "").strip()
        if thinking:
            yield f"__HB__:{thinking[:100]}\n"

        messages.append(message)
        for call in tool_calls:
            fn = call.get("function", {})
            fn_name = fn.get("name")
            fn_args = fn.get("arguments") or {}
            tools_called.add(fn_name)
            yield f"__HB__:Ejecutando herramienta `{fn_name}`...\n"
            tool_result = _dispatch_tool(fn_name, fn_args)
            messages.append({
                "role": "tool",
                "tool_name": fn_name,
                "content": json.dumps(tool_result, ensure_ascii=False)
            })

    # Se agotaron los turnos sin una respuesta final: forzar un último
    # llamado sin herramientas para no dejar la corrida colgada.
    yield "__HB__:Se agotaron los turnos de herramientas, pidiendo veredicto final...\n"
    messages.append({
        "role": "user",
        "content": "Ya no puedes usar más herramientas. Da tu veredicto final ahora mismo con el formato "
                    "pedido, usando únicamente la información que ya obtuviste."
    })
    result = None
    async for item in call_ollama_chat(messages, model_name, "Veredicto final", timeout=300.0, num_ctx=8192):
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
    gate = classify_severity(matrix_rows)
    yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"
    yield "\n\n✅ **Análisis completo.**\n"
