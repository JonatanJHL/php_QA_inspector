import os
import re
import json

from ast_bridge import parse_php_ast, find_tainted_sinks, find_missing_where_via_ast


# --- Clasificación opcional de hosts producción/desarrollo (ver
# knowledge/db_hosts.json). Si el archivo no existe o está vacío, cae a
# patrones genéricos de palabras clave — nunca falla silenciosamente por
# falta de config, solo pierde precisión. Cargado una vez al importar. ---
_DB_HOSTS_PATH = os.path.join(os.path.dirname(__file__), "knowledge", "db_hosts.json")
_known_hosts = {"produccion": [], "desarrollo": []}
try:
    with open(_DB_HOSTS_PATH, "r", encoding="utf-8") as _f:
        _loaded = json.load(_f)
        _known_hosts["produccion"] = [h.lower() for h in _loaded.get("produccion", [])]
        _known_hosts["desarrollo"] = [h.lower() for h in _loaded.get("desarrollo", [])]
except Exception:
    pass  # Sin archivo de hosts conocidos: el detector genérico sigue funcionando solo

_PROD_KEYWORDS = ('prod', 'production', 'live')
_NONPROD_KEYWORDS = ('dev', 'test', 'staging', 'stage', 'local', 'sandbox', '127.0.0.1', 'localhost')

