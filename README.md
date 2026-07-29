# PHP QA Inspector

Orquestador local de un agente QA para análisis de código PHP, con un LLM (local vía Ollama, o en la nube vía NVIDIA NIM) haciendo el análisis real del código — no reglas fijas. Nació como herramienta interna para un sistema de RRHH, pero el pipeline no depende de nada específico de ese proyecto: funciona contra cualquier directorio de código PHP, con o sin el archivo de conocimiento opcional descrito más abajo.

## Qué hace

Dado un directorio con código PHP, permite:

- **Agente de QA** (`/api/qa/agent-test`, un archivo a la vez): un loop de tool-calling (ReAct) donde el modelo decide qué necesita investigar — listar funciones, leer una función o un bloque de líneas, verificar sintaxis, buscar definiciones, consultar el schema de una tabla, calcular impacto — en vez de recibir el código servido de antemano. Tiene una compuerta programática que obliga a usar ciertas herramientas antes de aceptar un veredicto final, nunca acepta una respuesta vacía como definitiva, y al final genera automáticamente un diagrama de flujo Mermaid del archivo. El veredicto incluye una sección obligatoria donde el modelo evalúa explícitamente autorización/pertenencia, concurrencia, transiciones de estado y cálculos numéricos/de fecha — no solo lo obvio (inyección SQL, funciones no definidas).
- **Análisis de impacto** (`/api/qa/impact`): qué otros archivos dependen de uno dado (por include/require) o comparten una tabla de base de datos con él.
- **Prueba de escritorio** (`/api/qa/desktop-test`): pipeline fijo más antiguo que parte el archivo en bloques y los analiza uno por uno, con reintento recursivo por partición si un bloque falla. Sigue funcionando pero ya no está enlazado en la UI principal — el agente de arriba es más confiable y lo reemplaza en la práctica.
- **Orquestador multi-agente** (`/api/qa/multi-agent-test`, no expuesto en la UI): analiza varios archivos EN PARALELO (requiere `provider: "nvidia"` para que el paralelismo sea real) y agrega un agente coordinador que sintetiza riesgos que solo aparecen al combinar archivos, usando el grafo de dependencias real del proyecto. Deliberadamente no tiene botón en el frontend — correr muchos archivos a la vez sin control puede pegar contra rate limits del proveedor o saturar una GPU local. Sigue disponible vía API directa si lo necesitas puntualmente.

Todos los endpoints de análisis transmiten progreso en vivo (streaming) con un protocolo simple de texto: líneas `__HB__:` para heartbeats/progreso y una línea final `__GATE__:{json}` con el veredicto estructurado (aprobado / requiere confirmación / bloqueado), que el frontend interpreta para mostrar una barra de estado.

## Proveedores de LLM

Convive Ollama local con NVIDIA NIM en la nube — se elige por request desde un dropdown en la UI (con modelos preseleccionados ya probados), ninguno reemplaza al otro:

