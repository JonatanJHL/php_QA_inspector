#!/usr/bin/env bash
# ============================================================
#  QA Inspector v3 - Cliente SSH para el Orquestador de Agentes
#  Uso: ./qa_inspect.sh /ruta/al/archivo.php
# ============================================================

# --- CONFIGURACION ---
# Si usas un tunel SSH inverso (ssh -R 8000:localhost:8000 usuario@este-vps)
# corriendo DESDE la PC Windows hacia este servidor, usa localhost aqui:
QA_URL="http://localhost:8000"
# Si usas Tailscale en vez de un tunel SSH, comenta la linea de arriba y usa
# la IP de Tailscale de la PC Windows:
# QA_URL="http://100.x.x.x:8000"
# Si en vez de eso sigues usando ngrok, pon la URL de ngrok aqui y descomenta
# el header de abajo (ngrok muestra una pagina de advertencia sin el).
# QA_URL="https://tu-url.ngrok-free.app"
# QA_NGROK_HEADER="ngrok-skip-browser-warning: 1"
QA_NGROK_HEADER="${QA_NGROK_HEADER:-}"

# Exporta esta variable en tu ~/.bashrc o antes de correr el script — NUNCA
# la hardcodees aqui. Debe coincidir con QA_API_KEY configurada en la PC
# Windows (si esa PC no la tiene configurada, deja esto vacio).
QA_API_KEY="${QA_API_KEY:-}"

LOG_DIR="${HOME}/qa_logs"
mkdir -p "$LOG_DIR"

# Headers extra a mandar en cada curl — se construyen como array para no
# mandar un -H vacio si QA_NGROK_HEADER/QA_API_KEY no estan configuradas.
EXTRA_HEADERS=()
[ -n "$QA_NGROK_HEADER" ] && EXTRA_HEADERS+=(-H "$QA_NGROK_HEADER")
[ -n "$QA_API_KEY" ] && EXTRA_HEADERS+=(-H "X-API-Key: ${QA_API_KEY}")

# Colores ANSI
RED='\033[0;31m'
ORANGE='\033[0;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

# --- VALIDACION ---
if [ -z "$1" ]; then
  echo -e "${RED}ERROR: Debes pasar la ruta del archivo como argumento.${RESET}"
  echo "  Uso:    ./qa_inspect.sh /ruta/al/archivo"
  exit 1
fi

FILEPATH="$1"
FILENAME=$(basename "$FILEPATH")
TIMESTAMP=$(date '+%Y-%m-%d_%H-%M-%S')
LOG_FILE="${LOG_DIR}/${TIMESTAMP}_${FILENAME}.log"

# Funcion para escribir tanto en consola como en log
log() { echo -e "$1"; echo -e "$1" | sed 's/\033\[[0-9;]*m//g' >> "$LOG_FILE"; }

if [ ! -f "$FILEPATH" ]; then
  log "${RED}ERROR: El archivo no existe: ${FILEPATH}${RESET}"
  exit 1
fi

log ""
log "${BOLD}${CYAN}============================================${RESET}"
log "${BOLD}${CYAN}  QA INSPECTOR v3 - Analisis Remoto SSH   ${RESET}"
log "${BOLD}${CYAN}============================================${RESET}"
log "  Servidor QA : ${QA_URL}"
log "  Archivo     : ${FILEPATH}"
log "  Nombre      : ${FILENAME}"
log "  Log         : ${LOG_FILE}"
log "  Fecha/Hora  : ${TIMESTAMP}"
log ""

# --- PASO 1: Verificar conectividad ---
log "${CYAN}[1/3] Verificando conectividad con el servidor QA...${RESET}"
PING_HTTP_CODE=$(curl -s -o /tmp/qa_ping_body.txt --max-time 8 -w "%{http_code}" \
  "${EXTRA_HEADERS[@]}" "${QA_URL}/api/config" 2>&1)
PING_RESULT=$(cat /tmp/qa_ping_body.txt 2>/dev/null)
rm -f /tmp/qa_ping_body.txt

