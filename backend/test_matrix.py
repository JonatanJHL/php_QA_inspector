import re


# --- Gate de riesgo: parsear la Matriz de Casos de Prueba y clasificar
# severidad de las filas que fallan. Esto convierte el texto libre de
# Hermes3 en un veredicto estructurado que la UI puede usar para bloquear
# visualmente el archivo (crítico) o pedir confirmación (medio), en vez de
# depender de que el usuario lea manualmente toda la tabla y decida solo. ---
def parse_test_matrix(markdown_text: str):
    """Parse the '### 🧪 Matriz de Casos de Prueba' Markdown table produced by
    Hermes3 into structured rows. Returns a list of dicts, one per row:
    {numero, tipo, entrada, linea_o_funcion, resultado_esperado, veredicto}.
    Tolerant of minor formatting variance (extra spaces, missing trailing
    pipe, etc.) since this is LLM-generated Markdown, not machine-generated."""
    rows = []
    lines = markdown_text.split("\n")

    table_start = None
    for i, line in enumerate(lines):
        if "Pasa o Falla" in line and "|" in line:
            table_start = i
            break
    if table_start is None:
        return rows

    i = table_start + 1
    if i < len(lines) and re.match(r'^\s*\|?[\s:-]+\|', lines[i]):
        i += 1

    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith('|'):
            break
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) >= 6:
            rows.append({
                "numero": cells[0],
                "tipo": cells[1],
                "entrada": cells[2],
                "linea_o_funcion": cells[3],
                "resultado_esperado": cells[4],
                "veredicto": cells[5]
            })
        i += 1

    return rows


def classify_severity(rows: list, danger_score: int = 0, danger_flags: list = None):
    """Classify each 'Falla hoy' row as critical or medium severity.
    Critical = data loss/corruption, uncaught exceptions that crash the
    process, division-by-zero in financial calcs, or SQL injection/hardcoded
    credential language. Everything else that fails is medium severity
    (surfaced as a confirmation prompt, not a hard block).

    ALSO incorpora danger_score (de danger_flags.py) como una segunda vía de
    bloqueo independiente del texto de la matriz. Antes, el gate dependía
    ÚNICAMENTE de que el LLM redactara la fila con las palabras clave
    correctas ('elimina', 'borra', etc.) — un DELETE/UPDATE sin WHERE
    detectado de forma determinística por danger_flags.py podía quedar sin
    reflejarse en el veredicto si el modelo no lo mencionó en la tabla, o lo
    describió con otras palabras. danger_score >= 65 fuerza 'bloqueado'
    incluso si la matriz de casos no contiene ninguna fila con esas
    keywords, porque ese umbral ya corresponde a hallazgos verificados por
    código (no interpretación del LLM), como DELETE/UPDATE sin WHERE
    apuntando a un host de producción."""
    critical_keywords = [
        'división por cero', 'division por cero', 'divide by zero',
        'no capturada', 'no manejada', 'sin capturar', 'uncaught',
        'pérdida de datos', 'perdida de datos', 'corrupción', 'corrupcion',
        'sin validar', 'sin validación', 'sin validacion', 'no valida',
        'inyección', 'inyeccion', 'sql injection',
        'colapsa', 'crashea', 'detiene el proceso', 'cae el servidor',
        'elimina', 'borra', 'delete', 'sobrescribe', 'sobreescribe',
    ]

    critical_rows = []
    medium_rows = []

    for row in rows:
        veredicto_lower = row["veredicto"].lower().strip()
        is_failure = veredicto_lower in ("no", "falla", "falla hoy") or "falla" in veredicto_lower

        if not is_failure:
            continue

        combined_text = f"{row['entrada']} {row['linea_o_funcion']} {row['resultado_esperado']}".lower()
        is_critical = any(kw in combined_text for kw in critical_keywords)

        if is_critical:
            critical_rows.append(row)
        else:
            medium_rows.append(row)

    # Umbral determinístico independiente del texto de la matriz. 65+ ya
    # corresponde a hallazgos con múltiples señales agravantes confirmadas en
    # código (ver danger_flags.py: DELETE/UPDATE sin WHERE + host de
    # producción, o + entrada de usuario directa). 30-64 sube a
    # 'requiere_confirmacion' aunque la matriz esté "limpia" en apariencia.
    danger_forces_block = danger_score >= 65
    danger_forces_review = 30 <= danger_score < 65

    if critical_rows or danger_forces_block:
        gate_status = "bloqueado"
    elif medium_rows or danger_forces_review:
        gate_status = "requiere_confirmacion"
    else:
        gate_status = "aprobado"

    return {
        "gate_status": gate_status,
        "fallas_criticas": critical_rows,
        "fallas_medias": medium_rows,
        "total_casos": len(rows),
        "danger_score": danger_score,
        "danger_flags": danger_flags or [],
    }
