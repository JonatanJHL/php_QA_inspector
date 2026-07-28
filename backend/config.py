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


# Default configuration paths (using typical windows path structure found in metadata)
DEFAULT_PHP_DIR = r"c:\Users\jonat\OneDrive\Escritorio\backup\capa8\prod\SistemaColaboradores"
if not os.path.exists(DEFAULT_PHP_DIR):
    # Fallback to current directory if default doesn't exist
    DEFAULT_PHP_DIR = os.getcwd()

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
