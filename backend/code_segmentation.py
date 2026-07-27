import re


# --- Loop Engineering: segmentación de código para archivos grandes ---
# Hermes3 corre localmente con una ventana de contexto limitada (confirmado
# con `ollama ps`: 4096 tokens por defecto). Archivos reales de este proyecto
# (500-900 líneas) generan prompts que rebasan esa ventana, causando que el
# modelo ignore las instrucciones del system prompt. En vez de depender solo
# de subir num_ctx (que ya ayuda, pero tiene límites de RAM/velocidad en CPU),
# para archivos grandes partimos el código en bloques lógicos completos
# (función por función, balanceando llaves para nunca cortar una a la mitad)
# y hacemos una llamada a Ollama por bloque, acumulando un resumen compacto
# de hallazgos entre pasadas — así cada llamada individual siempre cabe
# cómodamente en el contexto, sin sacrificar cobertura del archivo completo.
LOOP_ENGINEERING_THRESHOLD_CHARS = 8000  # ~2000 tokens; por debajo de esto, una sola llamada ya cabe bien


def segment_php_code(code: str, max_block_chars: int = 6000):
    """Split PHP code into logical blocks: each top-level function/method
    becomes its own brace-balanced block, never cut mid-function. Code
    outside any function is grouped into its own "top_level" block(s).
    Adjacent small top_level fragments are merged up to max_block_chars to
    avoid a wasteful separate call for a 3-line snippet."""
    lines = code.split("\n")

    func_pattern = re.compile(
        r'^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)\s*\([^)]*\)\s*(?::\s*\??\w+\s*)?\{',
        re.MULTILINE
    )

    func_blocks = []
    consumed_ranges = []

    for m in func_pattern.finditer(code):
        func_name = m.group(1)
        brace_start = m.end() - 1
        depth = 0
        pos = brace_start
        end_pos = None
        while pos < len(code):
            ch = code[pos]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end_pos = pos + 1
                    break
            pos += 1
        if end_pos is None:
            end_pos = len(code)

        func_start = m.start()
        func_code = code[func_start:end_pos]
        start_line = code[:func_start].count("\n") + 1
        end_line = code[:end_pos].count("\n") + 1

        func_blocks.append({
            "kind": "function", "name": func_name, "code": func_code,
            "start_line": start_line, "end_line": end_line,
            "char_start": func_start, "char_end": end_pos
        })
        consumed_ranges.append((func_start, end_pos))

    func_blocks.sort(key=lambda b: b["char_start"])
    consumed_ranges.sort()

    top_level_blocks = []
    cursor = 0
    for start, end in consumed_ranges:
        if start > cursor:
            gap_code = code[cursor:start]
            if gap_code.strip():
                top_level_blocks.append({
                    "kind": "top_level", "name": None, "code": gap_code,
                    "start_line": code[:cursor].count("\n") + 1,
                    "end_line": code[:start].count("\n") + 1,
                    "char_start": cursor, "char_end": start
                })
        cursor = max(cursor, end)
    if cursor < len(code):
        gap_code = code[cursor:]
        if gap_code.strip():
            top_level_blocks.append({
                "kind": "top_level", "name": None, "code": gap_code,
                "start_line": code[:cursor].count("\n") + 1,
                "end_line": len(lines),
                "char_start": cursor, "char_end": len(code)
            })

    all_blocks = func_blocks + top_level_blocks
    all_blocks.sort(key=lambda b: b["char_start"])

    merged = []
    current = None
    for b in all_blocks:
        if current is None:
            current = dict(b)
            continue
        combined_len = len(current["code"]) + len(b["code"])
        if combined_len <= max_block_chars and b["kind"] == "top_level" and current["kind"] == "top_level":
            current["code"] += b["code"]
            current["end_line"] = b["end_line"]
            current["char_end"] = b["char_end"]
        else:
            merged.append(current)
            current = dict(b)
    if current is not None:
        merged.append(current)

    for b in merged:
        b.pop("char_start", None)
        b.pop("char_end", None)

    return merged
