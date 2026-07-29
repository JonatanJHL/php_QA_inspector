import os
import re
import json


# --- Schema de BD para contexto en prompts (se carga una vez al arrancar) ---
_DB_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "knowledge", "db_schema.json")
_db_schema: dict = {}
try:
    with open(_DB_SCHEMA_PATH, "r", encoding="utf-8") as _f:
        _db_schema = json.load(_f)
except Exception:
    pass  # Si no existe el archivo, el sistema sigue funcionando sin schema


def get_reglas_negocio() -> list:
    """Reglas de negocio documentadas en knowledge/db_schema.json (sección
    opcional `reglas_negocio`) — devuelve lista vacía si el archivo no
    existe o no tiene esa sección, sin error, para que el agente funcione
    igual de bien en proyectos que no tengan este archivo de conocimiento
    (no todos los sistemas van a tener uno). Sin esto, el agente solo puede
    detectar violaciones de lógica de negocio que sean obvias leyendo el
    código — nunca las que dependen de una regla que no está escrita en
    ningún lado del código mismo."""
    return _db_schema.get("reglas_negocio", [])


def get_table_schema(tabla: str):
    """Lookup puntual de una sola tabla por nombre (con o sin prefijo de
    schema, ej. 'tblColaborador' o 'modulos.tblColaborador'). A diferencia de
    build_schema_context (que escanea código para inyectar contexto de varias
    tablas a la vez), esto es para consultas dirigidas — ej. la herramienta
    consultar_schema_tabla del agente. Devuelve None si no se encuentra."""
    tablas = _db_schema.get("tablas", {})
    if tabla in tablas:
        return tablas[tabla]
    for key, info in tablas.items():
        if key.split(".")[-1].lower() == tabla.split(".")[-1].lower():
            return info
    return None


def build_schema_context(code: str) -> str:
    """Detecta las tablas referenciadas en el código y devuelve un bloque
    de contexto compacto con su schema real para inyectar en el prompt de Hermes.
    Solo incluye las tablas que realmente aparecen en el código.
    Nunca expone datos reales — solo estructura (nombres de columnas, tipos, notas)."""
    if not _db_schema:
        return ""

    tablas = _db_schema.get("tablas", {})

    # Busca referencias con prefijo de schema: modulos.tblXxx o whmcs81a.tblXxx
    refs_con_prefijo = set(
        f"{db}.{tbl}"
        for db, tbl in re.findall(r'\b(modulos|whmcs81a)\.(tbl[A-Za-z0-9_]+)\b', code, re.IGNORECASE)
    )
    # Busca nombres de tabla sueltos: tblXxx o tbl_xxx
    refs_sin_prefijo = set(re.findall(r'\b(tbl[A-Za-z0-9_]+)\b', code, re.IGNORECASE))

    encontradas = {}
    for key, info in tablas.items():
        if key in refs_con_prefijo:
            encontradas[key] = info
        elif key in refs_sin_prefijo:
            encontradas[key] = info
        else:
            # Busca por la parte después del punto (ej. "tblColaborador" matchea "modulos.tblColaborador")
            parte = key.split(".")[-1]
            if parte in refs_sin_prefijo:
                encontradas[key] = info

    if not encontradas:
        return ""

    advertencias = _db_schema.get("advertencias_criticas", [])
    tips = _db_schema.get("query_tips", [])

    lineas = [
        "### 🗄️ Schema Real de Base de Datos",
        f"_Sistema: {_db_schema.get('_meta', {}).get('sistema', 'este proyecto')} — "
        "Solo referencia estructural. NO ejecutar queries reales._\n"
    ]

    for nombre, info in encontradas.items():
        lineas.append(f"**{nombre}** — {info.get('desc', '')}")
        columnas = info.get("columnas", {})
        if isinstance(columnas, dict):
            cols_str = " | ".join(f"`{c}` {v}" for c, v in list(columnas.items())[:18])
            lineas.append(f"Columnas: {cols_str}")
        notas = info.get("notas", [])
        if notas:
            lineas.append("⚠️ " + " | ".join(notas))
        relaciones = info.get("relaciones", {})
        if relaciones:
            rels_str = ", ".join(f"{k}: {v}" for k, v in list(relaciones.items())[:4])
            lineas.append(f"Relaciones: {rels_str}")
        lineas.append("")

    # Agrega solo las advertencias globales que sean relevantes para las tablas encontradas
    tablas_encontradas_str = " ".join(encontradas.keys()).lower()
    advertencias_relevantes = [
        a for a in advertencias
        if any(parte.lower() in tablas_encontradas_str for parte in a.split()[:4])
    ]
    if advertencias_relevantes:
        lineas.append("**⚠️ Trampas críticas de esta BD:**")
        for adv in advertencias_relevantes[:6]:
            lineas.append(f"- {adv}")
        lineas.append("")

    # Tips de query solo si el código tiene SQL
    if re.search(r'\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN)\b', code, re.IGNORECASE) and tips:
        lineas.append("**💡 Tips de query para este sistema:**")
        for tip in tips[:4]:
            lineas.append(f"- {tip}")

    return "\n".join(lineas)
