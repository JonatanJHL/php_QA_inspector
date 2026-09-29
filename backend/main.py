import os
import re
import subprocess
import json
import httpx
import socket
import asyncio
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from danger_flags import analyze_danger_flags_content, analyze_danger_flags
from sarif_export import build_sarif_for_file
from test_matrix import parse_test_matrix, classify_severity
from code_segmentation import segment_php_code, LOOP_ENGINEERING_THRESHOLD_CHARS
from config import settings, get_local_ip, DEFAULT_NUM_CTX
from schema_context import build_schema_context
from qa_history import load_desktop_test_history, save_desktop_test_result
from dependency_graph import build_dependency_graph, get_table_impact
from project_graph_cache import get_project_graph
from ollama_client import call_ollama_chat
from agent_loop import run_agent_analysis
from agent_session import new_session_state, advance_session

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

class FileContentRequest(BaseModel):
    filepath: str

class DesktopTestRequest(BaseModel):
    filepath: str
    model: str

# Endpoints
@app.get("/api/config")
def get_config():
    return {
        "php_dir": settings.php_dir,
        "ollama_url": settings.ollama_url,
        "default_model": settings.default_model,
        "exists": os.path.exists(settings.php_dir),
        "local_ip": get_local_ip()
    }

@app.post("/api/config")
def update_config(req: ConfigUpdateRequest):
    if not os.path.exists(req.php_dir):
        raise HTTPException(status_code=400, detail=f"El directorio especificado no existe en el sistema local: {req.php_dir}")
    settings.php_dir = req.php_dir
    settings.ollama_url = req.ollama_url
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
    # Security check: Ensure file is inside the configured directory
    real_target = os.path.realpath(req.filepath)
    real_base = os.path.realpath(settings.php_dir)
    
    # We allow loading if it is in the backup directory or workspace for convenience,
    # but let's log or check it.
    if not real_target.startswith(real_base) and not req.filepath.startswith("c:\\Users\\jonat\\OneDrive\\Escritorio"):
         raise HTTPException(status_code=403, detail="Acceso denegado: El archivo está fuera de las rutas permitidas.")

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


def extract_block_summary(text: str, max_chars: int = 500) -> str:
    """Compact a single block's raw analyze_block response for running_summary.
    analyze_block's prompt asks for 4 numbered items (qué hace / variables /
    riesgo que rompe la ejecución / diagrama mermaid); a blind head-truncation
    at a few hundred chars only ever kept item 1 (and part of 2), silently
    dropping item 3 — the risk/breaking-case content the consolidation call
    and the final test matrix most depend on. This extracts item 3 explicitly
    so it survives truncation even when the block's full response is long."""
    risk_match = re.search(r'(?:^|\n)\s*3[\.\)]\s*(.*?)(?=\n\s*4[\.\)]|\Z)', text, re.DOTALL)
    if not risk_match:
        return text[:max_chars]
    risk_text = risk_match.group(1).strip()
    head = text[:150].strip()
    return f"{head} [...] Riesgo: {risk_text}"[:max_chars]


