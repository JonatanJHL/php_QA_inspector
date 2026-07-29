#!/usr/bin/env bash
# ============================================================
#  QA Agent Inspector - Cliente SSH para el Agente con LLM
#  Uso: ./qa_agent_inspect.sh nombre_o_ruta_relativa.php [provider] [model]
#
#  A diferencia de qa_inspect.sh (analisis estatico instantaneo, sin LLM,
#  manda el CONTENIDO del archivo tal cual vive en este servidor), este
#  script llama al AGENTE con herramientas (/api/qa/agent-test), que usa un
#  LLM real para investigar el codigo y dar un veredicto con matriz de
#  casos de prueba. Puede tardar varios minutos, sobre todo con NVIDIA.
#
#  IMPORTANTE: el agente lee el archivo del disco de la PC que corre el
#  backend (su `php_dir` configurado ahi), NO de este servidor SSH. Este
#  script busca el archivo por nombre via /api/files para encontrar su
#  ruta real en esa PC. Si el archivo cambio aqui pero el respaldo de esa
#  PC todavia no, el agente analiza la version vieja sin avisar.
# ============================================================

# --- CONFIGURACION ---
# Si usas un tunel SSH inverso (ssh -R 8000:localhost:8000 usuario@este-vps)
# corriendo DESDE la PC Windows hacia este servidor, usa localhost aqui:
QA_URL="http://localhost:8000"
# Si usas Tailscale en vez de un tunel SSH, comenta la linea de arriba y usa
# la IP de Tailscale de la PC Windows:
# QA_URL="http://100.x.x.x:8000"

# Exporta esta variable en tu ~/.bashrc o antes de correr el script — NUNCA
# la hardcodees aqui. Debe coincidir con QA_API_KEY configurada en la PC
# Windows (si esa PC no la tiene configurada, deja esto vacio).
QA_API_KEY="${QA_API_KEY:-}"

# Presets ya probados y confirmados como confiables — mismos que el dropdown
# del frontend (ver MODEL_PRESETS en frontend/app.js).
DEFAULT_PROVIDER="nvidia"
DEFAULT_MODEL="nvidia/llama-3.3-nemotron-super-49b-v1.5"

LOG_DIR="${HOME}/qa_logs"
mkdir -p "$LOG_DIR"

EXTRA_HEADERS=()
[ -n "$QA_API_KEY" ] && EXTRA_HEADERS+=(-H "X-API-Key: ${QA_API_KEY}")

RED='\033[0;31m'
ORANGE='\033[0;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

if [ -z "$1" ]; then
  echo -e "${RED}ERROR: Debes pasar el nombre o ruta relativa del archivo.${RESET}"
  echo "  Uso: ./qa_agent_inspect.sh nombre_archivo.php [provider] [model]"
  echo "  Ej:  ./qa_agent_inspect.sh mis_equipos.php nvidia"
  exit 1
fi

QUERY_NAME="$1"
PROVIDER="${2:-$DEFAULT_PROVIDER}"
MODEL="${3:-$DEFAULT_MODEL}"
TIMESTAMP=$(date '+%Y-%m-%d_%H-%M-%S')
LOG_FILE="${LOG_DIR}/agent_${TIMESTAMP}_$(basename "$QUERY_NAME").log"

log() { echo -e "$1"; echo -e "$1" | sed 's/\033\[[0-9;]*m//g' >> "$LOG_FILE"; }

log ""
log "${BOLD}${CYAN}=================================================${RESET}"
log "${BOLD}${CYAN}  QA AGENT INSPECTOR - Analisis con LLM via SSH   ${RESET}"
log "${BOLD}${CYAN}=================================================${RESET}"
log "  Servidor QA : ${QA_URL}"
log "  Archivo     : ${QUERY_NAME}"
log "  Proveedor   : ${PROVIDER}"
log "  Modelo      : ${MODEL}"
log "  Log         : ${LOG_FILE}"
log ""

# --- PASO 1: Resolver la ruta real del archivo en la PC que corre el backend ---
log "${CYAN}[1/3] Buscando '${QUERY_NAME}' en el servidor QA...${RESET}"
FILES_JSON=$(curl -s -m 15 "${EXTRA_HEADERS[@]}" "${QA_URL}/api/files")

if [ -z "$FILES_JSON" ]; then
  log "${RED}  X No se pudo conectar a ${QA_URL}/api/files${RESET}"
  exit 2
