import os
import json
import time

from dependency_graph import build_dependency_graph


# --- Cache del grafo de dependencias del proyecto (memoria de largo plazo) ---
# build_dependency_graph() recorre TODO settings.php_dir con os.walk() y
# vuelve a leer cada archivo .php con regex cada vez que se llama — hoy eso
# pasa en cada request a /api/qa/impact y en cada llamada a la tool
# `obtener_impacto` del agente (agent_loop.py). Para un proyecto grande eso
# es I/O repetido sin necesidad: el grafo de includes/tablas no cambia
# entre una llamada y la siguiente a menos que el código del proyecto
# cambie. Este módulo persiste el resultado una vez calculado y solo lo
# recalcula si detecta que algún archivo se modificó (por mtime), o si se
# pide explícitamente con force=True.

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "qa_history")
os.makedirs(CACHE_DIR, exist_ok=True)

GRAPH_CACHE_PATH = os.path.join(CACHE_DIR, "project_graph_cache.json")


def _scan_mtimes(php_dir: str) -> dict:
    """Snapshot ligero: ruta -> mtime de cada archivo relevante. Se usa
    solo para decidir si el cache sigue vigente, no se guarda como parte
    del grafo persistido."""
    allowed_exts = ('.php', '.js', '.tpl', '.html', '.css', '.sql', '.json')
    mtimes = {}
    for root, dirs, files in os.walk(php_dir):
        dirs[:] = [d for d in dirs if d not in ('vendor', '.git', 'node_modules', 'uploads_equipos', 'archivos')]
        for file in files:
            if file.endswith(allowed_exts):
                full_path = os.path.join(root, file)
                try:
                    mtimes[full_path] = os.path.getmtime(full_path)
                except OSError:
                    pass
    return mtimes


def get_project_graph(php_dir: str, force: bool = False) -> dict:
    """Devuelve el grafo de dependencias del proyecto, usando el cache en
    disco si sigue vigente. `force=True` ignora el cache y recalcula (botón
    de "reindexar" manual desde el frontend).

    Formato devuelto (todo con rutas RELATIVAS a php_dir, para que el cache
    sea portable si el proyecto se mueve de carpeta):
    {
        "php_dir": ...,
        "generated_at": ISO timestamp,
        "adj_includes": {rel_path: [rel_path, ...]},
        "adj_included_by": {rel_path: [rel_path, ...]},
        "file_tables": {rel_path: [tabla, ...]},
        "ambiguous_basenames": [nombre, ...]
    }
    """
    real_php_dir = os.path.realpath(php_dir)
    current_mtimes = _scan_mtimes(real_php_dir)

    if not force and os.path.exists(GRAPH_CACHE_PATH):
        try:
            with open(GRAPH_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if (cached.get("php_dir") == real_php_dir
                    and cached.get("file_mtimes") == current_mtimes):
                return cached
        except Exception:
            pass  # cache corrupto o ilegible: recalcular abajo

    adj_includes, adj_included_by, file_tables, ambiguous_basenames = build_dependency_graph(real_php_dir)

    def to_rel(p):
        return os.path.relpath(p, real_php_dir).replace("\\", "/")

    graph = {
        "php_dir": real_php_dir,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file_mtimes": current_mtimes,
        "adj_includes": {to_rel(k): [to_rel(v) for v in vs] for k, vs in adj_includes.items()},
        "adj_included_by": {to_rel(k): [to_rel(v) for v in vs] for k, vs in adj_included_by.items()},
        "file_tables": {to_rel(k): sorted(v) for k, v in file_tables.items() if v},
        "ambiguous_basenames": sorted(ambiguous_basenames),
    }

    try:
        with open(GRAPH_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # si no se puede persistir, seguimos con el grafo recién calculado en memoria

    return graph


def get_file_context_summary(php_dir: str, rel_filepath: str) -> str:
    """Resumen corto en texto, listo para inyectar en el system prompt del
    agente ANTES de que empiece a investigar (a diferencia de la tool
    `obtener_impacto`, que el modelo debe decidir llamar). Da una primera
    señal de qué tan conectado está el archivo, para orientar la
    investigación desde el turno 1 en vez de descubrirlo a ciegas."""
    graph = get_project_graph(php_dir)
    rel = rel_filepath.replace("\\", "/")

    includes = graph["adj_includes"].get(rel, [])
    included_by = graph["adj_included_by"].get(rel, [])
    tables = graph["file_tables"].get(rel, [])

    if not includes and not included_by and not tables:
        return (
            f"Contexto del proyecto: `{rel}` no tiene includes/requires detectados hacia otros archivos, "
            "nadie más lo incluye, y no referencia ninguna tabla conocida. Es probable que sea un script "
            "aislado (prueba de escritorio, utilidad suelta) y no código que corra dentro del flujo "
            "principal del sistema — confírmalo con las herramientas antes de asumirlo, pero es la señal "
            "inicial."
        )

    lines = [f"Contexto del proyecto para `{rel}` (de {len(graph['adj_includes'])} archivos indexados):"]
    if included_by:
        lines.append(f"- Incluido por {len(included_by)} archivo(s): {', '.join(included_by[:5])}"
                      + (", ..." if len(included_by) > 5 else ""))
    else:
        lines.append("- Ningún otro archivo lo incluye directamente.")
    if includes:
        lines.append(f"- Incluye a {len(includes)} archivo(s): {', '.join(includes[:5])}"
                      + (", ..." if len(includes) > 5 else ""))
    if tables:
        lines.append(f"- Tablas referenciadas: {', '.join(tables)}")

    return "\n".join(lines)