@app.post("/api/qa/desktop-test")
async def run_desktop_test(req: DesktopTestRequest):
    if not os.path.exists(req.filepath):
        raise HTTPException(status_code=404, detail="El archivo no existe.")
    
    try:
        with open(req.filepath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo leer el archivo: {str(e)}")
    
    ext = os.path.splitext(req.filepath)[1].lower()
    
    if ext == '.js':
        role_description = "Eres un Agente Ingeniero de QA y Especialista en Pruebas de Escritorio para JavaScript y Node.js."
        instructions = (
            "Tu tarea es analizar el código JavaScript provisto y simular paso a paso su ejecución (prueba de escritorio) de forma lógica.\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Componente\n"
            "Explica brevemente qué hace este script, qué parámetros recibe (argumentos, variables de entorno, peticiones, etc.) y qué librerías o dependencias tiene.\n\n"
            "### 🔍 Simulación paso a paso (Prueba de Escritorio)\n"
            "Simula un flujo de ejecución típico. Describe cronológicamente qué pasa línea por línea o función por función, explicando el comportamiento lógico, callbacks, promesas o flujo asíncrono.\n\n"
            "### 📊 Tabla de Variables e Hilos de Estado\n"
            "Muestra una tabla de Markdown que rastree los valores y tipos de las variables clave en los puntos críticos de la ejecución.\n\n"
            "### ⚠️ Puntos Críticos y Errores Lógicos\n"
            "Identifica posibles fallos: variables no declaradas (global scope leaks), llamadas asíncronas no manejadas (unhandled promise rejections), callbacks faltantes, comparación insegura (`==` vs `===`), o variables undefined/null.\n\n"
            "### 🧪 Casos de Prueba Recomendados\n"
            "Sugiere de 2 a 3 escenarios de prueba concretos detallando entradas y comportamiento esperado."
        )
    elif ext == '.tpl':
        role_description = "Eres un Agente Ingeniero de QA y Especialista en Plantillas y Vistas (Templates) para Smarty/HTML."
        instructions = (
            "Tu tarea es analizar la plantilla de Smarty/Template provista y verificar su lógica y mapeo de variables.\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Template\n"
            "Explica brevemente qué sección de la interfaz representa esta plantilla y qué variables dinámicas de PHP espera recibir para renderizar.\n\n"
            "### 🔍 Simulación paso a paso (Flujo de Renderizado)\n"
            "Simula el renderizado de la plantilla. Explica cómo se procesan las estructuras condicionales (ej. `{if}`, `{else}`) y los bucles (ej. `{foreach}`, `{section}`).\n\n"
            "### ⚠️ Variables Requeridas del Controlador\n"
            "Lista todas las variables del template que provienen de la lógica de negocio (PHP) para que el desarrollador pueda validar que se están pasando correctamente.\n\n"
            "### ⚠️ Errores Lógicos y Estructurales\n"
            "Identifica posibles bugs: tags sin cerrar, llamadas a variables inexistentes, lógica condicional contradictoria o problemas con el escapado de datos (riesgos de XSS en layouts).\n\n"
            "### 🧪 Casos de Prueba Recomendados\n"
            "Sugiere escenarios de prueba (ej. arrays vacíos para los foreach, booleanos falsos en condicionales) para probar cómo reacciona la vista."
        )
    elif ext == '.sql':
        role_description = "Eres un Agente de Base de Datos y Especialista en QA SQL."
        instructions = (
            "Tu tarea es analizar el script de base de datos SQL provisto (DDL, DML, triggers, procedimientos, etc.).\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Script SQL\n"
            "Explica qué tablas se crean/modifican, qué relaciones tienen o qué datos se insertan.\n\n"
            "### 🔍 Análisis de Relaciones y Claves\n"
            "Detalla la estructura de claves primarias, claves foráneas, índices, restricciones y tipos de datos.\n\n"
            "### ⚠️ Puntos Críticos y Riesgos del Script\n"
            "Identifica riesgos: claves foráneas sin cascada o restricciones, tipos de datos inadecuados, falta de índices en columnas de búsqueda frecuentes, riesgo de pérdida de datos por ALTER TABLE destructivos o falta de transacciones.\n\n"
            "### 🧪 Pruebas de Inserción / Casos Límite\n"
            "Sugiere escenarios de inserción (ej. valores nulos, valores duplicados en campos únicos, desbordamientos de longitud) y qué restricciones deberían activarse."
        )
    else:
        # Default PHP template with SQL Mock logic
        role_description = "Eres un Agente Ingeniero de QA y Especialista en Pruebas de Escritorio para PHP."
        instructions = (
            "Tu tarea es analizar el código PHP provisto y simular paso a paso su ejecución (prueba de escritorio) de forma lógica, sin ejecutarlo realmente en un servidor.\n"
            "Debes estructurar tu respuesta en ESPAÑOL usando Markdown estricto y limpio, que contenga las siguientes secciones:\n\n"
            "### 📋 Resumen del Componente\n"
            "Explica brevemente qué hace este script, qué parámetros recibe (GET, POST, $_SESSION, etc.) y qué dependencias tiene (includes, base de datos).\n\n"
            "### 🗄️ Simulación de Base de Datos y Datos Mock\n"
            "Si el código contiene consultas SQL (SELECT, INSERT, UPDATE, etc.):\n"
            "1. Identifica y extrae las consultas SQL detectadas.\n"
            "2. Genera una lista de valores simulados (Mock Data) de 1 o 2 filas, estructuradas con las columnas del SELECT.\n"
            "3. Emula cómo el script PHP recuperará estos registros (por ejemplo, en bucles `while` o mapeos de arrays) y explica qué variables se poblarán y cómo.\n\n"
            "### 🔍 Simulación paso a paso (Prueba de Escritorio)\n"
            "Simula un flujo de ejecución típico utilizando los datos de entrada o los registros mock generados arriba. Describe cronológicamente qué pasa línea por línea o sección por sección, explicando cómo cambia el flujo lógico.\n\n"
            "### 📊 Tabla de Variables e Hilos de Estado\n"
            "Muestra una tabla de Markdown que rastree los valores teóricos de las variables más importantes en los puntos clave de la simulación.\n"
            "Ejemplo de formato:\n"
            "| Variable | Momento / Línea | Tipo | Valor Estimado / Estado | Descripción |\n"
            "| --- | --- | --- | --- | --- |\n\n"
            "### ⚠️ Puntos Críticos y Errores Lógicos\n"
            "Identifica posibles bugs que no detecta el compilador de sintaxis simple. Ejemplos:\n"
            "- Variables que podrían estar indefinidas bajo ciertas condiciones.\n"
            "- Falta de validación en parámetros de entrada (riesgos de SQL Injection o XSS en PHP puro).\n"
            "- Errores de lógica en bucles o condiciones.\n"
            "- Dependencias de variables globales (`$dao`, `$conn`, `$_SESSION`) no verificadas antes de su uso.\n\n"
            "### 🧪 Matriz de Casos de Prueba (Pasa / Falla)\n"
            "Esta es la sección MAS IMPORTANTE. No basta con describir escenarios en prosa: construye una TABLA "
            "de Markdown con AL MENOS 5 filas que cubra estos tres tipos de caso, mezclados:\n"
            "1. **Caso feliz** (mínimo 1): entrada válida típica, todo funciona como se espera.\n"
            "2. **Caso límite/borde** (mínimo 2): entrada vacía, cero, id inexistente, array vacío, string muy largo, "
            "caracteres especiales o UTF-8, valores nulos donde se esperaba un tipo concreto.\n"
            "3. **Caso que ROMPE el código intencionalmente** (mínimo 2, obligatorio): identifica una entrada "
            "específica que, dado el código real que tienes enfrente, HARÍA FALLAR el script (excepción no "
            "capturada, división por cero, acceso a índice de array inexistente, llamada a método sobre null, "
            "query SQL malformada por un valor no escapado, condición de carrera si dos requests llegan casi "
            "simultáneas, etc). No inventes fallas genéricas: señala la línea o función real del código donde "
            "ese caso específico rompería, y por qué.\n\n"
            "Formato obligatorio de la tabla:\n"
            "| # | Tipo (Feliz/Límite/Rompe) | Entrada / Escenario | Línea o función afectada | Resultado Esperado | ¿Pasa o Falla hoy? |\n"
            "| --- | --- | --- | --- | --- | --- |\n\n"
            "Después de la tabla, para cada fila marcada como 'Falla hoy', añade una sub-sección breve "
            "explicando la causa raíz y una sugerencia concreta de corrección (código o validación faltante).\n"
        )
        
    # El diagrama de flujo Mermaid ya NO se pide en este prompt principal —
    # se genera con una segunda llamada corta y dedicada (ver generate_mermaid_diagram
    # más abajo), porque Hermes3 tiende a ignorar instrucciones al final de
    # prompts largos con muchas secciones obligatorias.
    schema_context = build_schema_context(code)
    prompt_system = (
        f"{role_description}\n{instructions}"
        + (f"\n\n{schema_context}" if schema_context else "")
    )

    # --- Inyectar historial como memoria simulada ---
    # Hermes3/Ollama no recuerda nada entre llamadas por si mismo; esto le da
    # continuidad artificial mostrandole (resumido) que dijo la ultima vez
    # sobre este mismo archivo, y si el codigo cambio desde entonces.
    import hashlib
    current_code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
    history_entries = load_desktop_test_history(req.filepath, max_entries=2)

    history_context = ""
    if history_entries:
        last = history_entries[0]
        changed_note = (
            "El código NO ha cambiado desde ese análisis anterior."
            if last.get("code_hash") == current_code_hash
            else "El código SI cambió desde ese análisis anterior — verifica si los hallazgos previos ya se corrigieron."
        )
        history_context = (
            "\n\n---\n"
            "## 🧠 Contexto de análisis previos sobre este mismo archivo\n"
            f"({changed_note})\n\n"
        )
        for i, entry in enumerate(history_entries, 1):
            history_context += (
                f"**Análisis anterior #{i}** ({entry.get('timestamp', '?')}, modelo: {entry.get('model', '?')}):\n"
                f"{entry.get('summary', '')}\n\n"
            )
        history_context += (
            "Ten en cuenta lo anterior: si los mismos problemas siguen presentes, señálalo explícitamente "
            "('Este problema ya se detectó antes y sigue sin corregirse'). Si el código cambió y un problema "
            "previo ya no aplica, dilo también ('Este caso ya no falla, fue corregido').\n---\n"
        )

    prompt_user = (
        f"Realiza la prueba de escritorio para el siguiente archivo:\n"
        f"Ruta: `{req.filepath}`\n\n"
        f"Código:\n```{ext[1:]}\n{code}\n```"
        f"{history_context}"
    )

    url = f"{settings.ollama_url}/api/chat"
    payload = {
        "model": req.model,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user}
        ],
        "stream": True,
        # keep_alive="10m": evita que Ollama descarde el modelo de RAM entre
        # esta llamada y las siguientes (confirmado con `ollama ps` que el
        # default expira en ~1 minuto de inactividad). Sin esto, cada bloque
        # del loop engineering podría pagar el costo de recargar 6GB en RAM
        # antes de siquiera empezar a generar texto.
        "keep_alive": "10m",
        "options": {
            "temperature": 0.2,
            # num_ctx centralizado en config.DEFAULT_NUM_CTX. Confirmado
            # empíricamente en este equipo (M1 Pro, 16GB RAM, Ollama en GPU
            # vía Metal) que Ollama recarga el modelo completo en RAM cada
            # vez que num_ctx CAMBIA entre llamadas consecutivas (~52s de
            # penalización medida con curl directo), no por el valor en sí.
            # Por eso es crítico que TODAS las llamadas del backend usen
            # exactamente el mismo num_ctx — de ahí la constante compartida
            # en vez de números sueltos por archivo/función.
            "num_ctx": DEFAULT_NUM_CTX
        }
    }

    # --- DEBUG TEMPORAL: confirmar en consola exactamente qué se envía a Ollama ---
    print("=" * 60)
    print(f"[DEBUG desktop-test] modelo={req.model} ext={ext} len(system)={len(prompt_system)} len(user)={len(prompt_user)}")
    print(f"[DEBUG desktop-test] system_prompt (primeros 500 chars):\n{prompt_system[:500]}")
    print("=" * 60)

    async def call_ollama_with_heartbeat(prompt: str, model_name: str, label: str,
                                           timeout: float = 300.0, num_ctx: int = DEFAULT_NUM_CTX,
                                           heartbeat_interval: float = 4.0):
        """Thin wrapper around ollama_client.call_ollama_chat preserving the
        {"content": str, "error": str|None} shape this file's callers expect."""
        result = None
        async for item in call_ollama_chat(
            [{"role": "user", "content": prompt}], model_name, label,
            timeout=timeout, num_ctx=num_ctx, heartbeat_interval=heartbeat_interval
        ):
            if isinstance(item, dict):
                message = item.get("message")
                result = {"content": message.get("content", "") if message else "", "error": item.get("error")}
            else:
                yield item
        yield result

    async def generate_mermaid_diagram(code_str: str, extension: str, model_name: str) -> str:
        """Second, focused Ollama call dedicated ONLY to producing a Mermaid
        flowchart. Kept deliberately short: Hermes3 (a small local model)
        tends to drop instructions from the tail of a long, multi-section
        prompt — asking for 7 things at once meant the diagram request was
        the one most likely to get skipped. A short, single-purpose prompt
        is far more reliable for actually getting valid Mermaid syntax back.
        Non-streaming (simpler to parse a single response), with a short
        timeout since this is a small, bounded generation task.
        """
        diagram_prompt = (
            "Genera ÚNICAMENTE un diagrama de flujo en formato Mermaid (`flowchart TD`) que represente "
            "el flujo principal del siguiente código. No agregues explicación, resumen, ni texto fuera "
            "del bloque de código. Solo el bloque ```mermaid ... ```.\n\n"
            "Reglas del diagrama:\n"
            "- IDs de nodo cortos (A, B, C...), texto breve dentro de cada nodo (máximo 6 palabras).\n"
            "- Incluye puntos de entrada, decisiones condicionales (nodos de rombo), accesos a base de "
            "datos/servicios externos, y puntos de salida.\n"
            "- Si detectas un punto que claramente rompería la ejecución (excepción no capturada, "
            "división por cero, acceso a índice inexistente, etc.), márcalo con la clase `errorNode`: "
            "`classDef errorNode fill:#7f1d1d,stroke:#ef4444,color:#fff;`\n\n"
            f"Código ({extension[1:]}):\n```{extension[1:]}\n{code_str}\n```"
        )
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(
                    f"{settings.ollama_url}/api/chat",
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": diagram_prompt}],
                        "stream": False,
                        "keep_alive": "10m",
                        "options": {"temperature": 0.1, "num_ctx": DEFAULT_NUM_CTX}
                    }
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                return data.get("message", {}).get("content", "")
        except Exception:
            # The diagram is a nice-to-have on top of the main analysis; if
            # this second call fails for any reason, the main response the
            # user already saw is unaffected — just no diagram this time.
            return ""

    async def analyze_block(block: dict, accumulated_summary: str, model_name: str):
        """Analyze a single code block (one function, or a merged chunk of
        top-level code) with a short, focused prompt. Async GENERATOR: while
        Ollama is thinking (which can take minutes on CPU-only hardware),
        yields a heartbeat line every few seconds so the frontend has visible
        proof the process hasn't stalled — a single blocking await here would
        leave the stream silent for the entire duration of each block. The
        final yielded item is a dict {"text":..., "mermaid":...}; every
        earlier yielded item is a plain string heartbeat line."""
        block_label = f"función `{block['name']}`" if block['kind'] == 'function' else "código de nivel superior"
        prior_context = (
            f"Resumen de lo detectado en bloques anteriores del mismo archivo:\n{accumulated_summary}\n\n"
            "Nota: si algún bloque de arriba aparece como [NO ANALIZADO], fue por un error técnico "
            "(timeout) al procesarlo — NO es evidencia de que ese código tenga un problema, falte, o esté "
            "mal definido. No reportes una función como 'no definida' ni afirmes ningún riesgo basado "
            "únicamente en esa marca; repórtalo solo si lo ves directamente en el código de ESTE bloque.\n\n"
            if accumulated_summary.strip() else
            "Este es el primer bloque analizado de este archivo, no hay contexto previo.\n\n"
        )
        schema_ctx = build_schema_context(block["code"])
        schema_section = f"\n{schema_ctx}\n\n" if schema_ctx else ""
        block_prompt = (
            f"Estás analizando el archivo PHP `{req.filepath}` en partes, porque es demasiado grande para "
            f"analizarlo de una sola vez. Este es el bloque correspondiente a {block_label} "
            f"(líneas {block['start_line']}-{block['end_line']}).\n\n"
            f"{prior_context}"
            f"{schema_section}"
            "Para ESTE bloque únicamente, responde en español y de forma breve con:\n"
            "1. Qué hace este bloque (1-2 líneas).\n"
            "2. Variables clave que recibe, produce, o modifica.\n"
            "3. Cualquier riesgo o caso que ROMPERÍA la ejecución (sé específico: qué entrada, qué línea).\n"
            "4. Un fragmento de diagrama Mermaid (solo los nodos de ESTE bloque, con IDs que no se repitan "
            "entre bloques — usa el prefijo indicado abajo). SIEMPRE encierra el diagrama entre tres "
            "backticks y la palabra mermaid, así: ```mermaid ... ``` — nunca lo escribas sin ese cercado, "
            "aunque uses sintaxis 'graph LR' o 'graph TD' en vez de 'flowchart'.\n\n"
            f"Prefijo de IDs para este bloque: usa IDs como {block['name'] or 'TOP'}_A, {block['name'] or 'TOP'}_B, etc.\n\n"
            f"Código de este bloque:\n```php\n{block['code']}\n```"
        )

        # call_ollama_chat already retries once internally on failure (confirmed
        # necessary: this hardware's Ollama backend can crash mid-request and
        # auto-restarts within seconds), so no separate outer retry is needed here.
        result = None
        async for item in call_ollama_chat(
            [{"role": "user", "content": block_prompt}], model_name, "Analizando bloque",
            timeout=300.0, num_ctx=DEFAULT_NUM_CTX
        ):
            if isinstance(item, dict):
                message = item.get("message")
                error = item.get("error")
                if error or not message:
                    result = {
                        "text": f"(No se pudo analizar el bloque '{block['name']}': {error or 'sin respuesta'})",
                        "mermaid": "", "failed": True
                    }
                else:
                    content = message.get("content", "")
                    # Accept both ```mermaid fenced blocks AND bare "graph TD/LR"
                    # or "flowchart TD/LR" lines the model sometimes emits
                    # unfenced despite instructions, so a missed fence doesn't
                    # silently lose the diagram.
                    mermaid_match = re.search(r'```mermaid\s*\n?([\s\S]*?)```', content)
                    if mermaid_match:
                        mermaid_fragment = mermaid_match.group(1).strip()
                    else:
                        bare_match = re.search(r'((?:graph|flowchart)\s+(?:TD|LR|TB|RL)[\s\S]*?)(?:\n\n|\Z)', content)
                        mermaid_fragment = bare_match.group(1).strip() if bare_match else ""
                    result = {"text": content, "mermaid": mermaid_fragment, "failed": False}
            else:
                yield item

        yield result



    async def analyze_php_in_blocks(model_name: str):
        """Loop-engineering path for large PHP files: segment into logical
        blocks, analyze each with a short focused call (carrying forward a
        compact summary as context), then make one final consolidating call
        with all partial findings — never the raw code again — to produce
        the Resumen/Matriz/Veredicto in our standard format, plus a merged
        Mermaid diagram stitched from each block's fragment."""
        blocks = segment_php_code(code, max_block_chars=6000)
        yield f"_Archivo grande detectado ({len(code)} caracteres). Analizando en {len(blocks)} bloque(s) por separado para no saturar el contexto del modelo local..._\n\n---\n\n"

        running_summary = ""
        mermaid_fragments = []
        failed_labels = []

        for i, block in enumerate(blocks, 1):
            label = block['name'] if block['kind'] == 'function' else f"nivel superior (líneas {block['start_line']}-{block['end_line']})"
            yield f"**[Bloque {i}/{len(blocks)}] Analizando: `{label}`...**\n\n"

            # analyze_block is itself an async generator: it yields heartbeat
            # strings while Ollama is still thinking, and yields exactly one
            # dict (the real result) as its last item. Forward every
            # heartbeat straight to the frontend for real-time "still
            # working" feedback; capture the dict when it arrives.
            result = None
            async for item in analyze_block(block, running_summary, model_name):
                if isinstance(item, dict):
                    result = item
                else:
                    yield item
            if result is None:
                result = {"text": f"(El bloque '{block['name']}' no produjo resultado.)", "mermaid": "", "failed": True}

            yield result["text"] + "\n\n---\n\n"

            if result.get("failed"):
                failed_labels.append(label)
                # Distinct marker (not the raw error text) so the consolidation
                # model can't mistake "ReadTimeout" prose for an actual code
                # finding and build a plausible-sounding story around it —
                # this is what caused fabricated function names in testing
                # when a block timed out and running_summary held only the
                # error message.
                running_summary += f"- Bloque '{label}': [NO ANALIZADO — error técnico, sin hallazgos reales]\n"
            else:
                # Feed forward a compact summary, not the full response, so the
                # context for later blocks doesn't itself grow unbounded.
                running_summary += f"- Bloque '{label}': {extract_block_summary(result['text'])}\n"
            if result["mermaid"]:
                mermaid_fragments.append(result["mermaid"])

        if failed_labels and len(failed_labels) == len(blocks):
            # Every block failed: there is zero real information to consolidate.
            # Do NOT call the consolidation model in this state — tested
            # empirically that it will confidently fabricate a full summary,
            # test matrix and gate verdict (including function names that
            # don't exist in the file) from nothing but an error string.
            yield (
                f"\n\n❌ **No se pudo completar el análisis**: los {len(blocks)} bloque(s) del archivo "
                "fallaron por timeout o error de conexión con Ollama. No se generará un resumen ni una "
                "matriz de pruebas, ya que no hay ningún hallazgo real del código para basarlos.\n\n"
                "Reintenta cuando el servidor de Ollama esté menos ocupado, o con un modelo/hardware más rápido.\n"
            )
            gate = {"gate_status": "error_analisis", "fallas_criticas": [], "fallas_medias": [], "total_casos": 0}
            yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"
            yield "\n\n⚠️ **Análisis incompleto.**\n"
            return

        # --- Final consolidation call: no raw code, only the accumulated
        # findings, so this call is always small regardless of file size. ---
        yield "**Consolidando hallazgos de todos los bloques...**\n\n"
        failed_note = ""
        if failed_labels:
            failed_note = (
                f"\n\nADVERTENCIA: {len(failed_labels)} de {len(blocks)} bloque(s) no se pudieron analizar "
                f"(marcados como [NO ANALIZADO] abajo): {', '.join(failed_labels)}. NO inventes hallazgos "
                "para esos bloques ni los uses como base de ninguna fila de la matriz — menciona explícitamente "
                "en el Resumen que esa parte del archivo quedó sin analizar por un error técnico.\n"
            )
        consolidation_prompt = (
            f"Analizaste un archivo PHP grande en {len(blocks)} bloque(s) separado(s) (una función a la vez). "
            "A continuación tienes el resumen de lo que se encontró en cada bloque — y SOLO esos bloques "
            f"existen en este archivo, no hay más. Menciona ÚNICAMENTE los {len(blocks)} bloque(s) que "
            "aparecen en 'Hallazgos por bloque' más abajo; no inventes bloques, funciones ni líneas "
            "adicionales que no fueron listados ahí.\n"
            "Con base ÚNICAMENTE en esa información (no tienes el código completo a la vista), redacta "
            "en español y en Markdown:"
            f"{failed_note}\n\n"
            "### 📋 Resumen del Componente\n"
            "Qué hace el archivo en general, integrando lo que hace cada bloque.\n\n"
            "### ⚠️ Puntos Críticos y Errores Lógicos\n"
            "Consolida los riesgos detectados en los bloques individuales.\n\n"
            "### 🧪 Matriz de Casos de Prueba (Pasa / Falla)\n"
            "Una tabla con al menos 5 filas (mínimo 1 caso feliz, 2 límite, 2 que rompan el código), "
            "usando los hallazgos de los bloques como base. No inventes fallas genéricas que no estén "
            "respaldadas por los hallazgos recibidos: cada fila 'Rompe' debe señalar el bloque o función "
            "real (según los hallazgos de abajo) donde ese caso ocurriría, y por qué.\n"
            "| # | Tipo (Feliz/Límite/Rompe) | Entrada / Escenario | Bloque afectado | Resultado Esperado | ¿Pasa o Falla hoy? |\n"
            "| --- | --- | --- | --- | --- | --- |\n\n"
            "Después de la tabla, para cada fila marcada como 'Falla hoy', añade una sub-sección breve "
            "explicando la causa raíz (según el hallazgo del bloque correspondiente) y una sugerencia "
            "concreta de corrección.\n\n"
            f"Hallazgos por bloque:\n{running_summary}"
        )
        consolidation_result = None
        async for item in call_ollama_with_heartbeat(
            consolidation_prompt, model_name, "Consolidando hallazgos", timeout=300.0
        ):
            if isinstance(item, dict):
                consolidation_result = item
            else:
                yield item

        consolidation_text = ""
        if consolidation_result and consolidation_result.get("content"):
            consolidation_text = consolidation_result["content"]
            yield consolidation_text + "\n\n"
        elif consolidation_result and consolidation_result.get("error"):
            yield f"(No se pudo generar el resumen consolidado: {consolidation_result['error']})\n\n"

        # --- Stitch all per-block Mermaid fragments into one flowchart ---
        if mermaid_fragments:
            merged_mermaid = "```mermaid\nflowchart TD\n" + "\n".join(mermaid_fragments) + "\n```"
            yield "\n\n### 🗺️ Diagrama de Flujo (Consolidado)\n" + merged_mermaid + "\n"

        # --- Risk gate: the test matrix lives in the consolidation response
        # (individual blocks only produce partial findings), so parse it
        # from there specifically. Also folds in danger_score from the
        # deterministic detector (danger_flags.py) so a DELETE/UPDATE sin
        # WHERE, o apuntando a producción, bloquea el gate aunque el LLM no
        # lo haya mencionado explícitamente en la tabla de casos. ---
        matrix_rows = parse_test_matrix(consolidation_text)
        code_danger_flags, code_danger_score = analyze_danger_flags_content(code, ext, filepath=req.filepath)
        gate = classify_severity(matrix_rows, danger_score=code_danger_score, danger_flags=code_danger_flags)
        yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"

        # Explicit end-of-stream marker: the frontend uses this exact string
        # to flip the running/success indicator, since with heartbeat lines
        # now present it's otherwise ambiguous whether the stream is still
        # active or truly finished right after the last content chunk.
        yield "\n\n✅ **Análisis completo.**\n"

    async def event_generator():
        accumulated = []  # capture the full response so we can save it to history after streaming

        # --- Loop engineering path: large PHP files get segmented and
        # analyzed block-by-block instead of one oversized call. ---
        if ext == '.php' and len(code) > LOOP_ENGINEERING_THRESHOLD_CHARS:
            try:
                async for chunk in analyze_php_in_blocks(req.model):
                    accumulated.append(chunk)
                    yield chunk
                full_text = "".join(accumulated)
                if full_text.strip():
                    save_desktop_test_result(req.filepath, req.model, code, full_text)
            except Exception as e:
                yield f"Error inesperado durante el análisis por bloques: {str(e)}"
            return

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        err_body = await response.aread()
                        yield f"Error: Ollama devolvió código {response.status_code}. Detalles: {err_body.decode('utf-8')}"
                        return
                    
                    async for line in response.aiter_lines():
                        if line.strip():
                            try:
                                data = json.loads(line)
                                chunk = data.get("message", {}).get("content", "")
                                if chunk:
                                    accumulated.append(chunk)
                                    yield chunk
                            except Exception:
                                pass

            # Main analysis finished. Now make the dedicated, short call for
            # the Mermaid diagram and stream it as an additional chunk. The
            # frontend just looks for a ```mermaid block anywhere in the full
            # accumulated text, so appending it here works with zero frontend changes.
            diagram_response = await generate_mermaid_diagram(code, ext, req.model)
            if diagram_response.strip():
                diagram_chunk = "\n\n---\n" + diagram_response
                accumulated.append(diagram_chunk)
                yield diagram_chunk

            # --- Risk gate: parse the test matrix from the accumulated text
            # and classify severity, so the frontend can show a structured
            # blocked/warning/approved verdict instead of relying on the
            # person to manually read the whole table and decide. Also folds
            # in danger_score (danger_flags.py) as an independent, code-level
            # signal, so it blocks even if the LLM's matrix wording missed it. ---
            full_text_so_far = "".join(accumulated)
            matrix_rows = parse_test_matrix(full_text_so_far)
            code_danger_flags, code_danger_score = analyze_danger_flags_content(code, ext, filepath=req.filepath)
            gate = classify_severity(matrix_rows, danger_score=code_danger_score, danger_flags=code_danger_flags)
            yield f"\n\n__GATE__:{json.dumps(gate, ensure_ascii=False)}\n"

            # Explicit end-of-stream marker, matching the loop-engineering
            # path, so the frontend has one reliable string to detect
            # completion regardless of which path produced the response.
            yield "\n\n✅ **Análisis completo.**\n"

            # Stream finished without raising: persist this run so the next
            # call on this same file can be told "here's what was found before".
            full_text = "".join(accumulated)
            if full_text.strip():
                save_desktop_test_result(req.filepath, req.model, code, full_text)
        except httpx.ConnectError:
            yield f"Error: No se pudo conectar a Ollama en {settings.ollama_url}. ¿Está encendido?"
        except httpx.TimeoutException:
            yield "Error: Tiempo de espera agotado al comunicarse con Ollama."
        except Exception as e:
            yield f"Error inesperado: {str(e)}"

    return StreamingResponse(event_generator(), media_type="text/plain")


