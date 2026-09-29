"""
Cliente ligero del agente de QA — corre en la máquina de CADA usuario.

Diseño (multi-tenant, Sep 2026): el servidor central YA NO lee filesystem de
nadie. Este módulo es el que sí lo hace — expone las mismas operaciones que
antes vivían embebidas en agent_loop.py (listar_funciones, leer_funcion,
verificar_sintaxis, obtener_impacto), pero apuntando SIEMPRE al disco de
quien lo ejecuta. El servidor solo orquesta el LLM y, cuando el agente pide
una herramienta, le regresa la petición a este cliente para que la resuelva
localmente y envíe de vuelta el resultado — nunca al revés.

Nada aquí se conecta a Ollama ni al servidor: eso vive en client_runner.py.
Este archivo es puro I/O local, reutilizando tal cual la lógica ya probada
de dependency_graph.py y code_segmentation.py (sin cambios de comportamiento,
solo sin depender de `settings` global del backend).
"""
import os
import re
import sys
import subprocess

# El cliente reutiliza dependency_graph.py y code_segmentation.py del
# backend TAL CUAL (son puro I/O local + regex, ya sin ninguna dependencia
# de `settings` global) en vez de duplicarlos aquí. Duplicar el archivo
# significaría mantener dos copias sincronizadas a mano; importar desde la
# ruta del backend evita eso a costa de este único sys.path.append.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from dependency_graph import build_dependency_graph, get_table_impact
from code_segmentation import segment_php_code