if ! [[ "$PING_HTTP_CODE" =~ ^[0-9]+$ ]]; then
  log "${RED}  X No se pudo conectar a ${QA_URL} (curl fallo, sin respuesta HTTP)${RESET}"
  exit 2
fi

if [ "$PING_HTTP_CODE" = "401" ]; then
  log "${RED}  X API key invalida o faltante (HTTP 401).${RESET}"
  log "${RED}    Revisa que QA_API_KEY aqui coincida con la configurada en la PC Windows.${RESET}"
  exit 2
fi

if [ "$PING_HTTP_CODE" != "200" ]; then
  log "${RED}  X El servidor QA respondio codigo HTTP ${PING_HTTP_CODE} (se esperaba 200)${RESET}"
  log "${RED}    Esto normalmente indica que el tunel esta muerto o apunta a un puerto sin backend activo.${RESET}"
  log "${RED}    Respuesta: ${PING_RESULT:0:200}${RESET}"
  exit 2
fi

if echo "$PING_RESULT" | grep -q "<!DOCTYPE\|<html"; then
  log "${RED}  X El servidor devolvio HTML en lugar de JSON en /api/config${RESET}"
  log "${RED}    Respuesta: ${PING_RESULT:0:200}${RESET}"
  exit 2
fi

log "${GREEN}  OK Servidor QA en linea (HTTP ${PING_HTTP_CODE})${RESET}"

# --- PASO 2: Leer archivo y enviar via archivo temporal ---
log ""
log "${CYAN}[2/3] Leyendo archivo y enviando al analizador...${RESET}"

# Escribir JSON a archivo temporal (evita problemas con caracteres especiales y archivos grandes)
TMP_JSON=$(mktemp /tmp/qa_body_XXXXXX.json)

python3 - << PYEOF > "$TMP_JSON"
import json, sys

filepath = "$FILEPATH"
filename = "$FILENAME"

try:
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
except Exception as e:
    print(json.dumps({'error': str(e)}))
    sys.exit(1)

body = {
    'filename': filename,
    'content': content,
    'server_path': filepath
}
print(json.dumps(body))
PYEOF

if [ ! -s "$TMP_JSON" ]; then
  log "${RED}  X No se pudo construir el JSON del archivo.${RESET}"
  rm -f "$TMP_JSON"
  exit 3
fi

# Verificar que el JSON es valido antes de enviarlo
if python3 -c "import json; json.load(open('$TMP_JSON'))" 2>/dev/null; then
  log "${GREEN}  OK Archivo leido ($(wc -c < $TMP_JSON) bytes de JSON)${RESET}"
else
  log "${RED}  X El JSON generado es invalido.${RESET}"
  rm -f "$TMP_JSON"
  exit 3
fi

# Llamar al API con el archivo temporal
TMP_RESPONSE=$(mktemp /tmp/qa_resp_XXXXXX.txt)
HTTP_CODE=$(curl -s --max-time 120 -X POST "${QA_URL}/api/qa/inspect-content" \
  -H "Content-Type: application/json" \
  "${EXTRA_HEADERS[@]}" \
  --data @"$TMP_JSON" \
  -o "$TMP_RESPONSE" \
  -w "%{http_code}" 2>&1)
RESULT_JSON=$(cat "$TMP_RESPONSE" 2>/dev/null)
rm -f "$TMP_JSON" "$TMP_RESPONSE"

# Guardar respuesta raw en el log
echo "=== HTTP CODE: ${HTTP_CODE} ===" >> "$LOG_FILE"
echo "=== RAW JSON RESPONSE ===" >> "$LOG_FILE"
echo "$RESULT_JSON" >> "$LOG_FILE"
echo "=========================" >> "$LOG_FILE"

# Verificar codigo HTTP
if [ "$HTTP_CODE" = "000" ]; then
  log "${RED}  X Timeout o sin conexion (curl no recibio respuesta en 120s)${RESET}"
  exit 4
fi

if [ "$HTTP_CODE" != "200" ]; then
  log "${RED}  X El servidor respondio HTTP ${HTTP_CODE}${RESET}"
  log "${RED}    Respuesta: ${RESULT_JSON:0:300}${RESET}"
  exit 4
