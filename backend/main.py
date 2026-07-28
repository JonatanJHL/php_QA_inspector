import os
import re
import subprocess
import json
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from danger_flags import analyze_danger_flags_content, analyze_danger_flags
from config import settings, get_local_ip
from qa_history import save_desktop_test_result
from dependency_graph import build_dependency_graph, get_impact_set, get_table_impact
from agent_loop import run_agent_analysis
from desktop_test import run_desktop_test_stream
from multi_agent import run_multi_file_analysis

app = FastAPI(title="PHP QA Orchestrator API")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic schemas for requests
class ConfigUpdateRequest(BaseModel):
    php_dir: str
    ollama_url: str
    llm_provider: Optional[str] = None
    nvidia_model: Optional[str] = None

class FileContentRequest(BaseModel):
    filepath: str

class DesktopTestRequest(BaseModel):
    filepath: str
    model: str
    provider: str = "ollama"  # "ollama" | "nvidia" — solo lo usa /api/qa/agent-test

class MultiAgentTestRequest(BaseModel):
    filepaths: list[str]
    model: str
    provider: str = "nvidia"

# Endpoints
@app.get("/api/config")
def get_config():
    return {
        "php_dir": settings.php_dir,
        "ollama_url": settings.ollama_url,
        "default_model": settings.default_model,
        "exists": os.path.exists(settings.php_dir),
        "local_ip": get_local_ip(),
        "llm_provider": settings.llm_provider,
        "nvidia_model": settings.nvidia_model,
        # Solo un booleano — nunca la key en si.
        "nvidia_api_key_configured": bool(os.environ.get("NVIDIA_API_KEY"))
    }

@app.post("/api/config")
def update_config(req: ConfigUpdateRequest):
    if not os.path.exists(req.php_dir):
        raise HTTPException(status_code=400, detail=f"El directorio especificado no existe en el sistema local: {req.php_dir}")
    settings.php_dir = req.php_dir
    settings.ollama_url = req.ollama_url
    if req.llm_provider is not None:
        settings.llm_provider = req.llm_provider
    if req.nvidia_model is not None:
        settings.nvidia_model = req.nvidia_model
    return {"status": "success", "config": get_config()}

@app.get("/api/files")
def list_files():
    if not os.path.exists(settings.php_dir):
        raise HTTPException(status_code=400, detail="El directorio de PHP configurado no existe.")
    
    php_files = []
    allowed_exts = ('.php', '.js', '.tpl', '.html', '.css', '.sql', '.json')
    for root, dirs, files in os.walk(settings.php_dir):
        # Exclude directories like vendor, .git, node_modules to keep it fast
        dirs[:] = [d for d in dirs if d not in ('vendor', '.git', 'node_modules', 'uploads_equipos', 'archivos')]
        for file in files:
            if file.endswith(allowed_exts):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, settings.php_dir)
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    size = 0
                php_files.append({
                    "name": file,
                    "rel_path": rel_path.replace("\\", "/"),
                    "full_path": full_path,
                    "size": size
                })
    return {"files": php_files}

@app.post("/api/file/content")
def get_file_content(req: FileContentRequest):
    # Security check: ensure the file is genuinely contained within
    # settings.php_dir using os.path.commonpath (not the previous startswith
    # on a raw string, which a sibling directory sharing a name prefix could
    # pass incorrectly — e.g. "...SistemaColaboradores" vs
    # "...SistemaColaboradoresBACKUP"). Also removes the old blanket
    # exception that allowed reading ANY file under the user's whole Desktop
    # folder regardless of php_dir — that was far broader than intended and,
    # combined with this endpoint having no authentication, a real
    # information-disclosure risk (confirmed real: this project has been
    # exposed via ngrok before, see qa_inspect.sh).
    real_target = os.path.realpath(req.filepath)
    real_base = os.path.realpath(settings.php_dir)
    try:
        contained = os.path.commonpath([real_target, real_base]) == real_base
    except ValueError:
        # e.g. different drives on Windows
        contained = False
    if not contained:
        raise HTTPException(status_code=403, detail="Acceso denegado: el archivo está fuera del directorio configurado.")

    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="El archivo no existe.")
        
    try:
        with open(req.filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"content": content, "filepath": req.filepath}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo el archivo: {str(e)}")