# --- Sesiones de agente multi-tenant: el servidor NUNCA lee filesystem aquí.
# Guardadas en memoria del proceso por ahora (suficiente para un solo
# worker); si se escala a varios workers, este dict se movería a algo
# compartido (Redis) sin cambiar el contrato de los endpoints. ---
_AGENT_SESSIONS: dict = {}


class AgentSessionStartRequest(BaseModel):
    session_id: str
    rel_path: str
    content: str
    project_context: str = ""
    model: str


class AgentSessionContinueRequest(BaseModel):
    session_id: str
    tool_name: str
    tool_result: dict


def _ext_of(rel_path: str) -> str:
    return os.path.splitext(rel_path)[1].lower()


async def _stream_session_step(session_id: str, model: str, tool_result: dict = None, tool_name: str = None):
    """Envuelve advance_session en el mismo protocolo de streaming
    (__HB__/marcador final) que ya usan /api/qa/desktop-test y
    /api/qa/agent-test, para que el frontend reuse el mismo parser."""
    state = _AGENT_SESSIONS.get(session_id)
    if state is None:
        yield f"Error: sesión '{session_id}' no encontrada o expirada.\n"
        yield "\n\n__SESSION_ERROR__\n"
        return

    step_result = None
    async for item in advance_session(state, model, tool_result=tool_result, tool_name=tool_name):
        if isinstance(item, dict):
            step_result = item
        else:
            yield item

    if step_result is None:
        yield "Error: el agente no produjo resultado en este paso.\n"
        return

    if step_result["status"] == "error":
        _AGENT_SESSIONS.pop(session_id, None)
        yield f"\n\n❌ {step_result['error']}\n"
        yield f"\n\n__GATE__:{json.dumps({'gate_status': 'error_analisis', 'fallas_criticas': [], 'fallas_medias': [], 'total_casos': 0}, ensure_ascii=False)}\n"
        return

    if step_result["status"] == "needs_tool":
        _AGENT_SESSIONS[session_id] = step_result["state"]
        yield f"\n\n__TOOL_REQUEST__:{json.dumps({'tool': step_result['tool'], 'args': step_result['args']}, ensure_ascii=False)}\n"
        return

    # done
    _AGENT_SESSIONS.pop(session_id, None)
    yield step_result["final_text"] + "\n\n"
    yield f"\n\n__GATE__:{json.dumps(step_result['gate'], ensure_ascii=False)}\n"
    yield "\n\n✅ **Análisis completo.**\n"