fi

# Verificar que es JSON valido
if echo "$RESULT_JSON" | grep -q "<!DOCTYPE\|<html"; then
  log "${RED}  X El servidor devolvio HTML (pagina de advertencia del tunel)${RESET}"
  log "${ORANGE}  Abre ${QA_URL} en el navegador y acepta la advertencia primero.${RESET}"
  exit 4
fi

if [ -z "$RESULT_JSON" ] || [ "$RESULT_JSON" = $'\n' ]; then
  log "${RED}  X El servidor devolvio HTTP ${HTTP_CODE} pero con cuerpo vacio.${RESET}"
  log "${RED}    Revisa la terminal donde corre el backend (uvicorn) para ver el error.${RESET}"
  exit 4
fi

if echo "$RESULT_JSON" | grep -q '"detail"'; then
  DETAIL=$(echo "$RESULT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('detail',''))" 2>/dev/null)
  log "${RED}  X Error del servidor: ${DETAIL}${RESET}"
  exit 4
fi

log "${GREEN}  OK Analisis completado (HTTP ${HTTP_CODE})${RESET}"

# --- PASO 3: Parsear TODOS los resultados ---
log ""
log "${CYAN}[3/3] Procesando resultados...${RESET}"

# Extraer todos los campos de una sola llamada python para evitar problemas
PARSED=$(echo "$RESULT_JSON" | python3 -c "
import sys, json

try:
    d = json.load(sys.stdin)
except Exception as e:
    print('PARSE_ERROR:' + str(e))
    sys.exit(1)

syntax_ok = str(d.get('syntax_ok', False)).lower()
syntax_out = d.get('syntax_output', '(sin detalle)')
syntax_line = str(d.get('syntax_error_line', '')) if d.get('syntax_error_line') else ''
risk_index = str(d.get('risk_index', 0))
risk_level = d.get('risk_level', 'N/A')
danger_score = str(d.get('danger_score', 0))
verdict = d.get('verdict', 'RECHAZADO')
flags = d.get('danger_flags', [])

print('SYNTAX_OK=' + syntax_ok)
print('SYNTAX_LINE=' + syntax_line)
print('RISK_INDEX=' + risk_index)
print('RISK_LEVEL=' + risk_level)
print('DANGER_SCORE=' + danger_score)
print('VERDICT=' + verdict)
print('FLAGS_COUNT=' + str(len(flags)))
print('---SYNTAX_OUT---')
print(syntax_out)
print('---END_SYNTAX_OUT---')
print('---FLAGS---')
for f in flags:
    print(f)
print('---END_FLAGS---')
" 2>&1)

if echo "$PARSED" | grep -q "PARSE_ERROR"; then
  log "${RED}  X Error al parsear la respuesta JSON:${RESET}"
  log "${RED}  $(echo "$PARSED" | grep PARSE_ERROR)${RESET}"
  log "${DIM}  Respuesta raw guardada en: ${LOG_FILE}${RESET}"
  exit 5
fi

# Extraer variables del bloque parseado
SYNTAX_OK=$(echo "$PARSED" | grep "^SYNTAX_OK=" | cut -d= -f2)
SYNTAX_LINE=$(echo "$PARSED" | grep "^SYNTAX_LINE=" | cut -d= -f2-)
RISK_INDEX=$(echo "$PARSED" | grep "^RISK_INDEX=" | cut -d= -f2)
RISK_LEVEL=$(echo "$PARSED" | grep "^RISK_LEVEL=" | cut -d= -f2-)
DANGER_SCORE=$(echo "$PARSED" | grep "^DANGER_SCORE=" | cut -d= -f2)
VERDICT=$(echo "$PARSED" | grep "^VERDICT=" | cut -d= -f2-)
FLAGS_COUNT=$(echo "$PARSED" | grep "^FLAGS_COUNT=" | cut -d= -f2)

SYNTAX_OUT=$(echo "$PARSED" | awk '/---SYNTAX_OUT---/{found=1; next} /---END_SYNTAX_OUT---/{found=0} found{print}')
FLAGS_TEXT=$(echo "$PARSED" | awk '/---FLAGS---/{found=1; next} /---END_FLAGS---/{found=0} found{print}')

# Color del riesgo
if   [ "$RISK_INDEX" -ge 90 ] 2>/dev/null; then RISK_COLOR=$RED;
elif [ "$RISK_INDEX" -ge 65 ] 2>/dev/null; then RISK_COLOR=$RED;
elif [ "$RISK_INDEX" -ge 30 ] 2>/dev/null; then RISK_COLOR=$ORANGE;
else                                             RISK_COLOR=$GREEN;
fi

# -- Sintaxis --
log ""
log "${BOLD}  --- RESULTADO DE SINTAXIS ---${RESET}"
if [ "$SYNTAX_OK" = "true" ]; then
  log "${GREEN}  OK Sintaxis valida${RESET}"
  log "${DIM}     ${SYNTAX_OUT}${RESET}"
else
  log "${RED}  X ERROR DE SINTAXIS DETECTADO${RESET}"
  if [ -n "$SYNTAX_LINE" ]; then
    log "${RED}${BOLD}     Linea del error : ${SYNTAX_LINE}${RESET}"
  fi
  log "${RED}  Detalle completo del error:${RESET}"
  # Mostrar TODAS las lineas del error
  while IFS= read -r line; do
    log "${RED}     | ${line}${RESET}"
  done <<< "$SYNTAX_OUT"
  log ""
  log "${RED}${BOLD}  ADVERTENCIA: Un error de sintaxis en un hook PHP derrumba todo${RESET}"
  log "${RED}  el servidor porque los hooks se ejecutan en modo sincrono.${RESET}"
fi

# -- Metricas --
log ""
log "${BOLD}  ===========================================${RESET}"
log "${BOLD}         REPORTE DE RIESGO                 ${RESET}"
log "${BOLD}  ===========================================${RESET}"
log ""
log "  Indice de Riesgo    : ${RISK_COLOR}${BOLD}${RISK_INDEX}% (${RISK_LEVEL})${RESET}"
log "  Peligrosidad codigo : ${BOLD}${DANGER_SCORE}%${RESET}"
log "  Banderas detectadas : ${BOLD}${FLAGS_COUNT}${RESET}"
log ""

# -- Banderas de peligro --
if [ -n "$FLAGS_TEXT" ] && [ "$FLAGS_COUNT" -gt 0 ] 2>/dev/null; then
  log "${RED}${BOLD}  Banderas de Peligro Detectadas:${RESET}"
  while IFS= read -r flag; do
    [ -n "$flag" ] && log "${RED}    ! ${flag}${RESET}"
  done <<< "$FLAGS_TEXT"
  log ""
else
  log "${GREEN}  Sin banderas de peligro en el codigo.${RESET}"
  log ""
fi

# -- Veredicto Final --
log "${BOLD}  ===========================================${RESET}"
case "$VERDICT" in
  "APROBADO")
    log "${GREEN}${BOLD}  VEREDICTO: APROBADO (Riesgo ${RISK_LEVEL})${RESET}";;
  "REVISAR CON CUIDADO")
    log "${ORANGE}${BOLD}  VEREDICTO: REVISAR CON CUIDADO (Riesgo ${RISK_LEVEL})${RESET}";;
  "REQUIERE REVISION INMEDIATA")
    log "${RED}${BOLD}  VEREDICTO: REQUIERE REVISION INMEDIATA (Riesgo ${RISK_LEVEL})${RESET}";;
  *)
    log "${RED}${BOLD}  VEREDICTO: RECHAZADO${RESET}";;
esac
log "${BOLD}  ===========================================${RESET}"
log ""
log "${DIM}  Log completo guardado en: ${LOG_FILE}${RESET}"
log ""

# Codigo de salida
if [ "$VERDICT" = "APROBADO" ] || [ "$VERDICT" = "REVISAR CON CUIDADO" ]; then
  exit 0
else
  exit 10
fi