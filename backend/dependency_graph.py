import os
import re


# Dependency Graph & Impact Analysis Helpers
#
# Nota de diseño (2026-07-25): si en el futuro se agrega una verificación de
# compatibilidad de tipos entre archivos con relación DIRECTA (ej. columna
# CHAR/VARCHAR en db_schema.json vs. lo que el PHP le asigna), ese chequeo
# debería vivir en un módulo separado (p. ej. type_impact.py) que consuma
# `get_table_impact` de aquí — NO expandirlo al grafo transitivo de
# `get_impact_set`, que es intencionalmente de impacto amplio, no directo.
def build_dependency_graph(php_dir: str):
    files_list = []
    allowed_exts = ('.php', '.js', '.tpl', '.html', '.css', '.sql', '.json')
    for root, dirs, files in os.walk(php_dir):
        dirs[:] = [d for d in dirs if d not in ('vendor', '.git', 'node_modules', 'uploads_equipos', 'archivos')]
        for file in files:
            if file.endswith(allowed_exts):
                files_list.append(os.path.join(root, file))

    adj_includes = {}
    adj_included_by = {}
    base_names = {}          # basename -> [full_path, ...]  (may collide across folders)
    rel_paths = {}           # normalized rel path (from php_dir) -> full_path
    file_tables = {}         # full_path -> set of "schema.table" referenced

    for filepath in files_list:
        bname = os.path.basename(filepath)
        base_names.setdefault(bname, []).append(filepath)
        rel = os.path.relpath(filepath, php_dir).replace("\\", "/")
        rel_paths[rel] = filepath
        adj_includes[filepath] = []
        adj_included_by[filepath] = []

    # Plain literal includes: include/require(_once) 'some/path.php'
    include_pattern = re.compile(
        r'(?:include|require)(?:_once)?\s*\(?\s*[\'"]([^\'"]+)[\'"]\s*\)?',
        re.IGNORECASE
    )
    # __DIR__ . '/relative.php' concatenation, the most common pattern this
    # codebase uses for includes that aren't plain string literals (e.g.
    # `require __DIR__ . '/db_schema.php';` in bot.php).
    dir_concat_pattern = re.compile(
        r'(?:include|require)(?:_once)?\s*\(?\s*__DIR__\s*\.\s*[\'"]([^\'"]+)[\'"]\s*\)?',
        re.IGNORECASE
    )
    # Referencias a tablas, con o sin prefijo de schema (modulos.tblXxx,
    # whmcs81a.tblXxx, o solo tblXxx a secas — confirmado con datos reales que
    # la mayoría del código de este proyecto referencia las tablas SIN
    # prefijo, y antes de este fix solo se detectaba la forma con prefijo, lo
    # que dejaba file_tables vacío para la mayoría de los archivos reales).
    # Se captura siempre el nombre de tabla en su forma sin prefijo (bare),
    # para que dos archivos que referencien la misma tabla — uno con prefijo
    # y otro sin — sigan cayendo bajo la misma llave y se detecten como
    # relacionados.
    table_ref_pattern = re.compile(
        r'\b(?:(?:modulos|whmcs81a)\.)?(tbl[A-Za-z0-9_]+)\b'
    )

    ambiguous_basenames = set()

    for filepath in files_list:
        if not filepath.endswith('.php'):
            continue
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # --- table references (any file type we scan, not just includes) ---
            tables_found = {m.group(1) for m in table_ref_pattern.finditer(content)}
            if tables_found:
                file_tables[filepath] = tables_found

            # --- resolve include targets ---
            raw_targets = include_pattern.findall(content) + dir_concat_pattern.findall(content)
            file_dir = os.path.dirname(filepath)

            for raw in raw_targets:
                target_path = None

                # 1. Try resolving relative to the including file's own directory
                #    (handles __DIR__ . '/x.php' and './x.php' style relative includes).
                candidate = os.path.normpath(os.path.join(file_dir, raw))
                if candidate in rel_paths.values() or os.path.exists(candidate):
                    real_candidate = os.path.realpath(candidate)
                    for fp in files_list:
                        if os.path.realpath(fp) == real_candidate:
                            target_path = fp
                            break

                # 2. Try resolving relative to the configured php_dir root.
                if target_path is None:
                    norm_raw = raw.lstrip("./").replace("\\", "/")
                    if norm_raw in rel_paths:
                        target_path = rel_paths[norm_raw]

                # 3. Fallback: match by basename only, but flag ambiguity if the
                #    basename exists in more than one folder (previously this
                #    fallback was the *only* strategy, silently picking whichever
                #    path happened to be found first).
                if target_path is None:
                    inc_bname = os.path.basename(raw)
                    candidates = base_names.get(inc_bname, [])
                    if len(candidates) == 1:
                        target_path = candidates[0]
                    elif len(candidates) > 1:
                        ambiguous_basenames.add(inc_bname)
                        # Best-effort: still link to the first candidate so the
                        # graph isn't silently incomplete, but this case is
                        # reported back to the caller as a caveat.
                        target_path = candidates[0]

                if target_path and target_path != filepath and target_path not in adj_includes[filepath]:
                    adj_includes[filepath].append(target_path)
        except Exception:
            pass

    for source, targets in adj_includes.items():
        for target in targets:
            if source not in adj_included_by[target]:
                adj_included_by[target].append(source)

    return adj_includes, adj_included_by, file_tables, ambiguous_basenames


def get_impact_set(filepath: str, adj_included_by: dict):
    visited = set()
    queue = [filepath]
    while queue:
        current = queue.pop(0)
        if current not in visited:
            visited.add(current)
            parents = adj_included_by.get(current, [])
            for parent in parents:
                if parent not in visited:
                    queue.append(parent)
    visited.discard(filepath)
    return list(visited)


def get_table_impact(filepath: str, file_tables: dict):
    """Second impact layer: files that share a DB table with `filepath` even
    without any include/require relationship between them. A schema change or
    a destructive query on a shared table can break both files even though
    they never reference each other directly — the include-graph alone can't
    see that, so this is tracked separately.

    Returns: (tables_used_by_this_file, {table: [other_file, ...]})
    """
    # file_tables está indexado con las rutas nativas que devuelve os.walk
    # (os.path.join, separador de la plataforma). `filepath` puede llegar con
    # otro estilo de separador (ej. barras normales desde una API request) —
    # sin normalizar, el lookup fallaba en silencio y devolvía "sin tablas
    # compartidas" incluso cuando sí las había (confirmado con datos reales).
    filepath = os.path.normpath(filepath)
    my_tables = file_tables.get(filepath, set())
    if not my_tables:
        return set(), {}

    related_by_table = {}
    for other_path, other_tables in file_tables.items():
        if other_path == filepath:
            continue
        shared = my_tables & other_tables
        for tbl in shared:
            related_by_table.setdefault(tbl, []).append(other_path)

    return my_tables, related_by_table
