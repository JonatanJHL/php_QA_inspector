import os
import socket
from pydantic import BaseModel


# App State / Configuration
class Settings(BaseModel):
    php_dir: str
    ollama_url: str = "http://localhost:11434"
    default_model: str = "hermes3:latest"
    # NVIDIA NIM (build.nvidia.com) — proveedor en la nube, opcional, convive
    # con Ollama sin reemplazarlo. IMPORTANTE: la API key NUNCA vive aquí ni
    # en ningún campo de Settings — este objeto se refleja tal cual en
    # GET /api/config, y esa credencial no debe poder filtrarse por ahí. Se
    # lee directo de os.environ["NVIDIA_API_KEY"] dentro de nvidia_client.py.
    llm_provider: str = "ollama"  # "ollama" | "nvidia"
    nvidia_model: str = "nvidia/nemotron-4-340b-instruct"


# Directorio PHP por defecto: opcionalmente configurable via QA_PHP_DIR (util
# para no tener que abrir la UI y guardarlo manualmente cada vez que se clona
# el repo en una maquina nueva), si no se especifica cae al directorio desde
# donde se ejecuta el servidor. Nunca se hardcodea una ruta de una maquina
# especifica -- este proyecto esta pensado para usarse contra cualquier
# codigo PHP, no solo el sistema donde se origino.
DEFAULT_PHP_DIR = os.environ.get("QA_PHP_DIR") or os.getcwd()

settings = Settings(
    php_dir=DEFAULT_PHP_DIR,
    ollama_url="http://localhost:11434",
    default_model="hermes3:latest"
)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