| | Ollama (local) | NVIDIA NIM (nube) |
|---|---|---|
| Costo | Gratis | Requiere API key de [build.nvidia.com](https://build.nvidia.com) |
| Velocidad | Limitada por tu GPU, una request a la vez | Más rápida |
| Uso recomendado | Pruebas rápidas, sin conexión | Análisis del día a día — más confiable con modelos grandes |

La API key de NVIDIA se lee **únicamente** de la variable de entorno `NVIDIA_API_KEY` — nunca se guarda en archivos de configuración ni se expone por ningún endpoint (`GET /api/config` solo devuelve un booleano indicando si está configurada).

```powershell
# Antes de iniciar el servidor, en la misma terminal:
[Environment]::SetEnvironmentVariable("NVIDIA_API_KEY", "nvapi-...", "User")
```

## Instalación y arranque

Requiere Python 3.11+ y, si vas a usar el proveedor local, [Ollama](https://ollama.com) corriendo con al menos un modelo descargado (ej. `ollama pull hermes3`).

En Windows, lo más simple es correr `run.bat` — crea el entorno virtual, instala dependencias y levanta el servidor en `http://127.0.0.1:8000`.

Manual:

```bash
python -m venv .venv
.venv\Scripts\activate       # o `source .venv/bin/activate` en Mac/Linux
pip install -r backend/requirements.txt
python -m uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000 --reload
```

Al abrir `http://127.0.0.1:8000` en el navegador vas a poder configurar el directorio PHP a analizar desde la pestaña de configuración. Por defecto usa el directorio desde donde se ejecuta el servidor — para fijar otro sin pasar por la UI cada vez, exporta `QA_PHP_DIR` antes de iniciar.

## Autenticación (opcional, recomendada si expones el servidor más allá de localhost)

Por defecto no hay ninguna autenticación — pensado para uso puramente local. Si vas a exponer el servidor a otra máquina (red local, túnel SSH, Tailscale), configura `QA_API_KEY` como variable de entorno **antes** de iniciar el servidor:

```powershell
[Environment]::SetEnvironmentVariable("QA_API_KEY", "elige-una-clave-larga-aleatoria", "User")
```

Con esto configurado, todas las rutas `/api/` exigen el header `X-API-Key` con esa misma clave — cualquier request sin ella (o con una distinta) recibe HTTP 401. Igual que `NVIDIA_API_KEY`, nunca se guarda en archivos ni se expone por ningún endpoint.

## Clientes para servidores remotos (SSH)

Dos scripts en la raíz del repo, pensados para correr desde otra máquina (ej. tu servidor de producción) contra este backend:

- **`qa_inspect.sh`**: manda el *contenido* del archivo tal cual vive en esa máquina (no necesita que el archivo exista en el servidor que corre el backend) — corre solo análisis estático instantáneo (sintaxis + banderas de peligro por patrones), sin LLM.
- **`qa_agent_inspect.sh`**: llama al agente con LLM real. A diferencia del anterior, busca el archivo por nombre en el `php_dir` configurado en el backend (el agente lee del disco de esa máquina, no del servidor remoto) — más lento (minutos, no segundos), pero es el análisis real con veredicto y matriz de pruebas.

Ambos soportan `QA_API_KEY` (ver arriba) y están comentados con las opciones de conexión (túnel SSH inverso, Tailscale, o ngrok como respaldo).

## Dos archivos que necesitas generar tú (no vienen en el repo)

Estos dos se excluyeron deliberadamente de git porque contienen información específica de la base de datos/código que se esté analizando — no tiene sentido publicarlos junto con la herramienta. Ambos son completamente opcionales: sin ellos, el agente sigue funcionando, solo con menos contexto.

### `backend/knowledge/db_schema.json` (opcional, pero recomendado)

Si existe, el agente lo usa para tres cosas: inyectar contexto real de schema (columnas, notas, relaciones) cuando el código referencia una tabla conocida, responder la herramienta `consultar_schema_tabla`, y — la parte más valiosa para encontrar bugs de lógica de negocio, no solo técnicos — verificar el código contra reglas de negocio documentadas (fórmulas, límites, validaciones esperadas) que de otra forma serían invisibles con solo leer el código.

Formato esperado:

```json
{
  "_meta": { "sistema": "...", "actualizado": "2026-01-01" },
  "tablas": {
    "modulos.tblEjemplo": {
      "desc": "Descripción breve de la tabla",
      "columnas": { "id": "INT - PK", "colaboradorId": "INT - FK -> modulos.tblColaborador.id" },
      "notas": ["Cualquier trampa o convención no obvia, ej. una columna invertida"],
      "relaciones": { "otraTabla": "descripción de la relación" }
    }
  },
  "reglas_negocio": {
    "nombre_de_la_regla": {
      "desc": "Explica la regla en texto libre",
      "cualquier_detalle_estructurado": "formulas, límites, tablas de valores — la forma es libre, se pasa tal cual al modelo"
    }
  },
  "query_tips": ["..."],
  "advertencias_criticas": ["..."]
}
```

Solo se necesitan las tablas y reglas que realmente te interese que el modelo entienda a fondo — no hace falta documentar el 100% del sistema. `reglas_negocio` no tiene una forma fija (puede ser una lista simple de strings, o un dict anidado como el ejemplo) — se serializa tal cual al prompt.

### `qa_history/*.json` (se genera solo)

Es donde se guarda el historial de resultados de cada prueba de escritorio, por archivo analizado (usa un hash del filepath como nombre). Se crea automáticamente en la primera prueba que corras — no necesitas crear nada a mano, solo ten presente que esta carpeta va a acumular archivos con rutas locales y fragmentos del código que analices, por eso queda fuera del repo.

## Seguridad

- Todas las rutas de archivo se validan contra el directorio PHP configurado con `os.path.commonpath` (no un simple `startswith`) antes de leer nada — evita path traversal vía `../` o directorios hermanos con nombre similar.
- Las API keys (NVIDIA, QA_API_KEY) nunca tocan disco ni se reflejan en ningún endpoint.
- Autenticación por API key es **opcional** (ver arriba) — actívala si el servidor deja de ser solo-localhost.
- **Pendiente / conocido**: CORS está abierto (`allow_origins=["*"]` + `allow_credentials=True`) — razonable para uso local, pero vale la pena restringirlo si además activas la API key.

## Estructura

```
backend/
  main.py              # FastAPI app, endpoints, middleware de auth opcional
  agent_loop.py         # Agente de tool-calling (ReAct) + diagrama de flujo
  desktop_test.py        # Pipeline fijo por bloques + reintento recursivo (legacy, sin UI)
  multi_agent.py         # Orquestador multi-archivo + agente coordinador (sin UI)
  ollama_client.py       # Cliente Ollama
  nvidia_client.py       # Cliente NVIDIA NIM (OpenAI-compatible)
  llm_client.py          # Dispatcher de proveedor
  dependency_graph.py    # Grafo de includes + tablas compartidas entre archivos
  schema_context.py       # Carga y consulta de knowledge/db_schema.json
  danger_flags.py         # Heurísticas estáticas (funciones peligrosas, etc.)
  code_segmentation.py    # Partición de código PHP en bloques/funciones
  test_matrix.py           # Parseo de la matriz de casos de prueba del veredicto
  qa_history.py            # Persistencia de resultados por archivo
frontend/
  index.html, app.js      # UI de una sola página, sin build step
qa_inspect.sh              # Cliente SSH: análisis estático por contenido
qa_agent_inspect.sh        # Cliente SSH: agente con LLM real
```