fi

if echo "$FILES_JSON" | grep -q '"detail".*API key'; then
  log "${RED}  X API key invalida o faltante — revisa QA_API_KEY.${RESET}"
  exit 2
fi

FULL_PATH=$(echo "$FILES_JSON" | python3 -c "
import sys, json
query = sys.argv[1].replace('\\\\', '/').lower()
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for f in data.get('files', []):
    name = f.get('name', '').lower()
    rel = f.get('rel_path', '').replace('\\\\', '/').lower()
    if name == query.split('/')[-1] or rel == query:
        print(f.get('full_path', '').replace('\\\\', '/'))
        break
" "$QUERY_NAME")

if [ -z "$FULL_PATH" ]; then
  log "${RED}  X No se encontro un archivo llamado '${QUERY_NAME}' en el servidor QA.${RESET}"
  log "${DIM}    Recuerda: el agente busca en el respaldo de la PC Windows, no en este servidor.${RESET}"
  exit 2
fi
log "${GREEN}  OK Encontrado: ${FULL_PATH}${RESET}"

# --- PASO 2: Lanzar el agente (streaming, pero se consume completo antes de mostrar) ---
log ""
log "${CYAN}[2/3] Corriendo el agente (puede tardar varios minutos)...${RESET}"

BODY=$(python3 -c "
import json, sys
print(json.dumps({'filepath': sys.argv[1], 'model': sys.argv[2], 'provider': sys.argv[3]}))
" "$FULL_PATH" "$MODEL" "$PROVIDER")

RAW_STREAM=$(mktemp /tmp/qa_agent_stream_XXXXXX.txt)
HTTP_CODE=$(curl -s -m 600 -X POST "${QA_URL}/api/qa/agent-test" \
  -H "Content-Type: application/json" \
  "${EXTRA_HEADERS[@]}" \
  --data "$BODY" \
  -o "$RAW_STREAM" \
  -w "%{http_code}")

if [ "$HTTP_CODE" = "000" ]; then
  log "${RED}  X Timeout (10 min) o sin conexion.${RESET}"
  rm -f "$RAW_STREAM"
  exit 3
fi

if [ "$HTTP_CODE" = "401" ]; then
  log "${RED}  X API key invalida o faltante (HTTP 401).${RESET}"
  rm -f "$RAW_STREAM"
  exit 3
fi

if [ "$HTTP_CODE" != "200" ]; then
  log "${RED}  X El servidor respondio HTTP ${HTTP_CODE}${RESET}"
  log "${RED}    $(head -c 300 "$RAW_STREAM")${RESET}"
  rm -f "$RAW_STREAM"
  exit 3
fi

log "${GREEN}  OK Agente respondio (HTTP ${HTTP_CODE})${RESET}"

# --- PASO 3: Filtrar heartbeats, mostrar el reporte + el veredicto del gate ---
log ""
log "${CYAN}[3/3] Reporte del agente:${RESET}"
log ""

REPORT=$(grep -v '^__HB__:' "$RAW_STREAM" | grep -v '^__GATE__:')
echo "$REPORT" >> "$LOG_FILE"
echo -e "$REPORT"

GATE_LINE=$(grep '^__GATE__:' "$RAW_STREAM" | tail -1)
rm -f "$RAW_STREAM"

if [ -n "$GATE_LINE" ]; then
  GATE_STATUS=$(echo "${GATE_LINE#__GATE__:}" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('gate_status', '?'))
except Exception:
    print('?')
")
  log ""
  log "${BOLD}  ===========================================${RESET}"
  case "$GATE_STATUS" in
    bloqueado)
      log "${RED}${BOLD}  GATE: BLOQUEADO (riesgo critico detectado)${RESET}";;
    requiere_confirmacion)
      log "${ORANGE}${BOLD}  GATE: REQUIERE CONFIRMACION${RESET}";;
    aprobado)
      log "${GREEN}${BOLD}  GATE: APROBADO${RESET}";;
    *)
      log "${DIM}  GATE: ${GATE_STATUS}${RESET}";;
  esac
  log "${BOLD}  ===========================================${RESET}"
fi

log ""
log "${DIM}  Log completo guardado en: ${LOG_FILE}${RESET}"
log ""

case "$GATE_STATUS" in
  aprobado) exit 0;;
  requiere_confirmacion) exit 10;;
  *) exit 11;;
esac