@app.post("/api/qa/syntax")
def check_syntax(req: FileContentRequest):
    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="El archivo no existe.")
    
    ext = os.path.splitext(req.filepath)[1].lower()
    
    try:
        if ext == '.php':
            result = subprocess.run(
                ["php", "-l", req.filepath],
                capture_output=True,
                text=True
            )
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            is_ok = result.returncode == 0
            error_msg = stdout or stderr
            error_line = None
            if not is_ok:
                import re
                match = re.search(r"on line (\d+)", error_msg)
                if match:
                    error_line = int(match.group(1))
            return {"success": is_ok, "output": error_msg, "error_line": error_line}

        elif ext == '.js':
            result = subprocess.run(
                ["node", "--check", req.filepath],
                capture_output=True,
                text=True
            )
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            is_ok = result.returncode == 0
            error_msg = stdout or stderr
            error_line = None
            if not is_ok:
                import re
                match = re.search(r":(\d+)\r?\n", error_msg)
                if match:
                    error_line = int(match.group(1))
                else:
                    match = re.search(r"line (\d+)", error_msg, re.IGNORECASE)
                    if match:
                        error_line = int(match.group(1))
            return {"success": is_ok, "output": error_msg or "No se detectaron errores de sintaxis en JavaScript.", "error_line": error_line}
            
        elif ext == '.json':
            try:
                with open(req.filepath, "r", encoding="utf-8") as f:
                    json.load(f)
                return {"success": True, "output": "Archivo JSON válido.", "error_line": None}
            except json.JSONDecodeError as jde:
                return {
                    "success": False,
                    "output": f"Error de parseo JSON: {jde.msg} en la línea {jde.lineno}, columna {jde.colno}.",
                    "error_line": jde.lineno
                }
                
        else:
            return {
                "success": True,
                "output": f"La validación sintáctica básica no está configurada o no es requerida para archivos {ext}.\nProcediendo a auditoría lógica del Agente.",
                "error_line": None
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error ejecutando linter: {str(e)}")

class InspectContentRequest(BaseModel):
    filename: str          # just the basename, e.g. "mi_hook.php"
    content: str           # full raw file content as string
    server_path: Optional[str] = None  # optional: remote path for display only

@app.post("/api/qa/inspect-content")
def inspect_content(req: InspectContentRequest):
    """
    Inspect a file by its RAW CONTENT instead of a local path.
    Intended for remote SSH clients that read the file and pipe it here.
    Runs: syntax check (via temp file) + danger flag analysis.
    Does NOT run dependency graph (requires local file system context).
    """
    import tempfile
    
    ext = os.path.splitext(req.filename)[1].lower()
    content = req.content
    display_path = req.server_path or req.filename
    
    result = {
        "filename": req.filename,
        "server_path": display_path,
        "ext": ext,
        "syntax_ok": True,
        "syntax_output": "",
        "syntax_error_line": None,
        "danger_flags": [],
        "danger_score": 0,
        "risk_index": 0,
        "risk_level": "Bajo",
        "verdict": "APROBADO"
    }
    
    # --- 1. Syntax check via temp file ---
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix=ext, delete=False,
                                         encoding='utf-8', errors='replace') as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        
        if ext == '.php':
            proc = subprocess.run(["php", "-l", tmp_path],
                                  capture_output=True, text=True)
            out = proc.stdout.strip() or proc.stderr.strip()
            result["syntax_ok"] = proc.returncode == 0
            result["syntax_output"] = out.replace(tmp_path, req.filename)
            if not result["syntax_ok"]:
                m = re.search(r"on line (\d+)", out)
                if m:
                    result["syntax_error_line"] = int(m.group(1))

        elif ext == '.js':
            proc = subprocess.run(["node", "--check", tmp_path],
                                  capture_output=True, text=True)
            out = proc.stdout.strip() or proc.stderr.strip()
            result["syntax_ok"] = proc.returncode == 0
            result["syntax_output"] = out.replace(tmp_path, req.filename)
            if not result["syntax_ok"]:
                m = re.search(r":(\d+)\r?\n", out) or re.search(r"line (\d+)", out, re.IGNORECASE)
                if m:
                    result["syntax_error_line"] = int(m.group(1))
                    
        elif ext == '.json':
            try:
                json.loads(content)
                result["syntax_output"] = "Archivo JSON válido."
            except json.JSONDecodeError as jde:
                result["syntax_ok"] = False
                result["syntax_output"] = f"Error JSON: {jde.msg} en línea {jde.lineno}, columna {jde.colno}."
                result["syntax_error_line"] = jde.lineno
        else:
            result["syntax_output"] = f"Sin validador de sintaxis para {ext}."
            
        os.unlink(tmp_path)
    except Exception as e:
        result["syntax_output"] = f"Error en linter: {str(e)}"

    # --- 2. Danger flag analysis on raw content (shared detector, same rules
    #        used by the local /api/qa/impact endpoint) ---
    flags, danger_score = analyze_danger_flags_content(content, ext)
    result["danger_flags"] = flags
    result["danger_score"] = danger_score

    # --- 3. Compute final risk ---
    impact_score = 10  # No graph available for remote content
    risk_index = int((impact_score + danger_score) / 2)
    has_hardcoded_creds = any("CREDENCIAL HARDCODEADA" in f for f in flags)
    if has_hardcoded_creds:
        # Credentials leaks are a high-severity finding on their own: guarantee
        # at least an ALTO risk floor even if the rest of the file looks clean.
        risk_index = max(risk_index, 65)
    if not result["syntax_ok"]:
        risk_index = 100
        flags.append("ERROR DE SINTAXIS ACTIVO: El archivo colapsará inmediatamente en ejecución síncrona en producción.")

    result["risk_index"] = risk_index
    if risk_index >= 90:
        result["risk_level"] = "CRÍTICO"
        result["verdict"] = "RECHAZADO"
    elif risk_index >= 65:
        result["risk_level"] = "ALTO"
        result["verdict"] = "REQUIERE REVISION INMEDIATA"
    elif risk_index >= 30:
        result["risk_level"] = "MEDIO"
        result["verdict"] = "REVISAR CON CUIDADO"
    else:
        result["risk_level"] = "BAJO"
        result["verdict"] = "APROBADO"
        
    if not result["syntax_ok"] and result["verdict"] == "APROBADO":
        result["verdict"] = "RECHAZADO"

    return result

