import os
import json
import hashlib


# --- Historial de pruebas de escritorio (memoria persistente entre corridas) ---
# Ollama/Hermes3 no tiene memoria propia entre llamadas: cada request es
# independiente. Para simular continuidad, guardamos en disco el resultado
# de cada prueba de escritorio por archivo (identificado por hash del path
# absoluto) y se lo re-inyectamos al modelo como contexto en la siguiente
# corrida sobre ese mismo archivo.
QA_HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "qa_history")
os.makedirs(QA_HISTORY_DIR, exist_ok=True)


def _history_key(filepath: str) -> str:
    return hashlib.sha256(os.path.realpath(filepath).encode("utf-8")).hexdigest()[:20]


def load_desktop_test_history(filepath: str, max_entries: int = 3):
    """Load the last `max_entries` desktop-test results for this exact file,
    most recent first. Returns [] if there's no history yet."""
    key = _history_key(filepath)
    hist_path = os.path.join(QA_HISTORY_DIR, f"{key}.json")
    if not os.path.exists(hist_path):
        return []
    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", [])
        return entries[-max_entries:][::-1]
    except Exception:
        return []


def save_desktop_test_result(filepath: str, model: str, code: str, result_markdown: str):
    """Append this run's result to the file's history, keeping at most 10
    entries. Stores a hash of the code so the model can be told whether the
    file changed since the last recorded run."""
    key = _history_key(filepath)
    hist_path = os.path.join(QA_HISTORY_DIR, f"{key}.json")
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]

    data = {"filepath": os.path.realpath(filepath), "entries": []}
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    data.setdefault("entries", []).append({
        "timestamp": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "code_hash": code_hash,
        "summary": result_markdown[:2000]
    })
    data["entries"] = data["entries"][-10:]

    try:
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