@app.post("/api/qa/agent-session/start")
async def start_agent_session(req: AgentSessionStartRequest):
    """Inicia una sesión de agente multi-tenant. El SERVIDOR nunca toca
    filesystem aquí: `content` ya viene leído por el cliente
    (client/local_agent_client.py) y `project_context` ya fue calculado
    localmente por él (dependency_graph.py corriendo en su máquina)."""
    ext = _ext_of(req.rel_path)
    state = new_session_state(req.rel_path, req.content, ext, project_context=req.project_context)
    state["_model"] = req.model  # se reusa en /continue, donde el request no vuelve a mandar el modelo
    _AGENT_SESSIONS[req.session_id] = state

    async def event_generator():
        async for chunk in _stream_session_step(req.session_id, req.model):
            yield chunk

    return StreamingResponse(event_generator(), media_type="text/plain")


@app.post("/api/qa/agent-session/continue")
async def continue_agent_session(req: AgentSessionContinueRequest):
    """El cliente ya ejecutó la tool solicitada en su propia máquina (vía
    client/local_agent_client.py) y manda aquí el resultado. El servidor
    solo lo inserta en el historial de la conversación y sigue pensando."""
    async def event_generator():
        async for chunk in _stream_session_step(
            req.session_id, model=_AGENT_SESSIONS.get(req.session_id, {}).get("_model", settings.default_model),
            tool_result=req.tool_result, tool_name=req.tool_name
        ):
            yield chunk

    return StreamingResponse(event_generator(), media_type="text/plain")


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
            async for chunk in run_agent_analysis(req.filepath, req.model):
                accumulated.append(chunk)
                yield chunk
            full_text = "".join(accumulated)
            if full_text.strip():
                save_desktop_test_result(req.filepath, req.model, code, full_text)
        except Exception as e:
            yield f"Error inesperado durante el análisis del agente: {str(e)}"

    return StreamingResponse(event_generator(), media_type="text/plain")


