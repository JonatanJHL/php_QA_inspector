"""Exportador de hallazgos a formato SARIF 2.1.0 (Static Analysis Results
Interchange Format), el estándar que GitHub Code Scanning, GitLab, Azure
DevOps y la mayoría de herramientas de CI/CD entienden nativamente.

Por qué existe: sin esto, los hallazgos de este agente solo viven dentro de
su propia UI — no se pueden subir a un pipeline de CI/CD, no aparecen como
anotaciones en un Pull Request, y no se pueden combinar con otras
herramientas de seguridad que sí hablan SARIF (Semgrep, CodeQL, Psalm).

Referencia del formato: OASIS SARIF v2.1.0
https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html
"""

import os
import re

SARIF_SCHEMA_URL = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
SARIF_VERSION = "2.1.0"

# Metadata de reglas conocidas: cada categoría que puede aparecer en
# danger_flags_structured (ver danger_flags.py) necesita una entrada aquí
# para que el SARIF tenga descripciones legibles de cada regla, no solo un
# ID crudo. GitHub Code Scanning muestra esta info en la UI de "Security".
RULE_METADATA = {
    "missing_where": {
        "name": "SQLDestructiveWithoutWhere",
        "shortDescription": "Consulta destructiva (DELETE/UPDATE) sin cláusula WHERE efectiva",
        "fullDescription": (
            "Una consulta DELETE o UPDATE no tiene cláusula WHERE, o tiene una "
            "condición WHERE trivial (siempre verdadera como 1=1), lo cual "
            "afecta TODAS las filas de la tabla en vez de las filas esperadas."
        ),
        "level": "error",
        "tags": ["security", "data-loss", "sql"],
    },
    "sql_injection": {
        "name": "SQLInjectionViaTaintedInput",
        "shortDescription": "Posible inyección SQL: entrada de usuario sin sanear llega a una consulta",
        "fullDescription": (
            "Un valor proveniente de $_GET/$_POST/$_REQUEST/$_COOKIE llega a una "
            "consulta SQL sin pasar por una función de escape o un statement "
            "preparado, confirmado siguiendo el flujo real de la variable "
            "(no solo por coincidencia de texto)."
        ),
        "level": "error",
        "tags": ["security", "external/cwe/cwe-089"],
    },
    "command_injection": {
        "name": "CommandInjectionViaTaintedInput",
        "shortDescription": "Posible inyección de comandos: entrada de usuario llega a ejecución del sistema",
        "fullDescription": (
            "Un valor proveniente de entrada de usuario llega a una función que "
            "ejecuta comandos del sistema operativo (exec, shell_exec, system, etc.)."
        ),
        "level": "error",
        "tags": ["security", "external/cwe/cwe-078"],
    },
    "code_injection": {
        "name": "CodeInjectionViaTaintedInput",
        "shortDescription": "Posible inyección de código: entrada de usuario llega a eval()/assert()",
        "fullDescription": (
            "Un valor proveniente de entrada de usuario llega a una evaluación "
            "de código dinámico (eval, assert), permitiendo ejecución arbitraria."
        ),
        "level": "error",
        "tags": ["security", "external/cwe/cwe-095"],
    },
    "path_traversal": {
        "name": "PathTraversalViaTaintedInput",
        "shortDescription": "Posible path traversal: entrada de usuario llega a acceso de archivo",
        "fullDescription": (
            "Un valor proveniente de entrada de usuario llega a una función de "
            "acceso a archivos (include, require, file_get_contents, fopen, unlink) "
            "sin validación de ruta, permitiendo potencialmente leer/incluir/borrar "
            "archivos fuera del directorio esperado."
        ),
        "level": "error",
        "tags": ["security", "external/cwe/cwe-022"],
    },
    "xss": {
        "name": "ReflectedXSSViaTaintedInput",
        "shortDescription": "Posible XSS reflejado: entrada de usuario llega a echo/print sin escapar",
        "fullDescription": (
            "Un valor proveniente de $_GET/$_POST/$_REQUEST/$_COOKIE se imprime "
            "directamente en la salida HTML (echo/print) sin pasar por "
            "htmlspecialchars(), htmlentities() o strip_tags(), permitiendo "
            "potencialmente inyectar HTML/JavaScript arbitrario en la página."
        ),
        "level": "error",
        "tags": ["security", "external/cwe/cwe-079"],
    },
    "insecure_deserialization": {
        "name": "InsecureDeserializationViaTaintedInput",
        "shortDescription": "Posible deserialización insegura: entrada de usuario llega a unserialize()",
        "fullDescription": (
            "Un valor proveniente de entrada de usuario llega a unserialize() sin "
            "restringir las clases permitidas (parámetro 'allowed_classes'), lo "
            "cual puede habilitar PHP Object Injection si alguna clase cargada "
            "tiene métodos mágicos explotables (__wakeup, __destruct, etc.)."
        ),
        "level": "error",
        "tags": ["security", "external/cwe/cwe-502"],
    },
    "ssrf": {
        "name": "SSRFViaTaintedInput",
        "shortDescription": "Posible SSRF: entrada de usuario controla una URL/petición de red",
        "fullDescription": (
            "Un valor proveniente de entrada de usuario controla, total o "
            "parcialmente, la URL de una petición de red (file_get_contents, "
            "fopen, curl) sin validar contra una lista de hosts permitidos, "
            "permitiendo potencialmente que el servidor haga peticiones a "
            "endpoints internos no expuestos públicamente (Server-Side Request Forgery)."
        ),
        "level": "error",
        "tags": ["security", "external/cwe/cwe-918"],
    },
}

