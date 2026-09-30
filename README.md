# Orquestador de Agentes QA para PHP (local, Mac)

Herramienta de análisis de código PHP que corre 100% local usando Ollama
(sin costos de API externos). Analiza sintaxis, riesgo de seguridad,
impacto en el resto del proyecto, y simula un agente de QA con
tool-calling que investiga el archivo antes de dar un veredicto.

## Requisitos

- **macOS con Apple Silicon** (probado en M1 Pro, 16GB RAM). El hardware
  importa: ver la sección "Notas de hardware" abajo.
- **Ollama** instalado y corriendo (`ollama serve`), con al menos un modelo
  con capability `tools` descargado. Recomendado: `qwen2.5-coder:14b`
  (ver más abajo por qué).
- **Python 3.10+** con `venv`.
- **PHP 8.x + Composer** instalados (`brew install php composer`). Se
  usan para: (a) `php -l` como linter de sintaxis, y (b) el puente de AST
  real en `backend/php_ast_bridge/` (ver esa sección).
- **Node.js** (para `node --check`, usado como linter de sintaxis JS).

## Arranque rápido

```bash
./run.sh
```

Esto crea el entorno virtual si no existe, instala dependencias de Python,
y levanta el backend en `http://127.0.0.1:8000` (que también sirve el
frontend). Presiona `Ctrl+C` para detener.

Si es la primera vez que usas el puente de AST de PHP, instala sus
dependencias una sola vez:

```bash
cd backend/php_ast_bridge && composer install
```

## Configuración

Edita `backend/config.py`:

- `default_model`: el modelo de Ollama que se usa por defecto. Debe existir
  en `ollama list` y tener capability `tools` (`ollama show <modelo>` para
  confirmar). **No usar modelos que rebasen tu RAM disponible** — ver nota
  de hardware abajo.
- `DEFAULT_PHP_DIR`: la carpeta con el proyecto PHP a analizar. Se configura
  con la variable de entorno `QA_AGENT_PHP_DIR` (no hardcodeada en el repo):
  ```bash
  export QA_AGENT_PHP_DIR="/ruta/a/tu/proyecto/php"
  ```
  Sin esa variable, cae a `os.getcwd()` (el directorio desde donde se lanza
  el proceso).
- `DEFAULT_NUM_CTX`: ventana de contexto por llamada a Ollama. Debe
  mantenerse **constante** en todo el backend — cambiar este valor entre
  llamadas fuerza a Ollama a recargar el modelo completo en RAM (~50s de
  penalización, confirmado empíricamente). Todo el código importa este
  valor desde `config.py`; no hardcodear `num_ctx` en otro archivo.

Opcional: `backend/knowledge/db_hosts.json` — clasifica hosts/IPs conocidos
como producción o desarrollo, para que el detector de peligro pueda avisar
si un archivo con una consulta destructiva se conecta a producción. Si no
se llena, el detector usa patrones genéricos (`prod`/`dev`/`test`/etc.) en
las palabras del host, así que sigue funcionando sin este archivo.

Ambos archivos contienen información sensible de infraestructura/negocio
real y están en `.gitignore` — cada uno tiene un `.json.example` junto a
él como plantilla. Para configurarlos:

```bash
cp backend/knowledge/db_hosts.json.example backend/knowledge/db_hosts.json
cp backend/knowledge/db_schema.json.example backend/knowledge/db_schema.json
# luego edita ambos con los datos reales de tu proyecto
```

`db_schema.json` es el contexto de negocio que el LLM usa para razonar
sobre nombres de tabla/columna reales, "trampas" conocidas (columnas con
valores invertidos, nombres con mayúsculas irregulares, etc.) — mientras
más completo, mejor calidad de análisis.

## Notas de hardware (M1 Pro / 16GB — ajustar si tu equipo es distinto)

Confirmado empíricamente en este equipo:

- `qwen2.5-coder:7b` corre 100% GPU, ~5GB RAM, respuesta en caliente ~1.4s.
- `qwen2.5-coder:14b` con `num_ctx=8192` corre 100% GPU, ~10GB RAM,
  respuesta en caliente ~5.3s — el punto elegido como default.
- `qwen2.5-coder:14b` con `num_ctx=16384` empieza a ceder cómputo a CPU
  (91%/9% GPU/CPU) porque se acerca al límite de RAM unificada disponible.