class ImpactAnalysisRequest(BaseModel):
    filepath: str


class FalsePositiveRequest(BaseModel):
    filepath: str
    category: str
    line: Optional[int] = None
    nota: str = ""


@app.get("/api/qa/sarif")
def get_sarif_export(filepath: str):
    """Exporta los hallazgos de peligro de UN archivo en formato SARIF 2.1.0
    (el estándar que entienden GitHub Code Scanning, GitLab, Azure DevOps).
    Diseñado para servir DOS usos con el mismo endpoint, sin duplicar
    lógica: (1) un botón de descarga en la UI, y (2) una llamada directa
    desde un pipeline de CI/CD (ej. `curl .../api/qa/sarif?filepath=... -o
    resultado.sarif`) sin pasar por el navegador en absoluto.

    Solo incluye hallazgos con línea real (los del AST — sql_injection,
    missing_where, etc.), ya que SARIF requiere una región de código
    concreta; los hallazgos de puro regex sin línea (credenciales, host de
    producción) no se pueden reportar honestamente con una línea inventada,
    así que se excluyen de este export específico."""
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="El archivo no existe.")

    _, _, danger_flags_structured = analyze_danger_flags(filepath, return_structured=True)
    sarif_doc = build_sarif_for_file(filepath, danger_flags_structured, php_dir=settings.php_dir)
    return sarif_doc