_DEFAULT_RULE = {
    "name": "GenericFinding",
    "shortDescription": "Hallazgo de seguridad detectado",
    "fullDescription": "Hallazgo detectado por el analizador de peligro del agente de QA.",
    "level": "warning",
    "tags": ["security"],
}


def build_sarif_for_file(filepath: str, danger_flags_structured: list, php_dir: str = None) -> dict:
    """Construye un documento SARIF 2.1.0 completo para UN archivo, a partir
    de los hallazgos estructurados (category+line+message) que ya produce
    danger_flags.py con return_structured=True.

    Solo incluye hallazgos con línea real — los hallazgos de puro regex sin
    línea (host de producción, credenciales hardcodeadas) no tienen una
    `region.startLine` que reportar de forma honesta en SARIF, así que se
    excluyen de este export en vez de inventar una línea 1 falsa."""
    rel_path = os.path.relpath(filepath, php_dir).replace("\\", "/") if php_dir else os.path.basename(filepath)
    # SARIF exige URIs, no paths de sistema de archivos crudos.
    artifact_uri = rel_path.replace("\\", "/")

    used_categories = sorted({f["category"] for f in danger_flags_structured})
    rules = []
    rule_index_by_category = {}
    for i, category in enumerate(used_categories):
        meta = RULE_METADATA.get(category, _DEFAULT_RULE)
        rule_index_by_category[category] = i
        rules.append({
            "id": category,
            "name": meta["name"],
            "shortDescription": {"text": meta["shortDescription"]},
            "fullDescription": {"text": meta["fullDescription"]},
            "defaultConfiguration": {"level": meta["level"]},
            "properties": {"tags": meta["tags"]},
        })

    results = []
    for finding in danger_flags_structured:
        category = finding["category"]
        meta = RULE_METADATA.get(category, _DEFAULT_RULE)
        line = finding.get("line")
        results.append({
            "ruleId": category,
            "ruleIndex": rule_index_by_category[category],
            "level": meta["level"],
            "message": {"text": finding["message"]},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": artifact_uri},
                    "region": {"startLine": line if line else 1},
                }
            }],
        })

    return {
        "version": SARIF_VERSION,
        "$schema": SARIF_SCHEMA_URL,
        "runs": [{
            "tool": {
                "driver": {
                    "name": "PHP-QA-Agent",
                    "informationUri": "https://angrifflabs.dev",
                    "version": "0.1.0",
                    "rules": rules,
                }
            },
            "results": results,
        }],
    }
