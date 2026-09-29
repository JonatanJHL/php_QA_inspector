import os
import socket
from pydantic import BaseModel


# App State / Configuration
class Settings(BaseModel):
    php_dir: str
    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen2.5-coder:14b"


# Directorio del proyecto PHP a analizar. Se lee de la variable de entorno
# QA_AGENT_PHP_DIR para no hardcodear una ruta personal en el repo — cada
# quien la exporta en su propio equipo antes de correr run.sh, ej.:
#   export QA_AGENT_PHP_DIR="/ruta/a/tu/proyecto/php"
# Sin esa variable, cae a os.getcwd() (el directorio desde donde se lanzó
# el proceso), que es un fallback razonable para probar rápido sin config.
DEFAULT_PHP_DIR = os.environ.get("QA_AGENT_PHP_DIR", "")
if not DEFAULT_PHP_DIR or not os.path.exists(DEFAULT_PHP_DIR):
    DEFAULT_PHP_DIR = os.getcwd()
    print(f"[config] QA_AGENT_PHP_DIR no configurada o no existe, usando cwd como fallback: {DEFAULT_PHP_DIR}")

settings = Settings(
    php_dir=DEFAULT_PHP_DIR,
    ollama_url="http://localhost:11434",
    default_model="qwen2.5-coder:14b"
)

# Ventana de contexto por llamada a Ollama. Centralizado aquí (en vez de
# repetido como 8192 hardcodeado en agent_loop.py, main.py y
# ollama_client.py) para que un solo cambio se propague a todo el backend.
# Confirmado empíricamente en este equipo (M1 Pro, 16GB RAM) con
# qwen2.5-coder:14b (modelo actual): con num_ctx=16384 el modelo ocupa 12GB
# y Ollama reporta 91%/9% GPU/CPU (empieza a exceder la RAM unificada
# disponible y cede una fracción a CPU). Con num_ctx=8192 el modelo ocupa
# 10GB y corre 100% GPU, con la MISMA velocidad de generación en caliente
# (~5.3s en ambos casos) — así que 8192 da el mismo rendimiento con más
# margen de RAM libre para el resto del sistema. Si se cambia a un modelo
# más grande en el futuro, repetir esta prueba antes de subir el valor.
DEFAULT_NUM_CTX = 8192


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
