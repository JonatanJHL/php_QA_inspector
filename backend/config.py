import os
import socket
from pydantic import BaseModel


# App State / Configuration
class Settings(BaseModel):
    php_dir: str
    ollama_url: str = "http://localhost:11434"
    default_model: str = "hermes3:latest"


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
