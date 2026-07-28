# PHP QA Inspector

Orquestador local de agentes QA para análisis de código PHP, con un LLM (local vía Ollama, o en la nube vía NVIDIA NIM) haciendo el análisis. Pensado originalmente para el sistema de RRHH interno CAPA 8 / SistemaColaboradores, pero el pipeline no depende de nada específico de ese proyecto salvo el schema de base de datos opcional (ver más abajo).

## Qué hace

Dado un directorio con código PHP, permite:

- **Prueba de escritorio** (`/api/qa/desktop-test`): pipeline fijo que parte el archivo en bloques (por función o por rango de líneas) y los analiza uno por uno, con reintento recursivo por partición si un bloque falla.
- **Agente de QA** (`/api/qa/agent-test`): un loop de tool-calling (ReAct) donde el modelo decide qué necesita investigar — listar funciones, leer una función o un bloque de líneas, verificar sintaxis, buscar definiciones, consultar el schema de una tabla, calcular impacto — en vez de recibir el código servido de antemano. Tiene una compuerta programática que obliga a usar ciertas herramientas antes de aceptar un veredicto final, y nunca acepta una respuesta vacía como definitiva.
- **Orquestador multi-agente** (`/api/qa/multi-agent-test`): analiza varios archivos EN PARALELO (requiere `provider: "nvidia"` para que el paralelismo sea real) y agrega un agente coordinador que sintetiza riesgos que solo aparecen al combinar archivos — usando el grafo de dependencias real del proyecto (includes + tablas de BD compartidas), no adivinado por el LLM.
- **Análisis de impacto** (`/api/qa/impact`): qué otros archivos dependen de uno dado (por include/require) o comparten una tabla de base de datos con él.

Todos los endpoints de análisis transmiten progreso en vivo (streaming) con un protocolo simple de texto: líneas `__HB__:` para heartbeats/progreso y una línea final `__GATE__:{json}` con el veredicto estructurado (aprobado / requiere confirmación / bloqueado), que el frontend interpreta para mostrar una barra de estado.

## Proveedores de LLM

Convive Ollama local con NVIDIA NIM en la nube — se elige por request, ninguno reemplaza al otro:

| | Ollama (local) | NVIDIA NIM (nube) |
|---|---|---|
| Costo | Gratis | Requiere API key de [build.nvidia.com](https://build.nvidia.com) |
| Velocidad | Limitada por tu GPU, una request a la vez | Más rápida, permite paralelismo real entre archivos |
| Uso recomendado | Pruebas rápidas, sin conexión | Orquestador multi-agente, modelos más grandes |

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

Al abrir `http://127.0.0.1:8000` en el navegador vas a poder configurar el directorio PHP a analizar desde la pestaña de configuración (por defecto apunta a una ruta que solo existe en la máquina original de desarrollo).

## Dos archivos que necesitas generar tú (no vienen en el repo)

Estos dos se excluyeron deliberadamente de git porque contienen información específica de la base de datos/código que se esté analizando — no tiene sentido publicarlos junto con la herramienta.

### `backend/knowledge/db_schema.json` (opcional, pero recomendado)

Si existe, el agente lo usa para dos cosas: inyectar contexto real de schema (columnas, notas, relaciones) en el prompt cuando el código referencia una tabla conocida, y responder la herramienta `consultar_schema_tabla`. Sin este archivo, el pipeline sigue funcionando normalmente — solo pierde ese contexto extra y el agente coordinador del modo multi-agente tiene menos con qué razonar sobre riesgos cruzados entre archivos.

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
  "reglas_negocio": ["..."],
  "query_tips": ["..."],
  "advertencias_criticas": ["..."]
}
```

Solo se necesitan las tablas que realmente te interese que el modelo entienda a fondo — no hace falta documentar el 100% del schema.

### `qa_history/*.json` (se genera solo)

Es donde se guarda el historial de resultados de cada prueba de escritorio, por archivo analizado (usa un hash del filepath como nombre). Se crea automáticamente en la primera prueba que corras — no necesitas crear nada a mano, solo ten presente que esta carpeta va a acumular archivos con rutas locales y fragmentos del código que analices, por eso queda fuera del repo.

## Seguridad

- Todas las rutas de archivo se validan contra el directorio PHP configurado con `os.path.commonpath` (no un simple `startswith`) antes de leer nada — evita path traversal vía `../` o directorios hermanos con nombre similar.
- La API key de NVIDIA nunca toca disco ni se refleja en ningún endpoint.
- **Pendiente / conocido**: no hay autenticación en ningún endpoint y CORS está abierto (`allow_origins=["*"]`) — pensado para uso local en red de confianza, no para exponerse directamente a internet.

## Estructura

```
backend/
  main.py              # FastAPI app, endpoints
  agent_loop.py         # Agente de tool-calling (ReAct)
  desktop_test.py        # Pipeline fijo por bloques + reintento recursivo
  multi_agent.py         # Orquestador multi-archivo + agente coordinador
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
```