@app.get("/api/qa/false-positives")
def get_false_positives(filepath: str):
    """Lista los falsos positivos confirmados para un archivo, para que el
    frontend pueda marcar visualmente qué hallazgos ya fueron revisados."""
    from qa_history import load_false_positives
    return {"false_positives": load_false_positives(filepath)}


@app.post("/api/qa/false-positives/mark")
def mark_false_positive_endpoint(req: FalsePositiveRequest):
    """Marca un hallazgo específico (category+line) como falso positivo
    confirmado para ESTE archivo. No afecta la detección de ese mismo patrón
    en ningún otro archivo del proyecto — es una excepción puntual, no un
    cambio a la regla general."""
    from qa_history import mark_false_positive
    ok = mark_false_positive(req.filepath, req.category, req.line, req.nota)
    if not ok:
        raise HTTPException(status_code=500, detail="No se pudo guardar el falso positivo.")
    return {"status": "success"}


@app.post("/api/qa/false-positives/unmark")
def unmark_false_positive_endpoint(req: FalsePositiveRequest):
    """Revierte una marca de falso positivo (el hallazgo vuelve a
    reportarse normalmente en la próxima corrida)."""
    from qa_history import unmark_false_positive
    ok = unmark_false_positive(req.filepath, req.category, req.line)
    return {"status": "success" if ok else "not_found"}


