#!/usr/bin/env bash
# Equivalente Mac de run.bat (que es un vestigio de Windows y no aplica en
# este equipo). Crea el entorno virtual si no existe, instala dependencias,
# y levanta el backend + frontend en http://127.0.0.1:8000.
set -e

cd "$(dirname "$0")"

echo "======================================================================"
echo "   INICIANDO EL ORQUESTADOR DE AGENTES QA PARA PHP (LOCAL Y GRATUITO)"
echo "======================================================================"
echo ""

if [ ! -d ".venv" ]; then
  echo "[INFO] Creando el entorno virtual de Python (.venv)..."
  python3 -m venv .venv
fi

echo "[INFO] Activando entorno virtual e instalando dependencias..."
source .venv/bin/activate
python3 -m pip install --upgrade pip -q
pip install -r backend/requirements.txt -q

# Verificación rápida de dependencias externas no-Python, para dar un aviso
# claro en vez de un error críptico más adelante si falta algo.
if ! command -v php &> /dev/null; then
  echo "[AVISO] 'php' no está en PATH. El linter de sintaxis PHP y el"
  echo "        análisis de AST/taint (php_ast_bridge) no funcionarán."
  echo "        Instalar con: brew install php"
fi
if ! command -v ollama &> /dev/null; then
  echo "[AVISO] 'ollama' no está en PATH. El análisis con LLM no funcionará."
  echo "        Instalar desde: https://ollama.com"
fi
if [ ! -d "backend/php_ast_bridge/vendor" ]; then
  echo "[AVISO] backend/php_ast_bridge/vendor no existe todavía."
  echo "        Correr: cd backend/php_ast_bridge && composer install"
fi
if [ -z "$QA_AGENT_PHP_DIR" ]; then
  echo "[AVISO] QA_AGENT_PHP_DIR no está exportada — se usará el directorio"
  echo "        actual como proyecto PHP por defecto. Para apuntar a tu"
  echo "        proyecto real: export QA_AGENT_PHP_DIR=\"/ruta/a/tu/proyecto\""
fi

echo "[INFO] Abriendo la aplicación en tu navegador..."
open http://127.0.0.1:8000 &

echo "[INFO] Iniciando el servidor local uvicorn en puerto 8000..."
echo "Presiona Ctrl+C para detener el servidor."
echo ""
python3 -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000 --reload
