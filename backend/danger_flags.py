import os
import re


def analyze_danger_flags_content(content: str, ext: str):
    """Core danger-flag detector, operating on in-memory content.
    Shared by both the local filepath-based analysis and the remote
    inspect-content endpoint, so both stay in sync with the same rules."""
    flags = []
    danger_score = 0
    ext = ext.lower()

    # Check SQL queries — matches the *entire* quoted string using the actual
    # matching quote char, so embedded/escaped quotes inside the same SQL string
    # (e.g. "UPDATE t SET x='$val' WHERE id=$id") don't truncate the match early.
    sql_string_pattern = re.compile(
        r'([\'"])((?:\\.|(?!\1).)*?(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b(?:\\.|(?!\1).)*)\1',
        re.IGNORECASE | re.DOTALL
    )
    seen_sql_flags = set()
    for m in sql_string_pattern.finditer(content):
        sql_body = m.group(2)
        sql_upper = sql_body.upper()
        if ("DELETE" in sql_upper or "UPDATE" in sql_upper) and "WHERE" not in sql_upper:
            key = "delete_update_no_where"
            if key not in seen_sql_flags:
                seen_sql_flags.add(key)
                flags.append("Consulta SQL destructiva (DELETE/UPDATE) sin cláusula WHERE detectada.")
                danger_score += 45
        if "DROP" in sql_upper or "TRUNCATE" in sql_upper:
            key = "drop_truncate"
            if key not in seen_sql_flags:
                seen_sql_flags.add(key)
                flags.append("Consulta SQL estructural destructiva (DROP/TRUNCATE) detectada.")
                danger_score += 35

    # Specific language vulnerabilities
    if ext == '.php':
        # PHP function names/keywords are case-insensitive at the language level
        # (EVAL(), Eval(), eval() all execute identically) — confirmed via testing
        # (2026-07-27) that these checks previously missed uppercase/mixed-case
        # calls entirely because they lacked re.IGNORECASE. Note this does NOT
        # apply to the .js branch below: JavaScript IS case-sensitive, so eval()
        # vs EVAL() are genuinely different identifiers there.
        if re.search(r'(?<![a-zA-Z0-9_])eval\s*\(', content, re.IGNORECASE):
            flags.append("Uso de función eval() detectado (riesgo crítico de inyección de código).")
            danger_score += 50

        # Negative lookbehind on the function name prevents false positives like
        # curl_exec(), mysqli_exec()-style names, array_exec(), etc. matching "exec(".
        exec_funcs = ['exec', 'shell_exec', 'system', 'passthru', 'popen', 'proc_open']
        for fn in exec_funcs:
            if re.search(r'(?<![a-zA-Z0-9_])' + re.escape(fn) + r'\s*\(', content, re.IGNORECASE):
                flags.append(f"Ejecución de comandos del sistema ({fn}) detectada.")
                danger_score += 30

        if re.search(r'(?<![a-zA-Z0-9_])extract\s*\(', content, re.IGNORECASE):
            flags.append("Uso de extract() detectado (riesgo de colisión de variables).")
            danger_score += 15

        if re.search(r'mysqli_query\s*\(|\->query\s*\(', content, re.IGNORECASE):
            # Check if variable concatenation inside queries is present
            if re.search(r'\b(mysqli_query|\->query)\s*\(\s*[^,]*\$.*[\'"].*[\'"]', content, re.IGNORECASE):
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

    elif ext == '.js':
        if re.search(r'(?<![a-zA-Z0-9_])eval\s*\(', content):
            flags.append("Uso de eval() detectado en JavaScript.")
            danger_score += 50
        if "child_process" in content or re.search(r'(?<![a-zA-Z0-9_])exec\s*\(', content):
            flags.append("Ejecución de procesos del sistema (child_process) detectada en JS.")
            danger_score += 30

    danger_score = min(danger_score, 100)
    return flags, danger_score


def analyze_danger_flags(filepath: str):
    """Filepath-based wrapper around analyze_danger_flags_content(), kept for
    backwards compatibility with callers that pass a path on disk."""
    if not os.path.exists(filepath):
        return [], 0
    ext = os.path.splitext(filepath)[1].lower()
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return ["No se pudo leer el archivo para análisis de peligro"], 50
    return analyze_danger_flags_content(content, ext)