- Modelos que requieren más RAM de la disponible (ej. un modelo MoE de
  ~22GB en un equipo de 16GB) pueden tardar **varios minutos** en una
  pregunta trivial, por swap de memoria — si ves esto, es la RAM, no un
  bug del código.

Si migras esto a un equipo con más RAM, sube `DEFAULT_NUM_CTX` gradualmente
y repite esta misma prueba (medir `load_duration`/`eval_duration` de la
respuesta cruda de Ollama) antes de asumir que un valor más alto es mejor.

## Arquitectura del backend

```
backend/
  main.py              FastAPI: todos los endpoints HTTP
  config.py            Settings (modelo, ruta, num_ctx)
  agent_loop.py         Agente con tool-calling (investiga antes de veredicto)
  ollama_client.py      Cliente HTTP a Ollama con streaming y heartbeat
  code_segmentation.py  Parte archivos grandes en bloques para caber en contexto
  danger_flags.py       Detector de peligro: regex + AST/taint (ver abajo)
  ast_bridge.py         Puente Python -> PHP (AST real, taint analysis)
  sarif_export.py       Exporta hallazgos a formato SARIF 2.1.0
  dependency_graph.py   Grafo de includes + tablas de BD compartidas
  schema_context.py     Contexto de negocio inyectado (columnas, typos conocidos)
  test_matrix.py        Parsea la matriz de casos del LLM y clasifica severidad
  qa_history.py         Persistencia: historial de corridas + falsos positivos
  php_ast_bridge/        Proyecto PHP standalone (composer) con nikic/php-parser
```

### Detector de peligro (`danger_flags.py`): dos capas complementarias

1. **Regex sobre texto** (rápido, amplio, pero no distingue comentarios de
   código real, ni sigue variables entre líneas).
2. **AST real vía `php_ast_bridge/`** (más lento por invocar un subprocess
   PHP, pero preciso): ignora comentarios por construcción, y hace *taint
   analysis* básico — sigue una variable desde `$_GET`/`$_POST`/etc. hasta
   una consulta SQL o ejecución de comando, aunque pase por 1+ asignaciones
   intermedias en líneas separadas.

Cuando el AST está disponible, se usa como fuente de verdad para el
chequeo de "DELETE/UPDATE sin WHERE" (evita el falso positivo de detectar
código dentro de un bloque `/* comentado */`). Si el AST falla (PHP no
instalado, o error de sintaxis real en el archivo), el regex sigue
funcionando solo — nunca es un punto único de falla.

### Falsos positivos

