"""
Runner del cliente: ejecuta el ciclo completo hablando con el servidor
multi-tenant (backend/agent_session.py vía /api/qa/agent-session/*).

Flujo:
  1. Lee el archivo objetivo LOCALMENTE (nunca se manda una ruta al server).
  2. Calcula el contexto de proyecto LOCALMENTE (dependency_graph.py).
  3. POST /start con {rel_path, content, project_context, model}.
  4. El servidor responde en streaming. Si el stream trae
     __TOOL_REQUEST__:{...}, este runner ejecuta esa tool LOCALMENTE
     (dispatch_local_tool) y hace POST /continue con el resultado.
  5. Repite 4 hasta que el stream termine con __GATE__ (análisis completo)
     o un error.

Uso:
    python3 client_runner.py --project /ruta/al/proyecto --file modulos/x.php
"""
import argparse
import json
import re
import uuid

import requests

from local_agent_client import LocalAgentClient, dispatch_local_tool


def _parse_stream(text: str):
    """Extrae el marcador especial (si hay) y el texto visible del cuerpo
    de un chunk de streaming. Mismo protocolo __HB__/__TOOL_REQUEST__/__GATE__
    que ya usa el resto del proyecto."""
    tool_request = None
    gate = None
    visible_lines = []
    for line in text.split("\n"):
        if line.startswith("__TOOL_REQUEST__:"):
            tool_request = json.loads(line[len("__TOOL_REQUEST__:"):])
        elif line.startswith("__GATE__:"):
            gate = json.loads(line[len("__GATE__:"):])
        elif line.startswith("__HB__:"):
            print(f"  ... {line[len('__HB__:'):]}")
        else:
            visible_lines.append(line)
    return "\n".join(visible_lines), tool_request, gate


def _post_and_read(url: str, payload: dict) -> str:
    """POST con streaming real: el servidor manda el cuerpo por chunks
    (StreamingResponse), así que hay que consumirlos activamente en vez de
    esperar resp.text, que en pruebas se quedó esperando de más con
    respuestas largas de este endpoint."""
    resp = requests.post(url, json=payload, stream=True, timeout=300)
    chunks = []
    for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
        if chunk:
            chunks.append(chunk)
    return "".join(chunks)


def run_analysis(server_url: str, project_dir: str, rel_filepath: str, model: str = "qwen2.5-coder:14b"):
    client = LocalAgentClient(project_dir)
    session_id = str(uuid.uuid4())

    file_result = client.leer_archivo(rel_filepath)
    if "error" in file_result:
        print(f"Error: {file_result['error']}")
        return

    project_context = client.resumen_contexto_proyecto(rel_filepath)

    print(f"Iniciando análisis de `{rel_filepath}` (sesión {session_id[:8]}...)")
    text = _post_and_read(
        f"{server_url}/api/qa/agent-session/start",
        {
            "session_id": session_id,
            "rel_path": file_result["rel_path"],
            "content": file_result["content"],
            "project_context": project_context,
            "model": model,
        },
    )
    full_text, tool_request, gate = _parse_stream(text)

    # Bucle: mientras el servidor pida tools, se resuelven localmente y se
    # continúa la sesión. Termina cuando llega un gate (análisis completo)
    # o el servidor deja de pedir tools sin dar un gate (caso de error).
    max_round_trips = 20  # tope defensivo: nunca debería tardar tanto como MAX_TURNS*2 del servidor
    round_trips = 0
    while tool_request and round_trips < max_round_trips:
        round_trips += 1
        tool_name = tool_request["tool"]
        tool_args = tool_request["args"]
        print(f"Ejecutando localmente: {tool_name}({tool_args})")
        tool_result = dispatch_local_tool(client, tool_name, tool_args)

        resp = _post_and_read(
            f"{server_url}/api/qa/agent-session/continue",
            {"session_id": session_id, "tool_name": tool_name, "tool_result": tool_result},
        )
        chunk_text, tool_request, gate = _parse_stream(resp)
        full_text += chunk_text

    print("\n--- Resultado ---\n")
    print(full_text.strip())
    if gate:
        print("\n--- Veredicto (gate) ---")
        print(json.dumps(gate, ensure_ascii=False, indent=2))
    elif tool_request:
        print(f"\n⚠️ Se alcanzó el límite de {max_round_trips} idas y vueltas sin terminar.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente local del agente de QA (multi-tenant)")
    parser.add_argument("--server", default="http://localhost:8000", help="URL del servidor central")
    parser.add_argument("--project", required=True, help="Directorio raíz del proyecto local a analizar")
    parser.add_argument("--file", required=True, help="Ruta relativa (dentro del proyecto) del archivo a analizar")
    parser.add_argument("--model", default="qwen2.5-coder:14b", help="Modelo a usar (debe existir en el servidor/Ollama)")
    args = parser.parse_args()

    run_analysis(args.server, args.project, args.file, args.model)
