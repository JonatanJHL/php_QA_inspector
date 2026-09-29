import re


# --- Loop Engineering: segmentación de código para archivos grandes ---
# Actualizado (2026-07-25) tras confirmar el hardware real de este equipo:
# Apple M1 Pro, 16GB RAM unificada, Ollama corriendo en GPU (Metal, no
# CPU/iGPU). Modelo actual: qwen2.5-coder:14b con num_ctx=8192 (ver
# config.DEFAULT_NUM_CTX — 8192 en vez de un valor mayor porque a 16384 el
# modelo de 14b empezaba a ceder cómputo a CPU en este equipo; 8192 mantiene
# 100% GPU). Con esa ventana real, un archivo PHP de ~300-500 líneas
# (~12000-15000 caracteres, ~3000-4000 tokens) cabe en una sola llamada
# dejando margen para el system prompt + schema context. El umbral solo debe
# activar el modo por bloques para archivos que sí rebasarían esa ventana,
# no como precaución general.
LOOP_ENGINEERING_THRESHOLD_CHARS = 14000  # ~3500-4000 tokens; deja margen para el system prompt + schema context dentro de num_ctx=8192


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