Los hallazgos que vienen del AST (tienen `category` + `line` reales) se
pueden marcar como falso positivo confirmado desde la UI (tab "Impacto y
Riesgo"). Se guarda en el mismo `qa_history/<hash>.json` del archivo, y
solo silencia ESE hallazgo específico para ESE archivo — el mismo patrón
sigue detectándose normalmente en cualquier otro archivo.

Endpoints: `GET/POST /api/qa/false-positives`.

### Export SARIF

`GET /api/qa/sarif?filepath=...` devuelve un documento SARIF 2.1.0 con los
hallazgos del AST (los de regex sin línea concreta, como host de
producción o credenciales, no se incluyen porque SARIF exige una línea
real). Compatible con GitHub Code Scanning, GitLab, Azure DevOps. Sirve
tanto al botón "Exportar SARIF" de la UI como a llamadas directas desde
CI/CD (`curl .../api/qa/sarif?filepath=... -o resultado.sarif`).

### Grafo de impacto: directo, no transitivo

El índice de riesgo y las listas de "archivos afectados" reflejan solo
**nivel 1** (archivos que directamente incluyen o son incluidos por el
archivo analizado, o comparten una tabla de base de datos) — no la cadena
completa de dependencias transitivas. Decisión de producto (ver
`main.py`, función `get_impact_analysis`): más señal, menos ruido.

## `backend/php_ast_bridge/` — qué es y cómo reinstalarlo

Proyecto PHP independiente (con su propio `composer.json`/`composer.lock`)
que expone `parse_ast.php`: recibe código PHP por stdin, devuelve un AST
simplificado + taint analysis en JSON por stdout. `backend/ast_bridge.py`
lo invoca vía `subprocess`.

Si clonas este proyecto en otro equipo, o si `vendor/` se borra:

```bash
cd backend/php_ast_bridge
composer install
```

Prueba manual rápida:

```bash
echo '<?php $x = $_GET["id"]; mysqli_query($c, "DELETE FROM t WHERE id=".$x);' \
  | php backend/php_ast_bridge/parse_ast.php | python3 -m json.tool
```

Debe mostrar `"tainted": true` en el argumento de `mysqli_query`.

## Scripts auxiliares

- **`run.sh`**: arranque local (equivalente Mac de `run.bat`, que es un
  vestigio de una versión anterior en Windows y ya no aplica aquí).
- **`qa_inspect.sh`**: cliente de línea de comandos para análisis ESTÁTICO
  (sin LLM, vía `/api/qa/inspect-content`) de un archivo a través de un
  túnel remoto (ngrok, SSH, Tailscale). Configurable con
  `QA_INSPECT_HOST`/`QA_INSPECT_PORT` y, si el servidor tiene `QA_API_KEY`
  configurada, con `QA_API_KEY` en el entorno de quien lo corre.
- **`qa_agent_inspect.sh`**: cliente de línea de comandos para el AGENTE
  CON LLM real (vía `/api/qa/agent-test`), a diferencia de `qa_inspect.sh`.
  Resuelve el archivo por nombre vía `/api/files`, corre el agente
  (puede tardar varios minutos, sobre todo con NVIDIA), y reporta el
  veredicto del gate con código de salida (0=aprobado, 10=requiere
  confirmación, 11=bloqueado/error). Uso:
  `./qa_agent_inspect.sh nombre_archivo.php [ollama|nvidia] [modelo]`.
  Configurar `QA_URL` al inicio del script según cómo se acceda al backend
  (túnel SSH inverso, Tailscale, ngrok).

## Proveedores de LLM: Ollama (local) y NVIDIA NIM (nube)

Todo el backend habla con el LLM a través de `llm_client.call_llm_chat`,
que despacha a `ollama_client.py` (local) o `nvidia_client.py` (nube, API
compatible con OpenAI) según el parámetro `provider` que llega en cada
request (`"ollama"` por defecto, o `"nvidia"`).

Para usar NVIDIA, exporta la variable de entorno antes de arrancar el
backend (nunca se guarda en `config.py` ni se expone por ningún endpoint):

```bash
export NVIDIA_API_KEY="tu-api-key-de-build.nvidia.com"
```

Sin esa variable, cualquier request con `provider: "nvidia"` falla con un
mensaje claro en vez de un error críptico.

**Diferencia de comportamiento**: con Ollama, la ruta de "archivo chico"
(`/api/qa/desktop-test` sin segmentación) transmite el texto token por
token en vivo. Con NVIDIA, esa misma ruta espera la respuesta completa y
la entrega de una sola vez al final (con heartbeats mientras tanto) — es
una limitación de cómo NVIDIA/OpenAI expone su streaming frente a cómo lo
usa este proyecto, no un bug.

El orquestador multi-agente (`/api/qa/multi-agent-test`, no expuesto en la
UI) analiza varios archivos en paralelo real — solo tiene sentido con
`provider="nvidia"`, ya que con Ollama local los agentes se siguen
sirviendo uno a la vez de todos modos (una sola GPU).

## Autenticación opcional por API key

El servidor puede exigir un header `X-API-Key` en toda ruta `/api/` si se
configura la variable de entorno `QA_API_KEY` antes de arrancar:

```bash
export QA_API_KEY="una-clave-larga-y-aleatoria"
```

Sin esa variable, el comportamiento es el de siempre — sin autenticación,
pensado para uso puramente local (`localhost`). Configúrala en cuanto el
backend deje de ser solo-localhost (por ejemplo si se expone por un túnel
SSH inverso o ngrok) — cualquiera con la URL puede leer el código fuente
bajo el directorio PHP configurado si no hay ninguna autenticación.

## Historial y datos persistidos

`qa_history/` guarda, por archivo analizado (hash del path real), hasta 10
corridas recientes y los falsos positivos confirmados. Es información
local de este equipo — no se sincroniza ni se sube a ningún servidor
externo. Considera excluir esta carpeta de git si el repo se comparte
públicamente (ver `.gitignore`).