@app.get("/api/qa/project-graph")
def get_project_graph_endpoint(force: bool = False):
    """Devuelve el grafo de dependencias cacheado del proyecto actual
    (project_graph_cache.py). `force=true` ignora el cache y reindexa desde
    cero — pensado como botón "Reindexar proyecto" en el frontend, para
    cuando se sabe que hubo cambios grandes (nuevos archivos, refactor) y no
    se quiere esperar a que el chequeo de mtimes lo detecte por sí solo."""
    if not os.path.exists(settings.php_dir):
        raise HTTPException(status_code=400, detail="El directorio de PHP configurado no existe.")
    graph = get_project_graph(settings.php_dir, force=force)
    return {
        "php_dir": graph["php_dir"],
        "generated_at": graph["generated_at"],
        "total_archivos": len(graph["adj_includes"]),
        "archivos_con_tablas": len(graph["file_tables"]),
        "basenames_ambiguos": graph["ambiguous_basenames"],
    }


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
    
    # 3. Direct-only impact set (nivel 1, NO transitivo). Antes esto usaba
    # get_impact_set (BFS que sigue toda la cadena de padres, potencialmente
    # decenas de archivos varios saltos de distancia). Por decisión de
    # producto (2026-07-25): el impacto reportado y el índice de riesgo
    # deben reflejar solo lo DIRECTO — archivos que de verdad incluyen a
    # este archivo, no todo lo que eventualmente podría tocarlo de forma
    # indirecta. Esto también alinea el número con lo que el grafo visual ya
    # dibuja (que siempre fue solo nivel 1). get_impact_set() se conserva en
    # dependency_graph.py por si en el futuro se quiere exponer el
    # transitivo como una vista aparte, opcional.
    impact_set_raw = included_by_direct_raw
    impact_set = included_by_direct

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

    # Calculate Impact Factor. Combine DIRECT (nivel 1) file-graph impact with
    # table-sharing impact: a file with zero includes but that writes to a
    # heavily-shared table is not actually "isolated" — that part of the old
    # logic was correct and se conserva. Lo que cambió es que impact_set_raw
    # ya no es transitivo (ver comentario arriba), así que este conteo ahora
    # refleja solo afectación directa, no la cadena completa de dependencias.
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

    danger_flags, danger_score, danger_flags_structured = analyze_danger_flags(req.filepath, return_structured=True)
    
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
        "graph": {"nodes": graph_nodes, "edges": graph_edges},
        # New: hallazgos con category+line reales (solo los que vienen del
        # AST — sql_injection, command_injection, code_injection,
        # path_traversal, missing_where), para que el frontend pueda ofrecer
        # "marcar como falso positivo" apuntando a un hallazgo específico.
        "danger_flags_structured": danger_flags_structured,
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
