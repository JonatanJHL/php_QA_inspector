"""Puente Python -> nikic/php-parser (vía subprocess a PHP).

Da acceso a AST real y taint analysis básico dentro de un solo archivo,
en vez de depender únicamente de regex sobre texto (ver danger_flags.py).
Deliberadamente NO reemplaza el detector de regex — lo complementa: el
regex sigue detectando patrones de texto rápido (útil incluso si el AST
falla por código con errores de sintaxis), y este módulo añade una capa de
verificación más profunda cuando el archivo sí parsea correctamente.

Requiere: php + composer install ya corrido en backend/php_ast_bridge/
(ver backend/php_ast_bridge/composer.json). Si el binario de PHP no está
disponible, o el parseo falla, retorna ok=False y el llamador debe seguir
funcionando solo con el detector de regex — esto nunca debe ser un punto
único de falla para todo el análisis de peligro.
"""

import os
import re
import json
import subprocess


_BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "php_ast_bridge", "parse_ast.php")


def parse_php_ast(code: str, timeout: float = 10.0) -> dict:
    """Ejecuta el puente PHP sobre el código dado. Devuelve:
    {"ok": True, "tainted_vars": {...}, "assignments": [...], "calls": [...]}
    o {"ok": False, "error": "..."} si algo falla (PHP no disponible, timeout,
    error de parseo real en el código, etc.) — nunca lanza excepción, para
    que el llamador pueda hacer fallback al detector de regex sin romper
    el flujo de análisis completo."""
    if not os.path.exists(_BRIDGE_SCRIPT):
        return {"ok": False, "error": "php_ast_bridge no está instalado (falta parse_ast.php o vendor/)."}

    try:
        result = subprocess.run(
            ["php", _BRIDGE_SCRIPT],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "El binario 'php' no está disponible en PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"El parseo AST excedió el timeout de {timeout}s."}
    except Exception as e:
        return {"ok": False, "error": f"Error ejecutando el puente PHP: {e}"}

    if result.returncode != 0 and not result.stdout.strip():
        return {"ok": False, "error": result.stderr.strip() or "El puente PHP no produjo salida."}

    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "error": "La salida del puente PHP no es JSON válido."}

    return parsed


# Nombres de función/método considerados "sinks" peligrosos si reciben un
# argumento marcado como tainted por parse_php_ast(). Mantenido separado de
# danger_flags.py para que este módulo pueda usarse independientemente
# (ej. en pruebas) sin arrastrar todas las reglas de detección de texto.
SQL_SINKS = {"mysqli_query", "query", "exec", "execute"}
COMMAND_SINKS = {"exec", "shell_exec", "system", "passthru", "popen", "proc_open"}
CODE_SINKS = {"eval", "assert"}
# path_traversal: acceso a ARCHIVO LOCAL con ruta controlada por el usuario
# (leer/incluir/borrar algo fuera del directorio esperado).
FILE_SINKS = {"include", "require", "include_once", "require_once", "fopen", "unlink"}
# ssrf: la MISMA familia de funciones que file_traversal en PHP suele servir
# también para pedir una URL remota (file_get_contents('http://...')) —
# se separan por categoría (no por función) más abajo, según si el string
# resuelto por el AST parece una URL o una ruta local.
SSRF_CANDIDATE_SINKS = {"file_get_contents", "fopen", "curl_setopt", "curl_init"}
# xss: los "sinks" sintéticos que parse_ast.php genera para echo/print,
# ya que estas son construcciones del lenguaje, no llamadas a función.
XSS_SINKS = {"__echo__", "__print__"}
# unserialize inseguro: deserializar datos controlados por el usuario sin
# validar/restringir clases permite inyección de objetos (PHP Object
# Injection) si alguna clase cargada tiene métodos mágicos explotables
# (__wakeup, __destruct, etc.) — vulnerabilidad clásica en apps PHP legacy.
UNSERIALIZE_SINKS = {"unserialize"}

_URL_LIKE_RE = re.compile(r'^https?://', re.IGNORECASE)