@app.get("/api/ollama/models")
async def get_ollama_models():
    url = f"{settings.ollama_url}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                models = [model["name"] for model in data.get("models", [])]
                return {"models": models}
            else:
                return {"models": [], "warning": f"Ollama respondió con código {response.status_code}"}
    except Exception as e:
        return {"models": [], "warning": f"No se pudo conectar a Ollama en {settings.ollama_url}. ¿Está encendido? ({str(e)})"}


@app.post("/api/qa/desktop-test")
async def run_desktop_test(req: DesktopTestRequest):
    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="El archivo no existe.")
    return StreamingResponse(run_desktop_test_stream(req.filepath, req.model), media_type="text/plain")


@app.post("/api/qa/agent-test")
async def run_agent_test(req: DesktopTestRequest):
    """Loop de agente secuencial con tool-calling (ver agent_loop.py) — a
    diferencia de /api/qa/desktop-test (pipeline fijo por bloques), aquí el
    modelo decide qué información pedir en vez de recibirla precocinada.
    Mismo request shape y mismo protocolo de streaming (__HB__/__GATE__) que
    desktop-test, así que el frontend puede reusar el mismo parser."""
    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="El archivo no existe.")

    try:
        with open(req.filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo leer el archivo: {str(e)}")

    async def event_generator():
        accumulated = []
        try:
            async for chunk in run_agent_analysis(req.filepath, req.model, provider=req.provider):
                accumulated.append(chunk)
                yield chunk
            full_text = "".join(accumulated)
            if full_text.strip():
                save_desktop_test_result(req.filepath, req.model, code, full_text)
        except Exception as e:
            yield f"Error inesperado durante el análisis del agente: {str(e)}"

    return StreamingResponse(event_generator(), media_type="text/plain")


@app.post("/api/qa/multi-agent-test")
async def run_multi_agent_test(req: MultiAgentTestRequest):
    """Orquestador multi-agente real (ver multi_agent.py): varios archivos
    analizados EN PARALELO + un agente coordinador que sintetiza el impacto
    cruzado entre ellos. Solo tiene sentido de verdad con provider="nvidia"
    (concurrencia real); con Ollama local, los agentes se siguen sirviendo
    uno a la vez de todos modos. Mismo protocolo __HB__/__GATE__."""
    missing = [fp for fp in req.filepaths if not os.path.exists(fp)]
    if missing:
        raise HTTPException(status_code=404, detail=f"Archivo(s) no encontrado(s): {', '.join(missing)}")

    async def event_generator():
        try:
            async for chunk in run_multi_file_analysis(req.filepaths, req.model, provider=req.provider):
                yield chunk
        except Exception as e:
            yield f"Error inesperado durante el análisis multi-agente: {str(e)}"

    return StreamingResponse(event_generator(), media_type="text/plain")


class ImpactAnalysisRequest(BaseModel):
    filepath: str

@app.post("/api/qa/impact")
def get_impact_analysis(req: ImpactAnalysisRequest):
    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="El archivo no existe.")

    adj_includes, adj_included_by, file_tables, ambiguous_basenames = build_dependency_graph(settings.php_dir)
    rel_filepath = os.path.relpath(req.filepath, settings.php_dir).replace("\\", "/")

    def to_rel(p):
        return os.path.relpath(p, settings.php_dir).replace("\\", "/")

    # 1. Direct dependencies (includes)
    includes_raw = adj_includes.get(req.filepath, [])
    includes = [to_rel(p) for p in includes_raw]
    
    # 2. Direct parents
    included_by_direct_raw = adj_included_by.get(req.filepath, [])
    included_by_direct = [to_rel(p) for p in included_by_direct_raw]
    
    # 3. Transitive impact set
    impact_set_raw = get_impact_set(req.filepath, adj_included_by)
    impact_set = [to_rel(p) for p in impact_set_raw]

    # 4. Table-level impact: files that share a DB table with this one, even
    #    with zero include/require relationship (e.g. bot.php and another
    #    script both writing to modulos.tblColaborador).
    my_tables, related_by_table = get_table_impact(req.filepath, file_tables)
    table_impact = [
        {"table": tbl, "files": sorted(set(to_rel(p) for p in files))}
        for tbl, files in sorted(related_by_table.items())
    ]
    table_impact_file_count = len({p for files in related_by_table.values() for p in files})

    # 5. Graph data for the visual node-edge diagram in the UI. Kept small and
    #    pre-shaped so the frontend can render without doing its own graph math:
    #    center node = this file, includes/included_by as direct edges, plus
    #    "table" pseudo-nodes for any shared-table relationships.
    graph_nodes = [{"id": rel_filepath, "type": "self"}]
    graph_edges = []
    for p in includes_raw:
        graph_nodes.append({"id": to_rel(p), "type": "include"})
        graph_edges.append({"from": rel_filepath, "to": to_rel(p), "kind": "include"})
    for p in included_by_direct_raw:
        graph_nodes.append({"id": to_rel(p), "type": "included_by"})
        graph_edges.append({"from": to_rel(p), "to": rel_filepath, "kind": "include"})
    for tbl, files in related_by_table.items():
        table_node_id = f"table:{tbl}"
        graph_nodes.append({"id": table_node_id, "type": "table", "label": tbl})
        graph_edges.append({"from": rel_filepath, "to": table_node_id, "kind": "table"})
        for p in files:
            fp_rel = to_rel(p)
            graph_nodes.append({"id": fp_rel, "type": "table_sibling"})
            graph_edges.append({"from": table_node_id, "to": fp_rel, "kind": "table"})
    # De-duplicate nodes by id, keeping the first (most specific) type seen
    seen_ids = {}
    for n in graph_nodes:
        if n["id"] not in seen_ids:
            seen_ids[n["id"]] = n
    graph_nodes = list(seen_ids.values())

    # Calculate Impact Factor. Combine file-graph impact with table-sharing
    # impact: a file with zero includes but that writes to a heavily-shared
    # table is not actually "isolated" — the old logic missed this entirely.
    total_files = len(adj_includes)
    combined_impact_count = len(set(impact_set_raw) | {p for files in related_by_table.values() for p in files})
    impact_percentage = (combined_impact_count / total_files * 100) if total_files > 0 else 0

    if combined_impact_count == 0:
        impact_level = "Aislado (Bajo)"
        impact_score = 10
    elif combined_impact_count <= 3:
        impact_level = "Focalizado (Medio)"
        impact_score = 35
    elif impact_percentage < 30:
        impact_level = "Amplio (Alto)"
        impact_score = 70
    else:
        impact_level = "Crítico / Global"
        impact_score = 95

    if ambiguous_basenames:
        # Surfaced as a caveat rather than a danger flag: this affects graph
        # *completeness*, not code safety, but the user should know the
        # include-resolution had to guess between same-named files.
        pass  # exposed via the `ambiguous_includes` field below

    danger_flags, danger_score = analyze_danger_flags(req.filepath)
    
    # Check syntax
    syntax_ok = True
    ext = os.path.splitext(req.filepath)[1].lower()
    if ext in ('.php', '.js', '.json'):
        try:
            s_result = check_syntax(FileContentRequest(filepath=req.filepath))
            syntax_ok = s_result.get("success", True)
        except Exception:
            pass
            
    risk_index = int((impact_score + danger_score) / 2)
    has_hardcoded_creds = any("CREDENCIAL HARDCODEADA" in f for f in danger_flags)
    if has_hardcoded_creds:
        # Credentials leaks are a high-severity finding on their own: guarantee
        # at least an ALTO risk floor even if impact/danger otherwise look low.
        risk_index = max(risk_index, 65)
    if not syntax_ok:
        risk_index = 100
        danger_flags.append("ERROR DE SINTAXIS ACTIVO: El archivo colapsará inmediatamente si es requerido en ejecución síncrona en producción.")

    return {
        "filepath": rel_filepath,
        "includes": includes,
        "included_by_direct": included_by_direct,
        "impact_set": impact_set,
        "impact_level": impact_level,
        "impact_score": impact_score,
        "danger_flags": danger_flags,
        "danger_score": danger_score,
        "risk_index": risk_index,
        "syntax_ok": syntax_ok,
        # New: table-sharing impact (files that write/read the same DB tables
        # even with zero include/require relationship).
        "tables_used": sorted(my_tables),
        "table_impact": table_impact,
        "table_impact_file_count": table_impact_file_count,
        # New: same-name-different-folder includes the resolver had to guess on.
        "ambiguous_includes": sorted(ambiguous_basenames),
        # New: pre-shaped graph data for the visual node-edge diagram.
        "graph": {"nodes": graph_nodes, "edges": graph_edges}
    }

# Mount frontend static files
# Make sure this is mounted last so api routes are matched first
frontend_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
else:
    # If starting for first time and frontend folder not built yet, we don't crash
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
