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


# --- Falsos positivos confirmados por el humano ---
# Reutiliza el mismo archivo JSON por archivo (qa_history/<hash>.json) en vez
# de un sistema de persistencia paralelo. Un hallazgo se identifica por
# (categoria, linea) en vez del texto completo del mensaje, para que
# pequeños cambios de redacción en danger_flags.py/ast_bridge.py no
# invaliden silenciosamente los falsos positivos ya confirmados por el
# usuario — lo que realmente identifica un hallazgo es DÓNDE está y de QUÉ
# tipo es, no las palabras exactas del mensaje generado.
def _fp_key(category: str, line) -> str:
    return f"{category}:{line if line is not None else 'sin_linea'}"


def load_false_positives(filepath: str) -> dict:
    """Devuelve {clave_hallazgo: {marcado_por, fecha, nota}} para este
    archivo. Vacío si nunca se marcó nada."""
    key = _history_key(filepath)
    hist_path = os.path.join(QA_HISTORY_DIR, f"{key}.json")
    if not os.path.exists(hist_path):
        return {}
    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("false_positives", {})
    except Exception:
        return {}


def mark_false_positive(filepath: str, category: str, line, nota: str = ""):
    """Marca un hallazgo específico (categoria+línea) como falso positivo
    confirmado para este archivo. La próxima vez que se analice el mismo
    archivo, ese hallazgo específico se silencia (no se elimina del
    detector, solo se excluye de danger_flags/danger_score para ESTE
    archivo en particular) — así el mismo patrón sigue detectándose
    normalmente en cualquier otro archivo del proyecto."""
    key = _history_key(filepath)
    hist_path = os.path.join(QA_HISTORY_DIR, f"{key}.json")

    data = {"filepath": os.path.realpath(filepath), "entries": [], "false_positives": {}}
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    fp_key = _fp_key(category, line)
    data.setdefault("false_positives", {})[fp_key] = {
        "timestamp": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "category": category,
        "line": line,
        "nota": nota,
    }

    try:
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def unmark_false_positive(filepath: str, category: str, line) -> bool:
    """Revierte una marca de falso positivo (por si el usuario se equivocó,
    o el código cambió y el hallazgo vuelve a ser real)."""
    key = _history_key(filepath)
    hist_path = os.path.join(QA_HISTORY_DIR, f"{key}.json")
    if not os.path.exists(hist_path):
        return False
    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        fp_key = _fp_key(category, line)
        if fp_key in data.get("false_positives", {}):
            del data["false_positives"][fp_key]
            with open(hist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        return False
    except Exception:
        return False
