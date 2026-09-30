import asyncio
import json
import time

from config import settings
from dependency_graph import build_dependency_graph, get_table_impact
from agent_loop import run_agent_analysis
from llm_client import call_llm_chat
from schema_context import get_table_schema


async def _run_and_collect(filepath: str, model_name: str, provider: str):
    """Corre run_agent_analysis para un archivo y devuelve (filepath, texto
    completo) — sin heartbeats individuales, esos se manejan de forma
    agregada en run_multi_file_analysis para no complicar el streaming con
    varias tareas escribiendo progreso a la vez."""
    accumulated = []
    try:
        async for chunk in run_agent_analysis(filepath, model_name, provider=provider):
            if not chunk.startswith("__HB__:"):
                accumulated.append(chunk)
    except Exception as e:
        accumulated.append(f"\n\n❌ Error analizando {filepath}: {e}\n")
    return filepath, "".join(accumulated)


def _basename(filepath: str) -> str:
    return filepath.replace("\\", "/").split("/")[-1]


async def run_multi_file_analysis(filepaths: list, model_name: str, provider: str = "nvidia"):
    """Orquestador multi-agente real: analiza varios archivos EN PARALELO
    (asyncio.gather) — en la práctica solo vale la pena con provider="nvidia",
    ya que la GPU local de Ollama sirve una request a la vez de todos modos —
    y al final una llamada de agente coordinador sintetiza el impacto
    cruzado entre ellos, usando el grafo de dependencias determinista que ya
    existe (build_dependency_graph/get_table_impact), no adivinado por el LLM.
    Mismo protocolo __HB__/__GATE__ que el resto del proyecto."""
    yield f"_Analizando {len(filepaths)} archivo(s) en paralelo con el proveedor '{provider}'..._\n\n---\n\n"

    tasks = [asyncio.create_task(_run_and_collect(fp, model_name, provider)) for fp in filepaths]
    start_time = time.monotonic()
    heartbeat_interval = 5.0

    while not all(t.done() for t in tasks):
        done_count = sum(1 for t in tasks if t.done())
        elapsed = int(time.monotonic() - start_time)
        yield f"__HB__:Analizando {len(filepaths)} archivo(s) en paralelo... ({done_count}/{len(filepaths)} terminados, {elapsed}s transcurridos)\n"
        await asyncio.sleep(heartbeat_interval)

    pairs = await asyncio.gather(*tasks)
    results = dict(pairs)

    for fp in filepaths:
        yield f"\n\n## 📄 {_basename(fp)}\n\n{results.get(fp, '(sin resultado)')}\n\n---\n\n"

    # --- Impacto determinista real entre los archivos analizados (sin LLM,
    # reutiliza el grafo de dependencias ya construido en Fase 2) ---
    adj_includes, adj_included_by, file_tables, _ = build_dependency_graph(settings.php_dir)
    cross_file_impact = {}
    for fp in filepaths:
        my_tables, related = get_table_impact(fp, file_tables)
        if related:
            cross_file_impact[_basename(fp)] = {
                "tablas": sorted(my_tables),
                "archivos_relacionados": {tbl: [_basename(p) for p in files] for tbl, files in related.items()}
            }

    # Además del nombre de la tabla compartida, se busca su schema real
    # (columnas, FKs, notas) en knowledge/db_schema.json — sin esto el
    # coordinador solo sabe QUE dos archivos comparten una tabla, no CÓMO se
    # relacionan sus columnas entre sí (ej. una FK), que es lo que realmente
    # le permite razonar sobre un riesgo cruzado en vez de responder que le
    # falta información (confirmado que esto pasaba en pruebas reales).
    tablas_compartidas = sorted({
        tbl for info in cross_file_impact.values() for tbl in info["archivos_relacionados"]
    })
    schema_tablas_compartidas = {}
    for tbl in tablas_compartidas:
        info = get_table_schema(tbl)
        if info:
            schema_tablas_compartidas[tbl] = {
                "desc": info.get("desc", ""),
                "columnas": info.get("columnas", {}),
                "notas": info.get("notas", []),
            }

    yield "\n\n**Sintetizando impacto cruzado entre archivos...**\n\n"
    coordinator_prompt = (
        "Eres un Agente Coordinador de QA. Se analizaron varios archivos PHP por separado, cada uno con su "
        "propio veredicto (arriba). A continuación tienes datos REALES (no adivinados, calculados por "
        "código) de qué tablas de base de datos comparten entre sí, más el schema real de esas tablas "
        "(columnas, llaves foráneas, notas) para que puedas razonar sobre la relación real entre ellas.\n\n"
        "Tu tarea es identificar riesgos que SOLO aparecen al combinar archivos — por ejemplo, si un "
        "archivo escribe en una tabla sin validar un campo, y otro archivo lee esa misma tabla (o una "
        "tabla relacionada por llave foránea) asumiendo que ese campo siempre es válido. NO repitas los "
        "hallazgos individuales ya reportados por archivo — concéntrate únicamente en riesgos que surgen "
        "de la combinación entre archivos.\n\n"
        f"Impacto real por tabla compartida:\n{json.dumps(cross_file_impact, ensure_ascii=False, indent=2)}\n\n"
        f"Schema real de las tablas compartidas:\n{json.dumps(schema_tablas_compartidas, ensure_ascii=False, indent=2)}\n\n"
        "Responde en español, en Markdown, con una sección '### 🔗 Riesgos Cruzados Entre Archivos'. Si "
        "genuinamente no hay ningún riesgo cruzado real (por ejemplo, si no comparten ninguna tabla, o el "
        "schema no sugiere ninguna interacción riesgosa), dilo explícitamente en vez de inventar uno."
    )
    coord_result = None
    async for item in call_llm_chat(
        [{"role": "user", "content": coordinator_prompt}], model_name, "Coordinador",
        provider=provider, timeout=280.0
    ):
        if isinstance(item, dict):
            coord_result = item
        else:
            yield item

    coord_text = ""
    if coord_result and coord_result.get("message"):
        coord_text = coord_result["message"].get("content") or ""
        yield "\n\n" + coord_text + "\n"
    elif coord_result and coord_result.get("error"):
        yield f"\n\n(No se pudo generar la síntesis cruzada: {coord_result['error']})\n"

    yield f"\n\n__GATE__:{json.dumps({'gate_status': 'requiere_confirmacion' if cross_file_impact else 'aprobado', 'fallas_criticas': [], 'fallas_medias': [], 'total_casos': len(filepaths)}, ensure_ascii=False)}\n"
    yield "\n\n✅ **Análisis multi-agente completo.**\n"