class LocalAgentClient:
    """Encapsula el directorio del proyecto de ESTE usuario. Reemplaza el
    `settings.php_dir` global del backend viejo: aquí es una instancia, no
    un módulo compartido, para que cada usuario tenga el suyo sin pisar el
    de otro (importante en cuanto el cliente corra como servicio persistente
    y no solo como script de una sola corrida)."""

    def __init__(self, project_dir: str):
        self.project_dir = os.path.realpath(project_dir)
        if not os.path.isdir(self.project_dir):
            raise ValueError(f"El directorio del proyecto no existe: {project_dir}")
        self._graph_cache = None

    def _graph(self, force: bool = False):
        if self._graph_cache is None or force:
            self._graph_cache = build_dependency_graph(self.project_dir)
        return self._graph_cache

    def _ensure_within_project(self, filepath: str) -> str:
        real_target = os.path.realpath(
            os.path.join(self.project_dir, filepath) if not os.path.isabs(filepath) else filepath
        )
        real_base = self.project_dir
        try:
            common = os.path.commonpath([real_target, real_base])
        except ValueError:
            raise ValueError(f"'{filepath}' está fuera del proyecto local.")
        if common != real_base:
            raise ValueError(f"'{filepath}' está fuera del proyecto local ({real_base}).")
        return real_target

    def to_rel(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self.project_dir).replace("\\", "/")

    def leer_archivo(self, filepath: str) -> dict:
        """Manda el contenido del archivo objetivo al servidor al iniciar un
        análisis — el servidor nunca ve la ruta real, solo rel_path+content."""
        try:
            real_path = self._ensure_within_project(filepath)
        except ValueError as e:
            return {"error": str(e)}
        if not os.path.exists(real_path):
            return {"error": f"El archivo no existe: {filepath}"}
        try:
            with open(real_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return {"error": f"No se pudo leer el archivo: {e}"}
        return {"rel_path": self.to_rel(real_path), "content": content}

    def listar_funciones(self, filepath: str) -> dict:
        res = self.leer_archivo(filepath)
        if "error" in res:
            return res
        blocks = segment_php_code(res["content"])
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

    def leer_funcion(self, filepath: str, nombre_funcion: str) -> dict:
        res = self.leer_archivo(filepath)
        if "error" in res:
            return res
        blocks = segment_php_code(res["content"])
        for b in blocks:
            if b["name"] == nombre_funcion:
                return {"codigo": b["code"], "linea_inicio": b["start_line"], "linea_fin": b["end_line"]}
        return {
            "error": f"No se encontró una función llamada '{nombre_funcion}' en este archivo. "
                     "Usa listar_funciones para ver los nombres disponibles."
        }

    def verificar_sintaxis(self, filepath: str) -> dict:
        try:
            real_path = self._ensure_within_project(filepath)
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

    def buscar_definicion_funcion(self, nombre_funcion: str) -> dict:
        pattern = re.compile(r'function\s+' + re.escape(nombre_funcion) + r'\s*\(')
        matches = []
        for root, dirs, files in os.walk(self.project_dir):
            dirs[:] = [d for d in dirs if d not in ('vendor', '.git', 'node_modules', 'uploads_equipos', 'archivos')]
            for file in files:
                if not file.endswith('.php'):
                    continue
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        for lineno, line in enumerate(f, 1):
                            if pattern.search(line):
                                matches.append({"archivo": self.to_rel(full_path), "linea": lineno})
                except Exception:
                    continue
        if not matches:
            return {"encontrada": False, "mensaje": f"No se encontró ninguna definición de '{nombre_funcion}' en el proyecto."}
        return {"encontrada": True, "ubicaciones": matches}

    def obtener_impacto(self, filepath: str) -> dict:
        try:
            real_path = self._ensure_within_project(filepath)
        except ValueError as e:
            return {"error": str(e)}
        adj_includes, adj_included_by, file_tables, _ = self._graph()
        if real_path not in adj_includes:
            return {"error": "El archivo no está dentro del proyecto local o no fue indexado todavía."}
        my_tables, related = get_table_impact(real_path, file_tables)
        return {
            "incluye_a": [self.to_rel(p) for p in adj_includes.get(real_path, [])],
            "incluido_por": [self.to_rel(p) for p in adj_included_by.get(real_path, [])],
            "tablas_usadas": sorted(my_tables),
            "archivos_que_comparten_tabla": {tbl: [self.to_rel(p) for p in files] for tbl, files in related.items()}
        }

    def resumen_contexto_proyecto(self, filepath: str) -> str:
        """Equivalente local de project_graph_cache.get_file_context_summary
        del servidor viejo — calculado en la máquina del usuario, para
        inyectarlo al iniciar la sesión con el servidor sin que el servidor
        jamás tenga que tocar disco para obtenerlo."""
        try:
            real_path = self._ensure_within_project(filepath)
        except ValueError:
            return ""
        adj_includes, adj_included_by, file_tables, _ = self._graph()
        rel = self.to_rel(real_path)
        included_by = [self.to_rel(p) for p in adj_included_by.get(real_path, [])]
        includes = [self.to_rel(p) for p in adj_includes.get(real_path, [])]
        tables = sorted(file_tables.get(real_path, []))

        if not includes and not included_by and not tables:
            return (
                f"Contexto del proyecto: `{rel}` no tiene includes/requires detectados, nadie más lo "
                "incluye, y no referencia ninguna tabla conocida. Probablemente es un script aislado."
            )
        lines = [f"Contexto del proyecto para `{rel}`:"]
        lines.append(f"- Incluido por: {', '.join(included_by) if included_by else 'nadie'}")
        if includes:
            lines.append(f"- Incluye a: {', '.join(includes)}")
        if tables:
            lines.append(f"- Tablas referenciadas: {', '.join(tables)}")
        return "\n".join(lines)


TOOL_DISPATCH_LOCAL = {
    "listar_funciones": lambda client, args: client.listar_funciones(args.get("filepath", "")),
    "leer_funcion": lambda client, args: client.leer_funcion(args.get("filepath", ""), args.get("nombre_funcion", "")),
    "verificar_sintaxis": lambda client, args: client.verificar_sintaxis(args.get("filepath", "")),
    "buscar_definicion_funcion": lambda client, args: client.buscar_definicion_funcion(args.get("nombre_funcion", "")),
    "obtener_impacto": lambda client, args: client.obtener_impacto(args.get("filepath", "")),
    # consultar_schema_tabla se queda del lado servidor a propósito: hoy es
    # contexto compartido de un proyecto específico (CAPA 8), no algo que
    # dependa del filesystem del usuario. Si en el futuro cada usuario
    # quiere su propio schema, se vuelve una entrada más aquí.
}


def dispatch_local_tool(client: "LocalAgentClient", name: str, args: dict) -> dict:
    handler = TOOL_DISPATCH_LOCAL.get(name)
    if handler is None:
        return {"error": f"Herramienta '{name}' no se resuelve localmente (puede ser del servidor)."}
    try:
        return handler(client, args or {})
    except Exception as e:
        return {"error": f"Error ejecutando '{name}' localmente: {e}"}
