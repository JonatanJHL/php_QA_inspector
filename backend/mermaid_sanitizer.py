"""Saneo de fragmentos de diagrama Mermaid generados por un LLM a partir de
código PHP/JS real.

Por qué existe: cuando se le pide a un LLM que dibuje un diagrama de flujo
del código que acaba de analizar, tiende a copiar condiciones PHP literales
dentro de las etiquetas de los nodos (ej. `TOP_C{$_GET['action'] ==
"succes"?}`) — eso trae símbolos que Mermaid no puede parsear sin escapar
($, comillas simples y dobles sin cerrar el label completo, ==, ?), y el
diagrama entero falla a renderizar. Confirmado en vivo contra un archivo
real (`principal.php` de CAPA 8): ese patrón exacto rompía el parser.

Compartido entre main.py (endpoint /api/qa/desktop-test, dos rutas:
analyze_php_in_blocks y generate_mermaid_diagram) y agent_loop.py
(_generate_flow_diagram_section) — los tres piden Mermaid al LLM a partir
de código real, mismo riesgo en los tres.
"""

import re


def _sanitize_mermaid_node_text(text: str) -> str:
    """Envuelve el texto interno de un nodo Mermaid en comillas dobles si no
    lo está ya, y neutraliza comillas dobles internas (que romperían el
    delimitador que se acaba de agregar). Corrige la causa #1 confirmada de
    fallo de renderizado: el modelo copiando una condición PHP literal
    dentro de un nodo (ej. `TOP_C{$_GET['action'] == "succes"?}`), que trae
    $, comillas simples y dobles, y == sin escapar — Mermaid exige que ese
    texto vaya como un solo string entre comillas dobles.
    Deliberadamente conservador: si el texto YA está entre comillas dobles
    (empieza y termina con "), se asume que el modelo ya lo hizo bien y no
    se toca — evitar doble-envolver "texto" en ""texto""."""
    already_quoted = text.startswith('"') and text.endswith('"') and len(text) >= 2
    inner = text[1:-1] if already_quoted else text
    # Comillas dobles internas se vuelven comilla tipográfica — Mermaid no
    # soporta \" escapado de forma consistente entre versiones, así que
    # cambiar el carácter es más confiable que intentar escaparlo.
    inner = inner.replace('"', '\u201d')
    return f'"{inner}"'


# Delimitadores de forma de nodo Mermaid, apertura -> cierre. El texto
# interno casi siempre contiene código PHP real, que trae sus PROPIOS
# corchetes/paréntesis ($_GET['x'], mysqli_num_rows($y)) — un regex simple
# no-codicioso hasta el primer delimitador de cierre corta el nodo a la
# mitad ahí, no en el cierre real (confirmado con un caso real:
# 'TOP_C{$_GET[\'action\']...}' se cortaba en el ']' de PHP). Por eso el
# saneo se hace con un escaneo manual que CUENTA profundidad de anidamiento
# del mismo tipo de delimitador, en vez de una sola expresión regular.
_NODE_ID_RE = re.compile(r'([A-Za-z_][\w]*)')
_OPEN_TO_CLOSE = {'[[': ']]', '((': '))', '[(': ')]', '{{': '}}', '[': ']', '(': ')', '{': '}'}
_OPENERS_BY_LENGTH = sorted(_OPEN_TO_CLOSE, key=len, reverse=True)  # probar los de 2 chars antes que los de 1


def sanitize_mermaid_fragment(fragment: str) -> str:
    """Recorre el fragmento carácter a carácter: cuando encuentra un ID de
    nodo seguido de un delimitador de apertura, cuenta profundidad hasta
    encontrar el delimitador de CIERRE que balancea esa apertura específica
    (ignorando delimitadores de otro tipo que puedan aparecer dentro, como
    los corchetes de $_GET['x'] dentro de un nodo {...}), y sanea solo ese
    texto interno con _sanitize_mermaid_node_text. Todo lo demás (flechas,
    IDs, texto fuera de nodos) se copia tal cual.

    Validado contra el parser real de Mermaid (mermaid-cli) con el caso
    real que motivó esto: exit code 0, SVG generado correctamente."""
    out = []
    i, n = 0, len(fragment)
    while i < n:
        id_match = _NODE_ID_RE.match(fragment, i)
        if not id_match:
            out.append(fragment[i])
            i += 1
            continue

        node_id = id_match.group(1)
        j = id_match.end()
        opener = None
        for cand in _OPENERS_BY_LENGTH:
            if fragment.startswith(cand, j):
                opener = cand
                break
        if opener is None:
            out.append(node_id)
            i = j
            continue

        closer = _OPEN_TO_CLOSE[opener]
        # Delimitador de anidamiento a contar: el primer carácter del closer
        # (ej. ']' para ']]'), ya que es lo único que puede repetirse dentro
        # (PHP real trae [ ] y ( ) sueltos, nunca [[ ]] dobles).
        open_char, close_char = opener[-1], closer[0]
        depth = 1
        k = j + len(opener)
        start_inner = k
        while k < n and depth > 0:
            if fragment[k] == open_char and open_char != close_char:
                depth += 1
            elif fragment[k] == close_char:
                depth -= 1
                if depth == 0:
                    break
            k += 1

        if depth != 0:
            # No se encontró cierre balanceado (fragmento truncado, o forma
            # no reconocida) — no se puede sanear con seguridad, se copia
            # el resto tal cual en vez de arriesgar corromper más el texto.
            out.append(fragment[i:])
            i = n
            continue

        inner_text = fragment[start_inner:k]
        sanitized = _sanitize_mermaid_node_text(inner_text)
        out.append(f'{node_id}{opener}{sanitized}{closer}')
        i = k + len(closer)

    return ''.join(out)


def strip_diagram_type_declaration(fragment: str) -> str:
    """Quita una línea inicial 'flowchart TD'/'graph LR'/etc. si el
    fragmento la trae — necesario cuando varios fragmentos de bloques
    distintos se van a unir bajo UNA sola declaración de tipo (ver
    main.py: analyze_php_in_blocks). Sin esto, un modelo que incluye su
    propia declaración en cada bloque produce una declaración DUPLICADA al
    consolidar (confirmado en vivo: 'flowchart TD\\nflowchart TD\\n...'),
    sintaxis Mermaid inválida."""
    return re.sub(r'^\s*(?:flowchart|graph)\s+(?:TD|LR|TB|RL)\s*\n?', '', fragment, flags=re.IGNORECASE)