# Patrones de conexión a BD comunes en PHP: mysqli_connect, new PDO, new mysqli,
# y arrays de config típicos ($config['host'] = '...', 'DB_HOST' => '...').
_CONNECTION_PATTERNS = [
    re.compile(r'mysqli_connect\s*\(\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
    re.compile(r'new\s+mysqli\s*\(\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
    re.compile(r'new\s+PDO\s*\(\s*[\'"]mysql:host=([^;\'"]+)', re.IGNORECASE),
    re.compile(r'(?:DB_HOST|db_host|host)\s*(?:=>|=)\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
]


def detect_connection_targets(content: str):
    """Busca strings de host/conexión a BD en el código y los clasifica como
    producción, desarrollo, o desconocido. No asume nada por defecto: un
    host que no coincide con ningún patrón conocido se reporta como
    'desconocido', no como seguro. Devuelve una lista de dicts
    {host, clasificacion}."""
    found = {}
    for pattern in _CONNECTION_PATTERNS:
        for m in pattern.finditer(content):
            host = m.group(1).strip()
            if not host or host in found:
                continue
            host_lower = host.lower()

            if host_lower in _known_hosts["produccion"]:
                clasificacion = "produccion"
            elif host_lower in _known_hosts["desarrollo"]:
                clasificacion = "desarrollo"
            elif any(kw in host_lower for kw in _PROD_KEYWORDS):
                clasificacion = "produccion (por nombre)"
            elif any(kw in host_lower for kw in _NONPROD_KEYWORDS):
                clasificacion = "desarrollo (por nombre)"
            else:
                clasificacion = "desconocido"

            found[host] = clasificacion

    return [{"host": h, "clasificacion": c} for h, c in found.items()]


def analyze_danger_flags_content(content: str, ext: str, filepath: str = None, return_structured: bool = False):
    """Core danger-flag detector, operating on in-memory content.
    Shared by both the local filepath-based analysis and the remote
    inspect-content endpoint, so both stay in sync with the same rules.

    `filepath` es OPCIONAL y solo se usa para consultar falsos positivos
    confirmados por el usuario (ver qa_history.load_false_positives). Sin
    filepath (ej. el endpoint remoto /api/qa/inspect-content, que recibe
    contenido sin un path local válido), el detector funciona exactamente
    igual, solo sin la capacidad de silenciar hallazgos ya marcados.

    `return_structured=True` hace que la función devuelva una TERCERA lista
    con los hallazgos que tienen category+line reales (los del AST — los
    únicos que se pueden marcar como falso positivo desde el frontend). Por
    defecto es False para no romper la firma de 2 valores que ya usan 5
    llamadores existentes en el proyecto."""
    flags = []
    structured_findings = []
    danger_score = 0
    ext = ext.lower()

    false_positives = {}
    if filepath:
        try:
            from qa_history import load_false_positives
            false_positives = load_false_positives(filepath)
        except Exception:
            false_positives = {}

    # Check SQL queries — matches the *entire* quoted string using the actual
    # matching quote char, so embedded/escaped quotes inside the same SQL string
    # (e.g. "UPDATE t SET x='$val' WHERE id=$id") don't truncate the match early.
    sql_string_pattern = re.compile(
        r'([\'"])((?:\\.|(?!\1).)*?(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b(?:\\.|(?!\1).)*)\1',
        re.IGNORECASE | re.DOTALL
    )
    # WHERE trivial: presente en el texto pero con una condición que siempre
    # es verdadera (1=1, id>0, etc.) — tan destructivo como no tener WHERE,
    # pero pasaría desapercibido si solo se busca la palabra "WHERE".
    trivial_where_pattern = re.compile(
        r'WHERE\s+(?:1\s*=\s*1|\'1\'\s*=\s*\'1\'|true\b|\d+\s*>\s*0\b)',
        re.IGNORECASE
    )
    seen_sql_flags = set()
    connection_targets = detect_connection_targets(content)
    prod_targets = [t for t in connection_targets if t["clasificacion"].startswith("produccion")]
    has_any_destructive_query = False

    # --- AST real primero (solo .php): si el archivo parsea correctamente,
    # usamos find_missing_where_via_ast como fuente de verdad para el
    # chequeo de WHERE en vez del regex de más abajo. Esto es necesario
    # porque el regex opera sobre el TEXTO COMPLETO del archivo, incluyendo
    # comentarios de bloque /* ... */ — confirmado con un caso real
    # (borrar_licencia.php en CAPA 8) que el regex reportaba "sin WHERE"
    # dentro de código comentado que nunca se ejecuta, un falso positivo que
    # el AST evita por construcción (el parser jamás emite nodos para
    # comentarios). Cuando el AST no está disponible (PHP no instalado, o
    # error de sintaxis real en el archivo), ast_missing_where queda None y
    # el regex de abajo actúa como iba a hacerlo de todas formas.
    ast_result = parse_php_ast(content) if ext == '.php' else {"ok": False}
    ast_missing_where = find_missing_where_via_ast(ast_result) if ast_result.get("ok") else None

    if ast_missing_where is not None:
        # AST disponible: reporta SOLO los hallazgos reales confirmados por
        # estructura, y marca que el regex de más abajo no debe repetir esta
        # regla (evita duplicar el mismo hallazgo dos veces con textos
        # distintos, y evita el falso positivo de comentarios).
        for finding in ast_missing_where:
            fp_key = f"missing_where:{finding['line']}"
            if fp_key in false_positives:
                continue  # Confirmado como falso positivo por el usuario para este archivo específico
            flags.append(f"CRÍTICO (verificado por AST, código realmente ejecutable): {finding['message']}")
            structured_findings.append({
                "category": "missing_where",
                "line": finding['line'],
                "message": finding['message'],
                "fp_key": fp_key,
            })
            danger_score += 45
            has_any_destructive_query = True
            if prod_targets:
                hosts_str = ", ".join(t["host"] for t in prod_targets)
                flags.append(f"CRÍTICO: la consulta destructiva de la línea {finding['line']} corre en un archivo cuya conexión apunta a un host de PRODUCCIÓN ({hosts_str}).")
                danger_score += 25
        skip_regex_where_check = True
    else:
        skip_regex_where_check = False

    for m in sql_string_pattern.finditer(content):
        sql_body = m.group(2)
        sql_upper = sql_body.upper()
        is_destructive = "DELETE" in sql_upper or "UPDATE" in sql_upper
        has_where = "WHERE" in sql_upper
        has_trivial_where = bool(trivial_where_pattern.search(sql_body))
        if is_destructive:
            has_any_destructive_query = True

        if is_destructive and (not has_where or has_trivial_where) and not skip_regex_where_check:
            key = "delete_update_no_where"
            if key not in seen_sql_flags:
                seen_sql_flags.add(key)
                if has_trivial_where:
                    flags.append("Consulta SQL destructiva (DELETE/UPDATE) con condición WHERE trivial (siempre verdadera) — equivale a no tener WHERE.")
                else:
                    flags.append("Consulta SQL destructiva (DELETE/UPDATE) sin cláusula WHERE detectada.")
                danger_score += 45

            # Agravante: si además esta misma query recibe entrada de usuario
            # sin escapar ($_GET/$_POST/$_REQUEST concatenado directamente),
            # el riesgo es mucho mayor que cualquiera de los dos por separado.
            if re.search(r'\$_(GET|POST|REQUEST)\b', sql_body):
                key2 = "delete_update_user_input"
                if key2 not in seen_sql_flags:
                    seen_sql_flags.add(key2)
                    flags.append("CRÍTICO: consulta destructiva (DELETE/UPDATE) sin WHERE efectivo, alimentada directamente por entrada de usuario ($_GET/$_POST/$_REQUEST).")
                    danger_score += 30

            # Agravante: si se detectó una query destructiva y la conexión
            # apunta a un host clasificado como producción, el riesgo real de
            # ejecutar esto es mucho mayor que en un entorno de desarrollo.
            if prod_targets:
                key3 = "delete_update_prod_target"
                if key3 not in seen_sql_flags:
                    seen_sql_flags.add(key3)
                    hosts_str = ", ".join(t["host"] for t in prod_targets)
                    flags.append(f"CRÍTICO: consulta destructiva detectada y la conexión del archivo apunta a un host de PRODUCCIÓN ({hosts_str}). Verifica que este script no se ejecute contra la base equivocada.")
                    danger_score += 25

        if "DROP" in sql_upper or "TRUNCATE" in sql_upper:
            key = "drop_truncate"
            if key not in seen_sql_flags:
                seen_sql_flags.add(key)
                flags.append("Consulta SQL estructural destructiva (DROP/TRUNCATE) detectada.")
                danger_score += 35
            if prod_targets:
                key4 = "drop_truncate_prod_target"
                if key4 not in seen_sql_flags:
                    seen_sql_flags.add(key4)
                    hosts_str = ", ".join(t["host"] for t in prod_targets)
                    flags.append(f"CRÍTICO: DROP/TRUNCATE detectado y la conexión apunta a un host de PRODUCCIÓN ({hosts_str}).")
                    danger_score += 25

    # Reporta hosts de conexión desconocidos (ni prod ni dev por ningún
    # patrón) como advertencia informativa de baja severidad — no es un
    # peligro en sí, pero vale la pena que el humano lo confirme.
    unknown_targets = [t for t in connection_targets if t["clasificacion"] == "desconocido"]
    if unknown_targets and has_any_destructive_query:
        hosts_str = ", ".join(t["host"] for t in unknown_targets)
        flags.append(f"El archivo tiene consultas destructivas y se conecta a un host no clasificado ({hosts_str}) — confirma manualmente si es producción o desarrollo.")
        danger_score += 10

    # Specific language vulnerabilities
    if ext == '.php':
        if re.search(r'(?<![a-zA-Z0-9_])eval\s*\(', content):
            flags.append("Uso de función eval() detectado (riesgo crítico de inyección de código).")
            danger_score += 50

        # Negative lookbehind on the function name prevents false positives like
        # curl_exec(), mysqli_exec()-style names, array_exec(), etc. matching "exec(".
        exec_funcs = ['exec', 'shell_exec', 'system', 'passthru', 'popen', 'proc_open']
        for fn in exec_funcs:
            if re.search(r'(?<![a-zA-Z0-9_])' + re.escape(fn) + r'\s*\(', content):
                flags.append(f"Ejecución de comandos del sistema ({fn}) detectada.")
                danger_score += 30

        if re.search(r'(?<![a-zA-Z0-9_])extract\s*\(', content):
            flags.append("Uso de extract() detectado (riesgo de colisión de variables).")
            danger_score += 15

        if "mysqli_query(" in content or "$conn->query(" in content:
            # Check if variable concatenation inside queries is present
            if re.search(r'\b(mysqli_query|\->query)\s*\(\s*[^,]*\$.*[\'"].*[\'"]', content):
                flags.append("Posible consulta SQL dinámica con concatenación directa de variables (Riesgo de SQL Injection).")
                danger_score += 25

        # --- Hardcoded credentials / secrets ---
        cred_patterns = [
            (r'sk-ant-[A-Za-z0-9\-_]{20,}', 'API key de Anthropic'),
            (r'sk-[A-Za-z0-9]{20,}', 'API key estilo OpenAI/genérica'),
            (r'xox[bpsr]-[A-Za-z0-9\-]{10,}', 'Token de Slack (bot/user/app)'),
            (r'AIza[0-9A-Za-z\-_]{35}', 'API key de Google'),
            (r'AKIA[0-9A-Z]{16}', 'AWS Access Key ID'),
            (r'ghp_[A-Za-z0-9]{36}', 'GitHub Personal Access Token'),
        ]
        for pattern, label in cred_patterns:
            if re.search(pattern, content):
                flags.append(f"CREDENCIAL HARDCODEADA DETECTADA: {label}. Debe moverse a variable de entorno o config fuera del webroot.")
                danger_score += 40

        # --- Taint analysis real vía AST (nikic/php-parser), no regex ---
        # Complementa (no reemplaza) las detecciones de arriba: sigue el
        # flujo real de una variable desde $_GET/$_POST/etc. hasta un sink
        # peligroso, incluso a través de 1+ asignaciones intermedias, que un
        # regex sobre el texto nunca puede ver (ej. `$id = $_GET['id']; ...
        # DELETE ... WHERE id = $id` en líneas separadas). Reutiliza el
        # ast_result ya calculado más arriba (mismo content) — evita una
        # segunda llamada al subprocess de PHP, que es la parte cara de
        # todo este análisis. Si el binario de PHP no está disponible o el
        # archivo tiene un error de sintaxis, ast_result.ok es False y esto
        # simplemente no aporta nada — el resto del detector de regex sigue
        # funcionando normalmente.
        if ast_result.get("ok"):
            tainted_findings = find_tainted_sinks(ast_result)
            seen_taint_categories = set()
            for finding in tainted_findings:
                category = finding["category"]
                fp_key = f"{category}:{finding['line']}"
                if fp_key in false_positives:
                    continue  # Confirmado como falso positivo por el usuario para este archivo específico
                flags.append(f"CRÍTICO (verificado por análisis de flujo real): {finding['message']}")
                structured_findings.append({
                    "category": category,
                    "line": finding['line'],
                    "message": finding['message'],
                    "fp_key": fp_key,
                })
                if category not in seen_taint_categories:
                    seen_taint_categories.add(category)
                    # Más peso que el regex equivalente porque esto está
                    # confirmado por seguimiento real de la variable, no por
                    # coincidencia de patrón de texto — menor probabilidad
                    # de falso positivo.
                    danger_score += 35

    elif ext == '.js':
        if re.search(r'(?<![a-zA-Z0-9_])eval\s*\(', content):
            flags.append("Uso de eval() detectado en JavaScript.")
            danger_score += 50
        if "child_process" in content or re.search(r'(?<![a-zA-Z0-9_])exec\s*\(', content):
            flags.append("Ejecución de procesos del sistema (child_process) detectada en JS.")
            danger_score += 30

    danger_score = min(danger_score, 100)
    if return_structured:
        return flags, danger_score, structured_findings
    return flags, danger_score


def analyze_danger_flags(filepath: str, return_structured: bool = False):
    """Filepath-based wrapper around analyze_danger_flags_content(), kept for
    backwards compatibility with callers that pass a path on disk."""
    if not os.path.exists(filepath):
        return ([], 0, []) if return_structured else ([], 0)
    ext = os.path.splitext(filepath)[1].lower()
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return (["No se pudo leer el archivo para análisis de peligro"], 50, []) if return_structured else (["No se pudo leer el archivo para análisis de peligro"], 50)
    return analyze_danger_flags_content(content, ext, filepath=filepath, return_structured=return_structured)