def find_tainted_sinks(ast_result: dict) -> list:
    """A partir del resultado de parse_php_ast(), devuelve una lista de
    hallazgos concretos: llamadas a funciones peligrosas (SQL, comandos,
    eval, includes) donde al menos un argumento fue marcado 'tainted'
    (viene de $_GET/$_POST/$_REQUEST/$_COOKIE, directo o vía 1+ variables
    intermedias). Cada hallazgo trae la línea real y el origen del taint,
    para que el mensaje al usuario sea específico y accionable."""
    if not ast_result.get("ok"):
        return []

    findings = []
    for call in ast_result.get("calls", []):
        name = call.get("name", "")
        line = call.get("line")
        for arg in call.get("args", []):
            if not arg.get("tainted"):
                continue

            if name in SQL_SINKS:
                category = "sql_injection"
                label = "consulta SQL"
            elif name in COMMAND_SINKS:
                category = "command_injection"
                label = "ejecución de comando del sistema"
            elif name in CODE_SINKS:
                category = "code_injection"
                label = "evaluación de código dinámico"
            elif name in XSS_SINKS:
                category = "xss"
                label = "salida de HTML (posible XSS reflejado)"
            elif name in UNSERIALIZE_SINKS:
                category = "insecure_deserialization"
                label = "deserialización (unserialize)"
            elif name in SSRF_CANDIDATE_SINKS:
                # Estas funciones sirven tanto para archivo local como para
                # URL remota — se distingue por si el valor RESUELTO (no
                # solo el nombre de variable) parece una URL. Si el AST no
                # pudo resolver el texto real (ej. viene de una variable
                # cuyo valor no se pudo reconstruir), se asume SSRF por
                # defecto ya que es el riesgo más común con estas funciones
                # cuando reciben entrada de usuario sin validar.
                resolved_text = arg.get("value") or ""
                if isinstance(resolved_text, str) and resolved_text and not _URL_LIKE_RE.match(resolved_text) and ("/" in resolved_text or "\\" in resolved_text) and not resolved_text.startswith("http"):
                    category = "path_traversal"
                    label = "acceso a archivo"
                else:
                    category = "ssrf"
                    label = "petición de red (posible SSRF)"
            elif name in FILE_SINKS:
                category = "path_traversal"
                label = "acceso a archivo"
            else:
                continue

            display_call = name
            if name in XSS_SINKS:
                display_call = "echo" if name == "__echo__" else "print"

            findings.append({
                "category": category,
                "line": line,
                "function": name,
                "taint_source": arg.get("taint_source"),
                "message": (
                    f"Línea {line}: {label} vía `{display_call}(...)` recibe un valor que proviene de "
                    f"entrada de usuario ({arg.get('taint_source')}) sin pasar por una asignación "
                    f"intermedia que la sanee visiblemente — confirmado siguiendo el flujo real de "
                    f"la variable, no solo por coincidencia de texto en la misma línea."
                ),
            })

    return findings


_DESTRUCTIVE_SQL_RE = re.compile(r'\b(DELETE|UPDATE)\b', re.IGNORECASE)
_WHERE_RE = re.compile(r'\bWHERE\b', re.IGNORECASE)
_TRIVIAL_WHERE_RE = re.compile(
    r'WHERE\s+(?:1\s*=\s*1|\'1\'\s*=\s*\'1\'|true\b|\d+\s*>\s*0\b)', re.IGNORECASE
)
# Custom DB wrapper methods used across this codebase instead of raw
# mysqli_query/PDO — sin esto, cualquier archivo que use $dao->execute(...)
# o $conn->query(...) (el patrón real en CAPA 8, ver dao2->execute) queda
# fuera del taint tracking de find_tainted_sinks() porque esos nombres no
# están en SQL_SINKS por defecto.
CUSTOM_DB_METHOD_NAMES = {"execute", "query", "run", "ejecutar"}


def find_missing_where_via_ast(ast_result: dict) -> list:
    """Complementa el chequeo de regex de danger_flags.py (que solo ve el
    texto literal de la query en el string donde aparece) con una versión
    que sigue variables: si $sql = "DELETE FROM x WHERE ..." se construye en
    una línea y luego se pasa como variable a un método (ej. $dao->execute
    ($sql)) en otra línea distinta, el regex plano sobre el string nunca ve
    la palabra 'execute' junto al SQL — pero el AST sí puede reconstruir el
    valor de la variable en el momento en que se usa como argumento.

    IMPORTANTE: solo evalúa el string RECONSTRUIDO por el AST, ignorando
    por completo cualquier cosa dentro de comentarios de bloque o de línea
    (el parser nunca los incluye como nodos) — esto es lo que corrige el
    falso positivo real encontrado en borrar_licencia.php, donde el regex
    plano detectaba 'sin WHERE' dentro de un bloque /* ... */ que nunca se
    ejecuta."""
    if not ast_result.get("ok"):
        return []

    findings = []
    for call in ast_result.get("calls", []):
        name = call.get("name", "")
        if name not in CUSTOM_DB_METHOD_NAMES and call.get("type") != "function":
            continue
        line = call.get("line")

        for arg in call.get("args", []):
            sql_text = arg.get("value")
            if not sql_text or not isinstance(sql_text, str):
                continue
            if not _DESTRUCTIVE_SQL_RE.search(sql_text):
                continue

            has_where = bool(_WHERE_RE.search(sql_text))
            has_trivial_where = bool(_TRIVIAL_WHERE_RE.search(sql_text))

            if not has_where or has_trivial_where:
                reason = "condición WHERE trivial (siempre verdadera)" if has_trivial_where else "sin cláusula WHERE"
                findings.append({
                    "line": line,
                    "function": name,
                    "sql_preview": sql_text[:150],
                    "message": (
                        f"Línea {line}: consulta destructiva (DELETE/UPDATE) con {reason}, "
                        f"detectada siguiendo el valor real de la variable pasada a `{name}(...)` "
                        f"(no solo por texto en la misma línea) — código verificado como REALMENTE "
                        f"ejecutable, no dentro de un comentario."
                    ),
                })

    return findings
