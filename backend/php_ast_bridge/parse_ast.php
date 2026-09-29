<?php
/**
 * Puente PHP -> Python: parsea código PHP con nikic/php-parser (AST real,
 * no regex sobre texto) y emite un AST SIMPLIFICADO en JSON por stdout.
 *
 * Por qué existe: danger_flags.py detectaba peligros con regex sobre el
 * string completo del archivo — funciona, pero no distingue código real de
 * un comentario, de un string literal usado solo para logging, ni puede
 * seguir una variable a través de un par de asignaciones intermedias antes
 * de llegar a una query SQL (taint analysis real). Este puente da acceso a
 * la estructura real del código sin reescribir todo el detector en PHP.
 *
 * Uso: php parse_ast.php < archivo.php
 * Salida: {"ok": true, ...} o {"ok": false, "error": "..."}
 */

require __DIR__ . '/vendor/autoload.php';

use PhpParser\Error;
use PhpParser\Node;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitorAbstract;
use PhpParser\ParserFactory;

$code = stream_get_contents(STDIN);

function node_line($node) {
    return $node->getStartLine();
}

// Funciones de PHP consideradas "saneamiento" para efectos de XSS: si el
// valor tainted pasa por una de estas antes de llegar a echo/print, deja
// de considerarse peligroso. Lista deliberadamente conservadora — mejor
// un falso negativo raro que marcar como seguro algo que no lo es; htmlspecialchars/htmlentities
// son los casos reales más comunes en PHP legacy tipo CAPA 8.
$GLOBALS['__XSS_SANITIZERS__'] = ['htmlspecialchars', 'htmlentities', 'strip_tags'];

function resolve_expr($expr) {
    if ($expr instanceof Node\Scalar\String_) {
        return ['kind' => 'literal', 'value' => $expr->value];
    }
    if ($expr instanceof Node\Scalar\LNumber || $expr instanceof Node\Scalar\DNumber) {
        return ['kind' => 'literal', 'value' => (string)$expr->value];
    }
    // Strings con interpolación de doble comilla: "DELETE FROM x WHERE id IN $ids"
    // Nikic los representa como Scalar\InterpolatedString (lista de partes:
    // Scalar\EncapsedStringPart para el texto literal, y Expr\Variable u
    // otras expresiones para las partes interpoladas). Sin este caso, CUALQUIER
    // query construida con comillas dobles e interpolación directa (muy común
    // en PHP) quedaba como source_kind=null, invisible para el detector.
    if ($expr instanceof Node\Scalar\InterpolatedString) {
        $text = '';
        $dynamic_sources = [];
        $any_dynamic = false;
        foreach ($expr->parts as $part) {
            if ($part instanceof Node\Scalar\EncapsedStringPart) {
                $text .= $part->value;
            } else {
                $resolved = resolve_expr($part);
                $any_dynamic = true;
                if ($resolved['kind'] === 'literal') {
                    $text .= $resolved['value'];
                } else {
                    $text .= '{' . $resolved['kind'] . ':' . ($resolved['value'] ?? '?') . '}';
                    $dynamic_sources[] = $resolved;
                    if (!empty($resolved['dynamic_sources'] ?? [])) {
                        $dynamic_sources = array_merge($dynamic_sources, $resolved['dynamic_sources']);
                    }
                }
            }
        }
        return [
            'kind' => $any_dynamic ? 'concat_dynamic' : 'literal',
            'value' => $text,
            'dynamic_sources' => $dynamic_sources,
        ];
    }
    if ($expr instanceof Node\Expr\BinaryOp\Concat) {
        $left = resolve_expr($expr->left);
        $right = resolve_expr($expr->right);
        $has_dynamic = ($left['kind'] !== 'literal') || ($right['kind'] !== 'literal');
        $text = ($left['kind'] === 'literal' ? $left['value'] : '{' . $left['kind'] . '}')
              . ($right['kind'] === 'literal' ? $right['value'] : '{' . $right['kind'] . '}');
        return [
            'kind' => $has_dynamic ? 'concat_dynamic' : 'literal',
            'value' => $text,
            'dynamic_sources' => array_merge(
                $left['dynamic_sources'] ?? ($left['kind'] !== 'literal' ? [$left] : []),
                $right['dynamic_sources'] ?? ($right['kind'] !== 'literal' ? [$right] : [])
            ),
        ];
    }
    if ($expr instanceof Node\Expr\Variable) {
        return ['kind' => 'variable', 'value' => is_string($expr->name) ? '$' . $expr->name : '$<dinamico>'];
    }
    if ($expr instanceof Node\Expr\ArrayDimFetch) {
        $baseVar = $expr->var instanceof Node\Expr\Variable && is_string($expr->var->name)
            ? '$' . $expr->var->name : '$<dinamico>';
        $dim = null;
        if ($expr->dim instanceof Node\Scalar\String_) {
            $dim = $expr->dim->value;
        }
        $superglobals = ['_GET', '_POST', '_REQUEST', '_COOKIE', '_SERVER'];
        $isSuperglobal = in_array(ltrim($baseVar, '$'), $superglobals, true);
        return [
            'kind' => $isSuperglobal ? 'user_input' : 'array_access',
            'value' => $baseVar . ($dim !== null ? "['$dim']" : '[...]'),
        ];
    }
    if ($expr instanceof Node\Expr\FuncCall && $expr->name instanceof Node\Name) {
        $fname = $expr->name->toString();
        $result = ['kind' => 'call', 'value' => $fname . '(...)'];

        // Si es una función conocida de saneamiento, el resultado NUNCA se
        // considera tainted, sin importar sus argumentos — es justamente
        // el propósito de esa función. Esto es lo que evita marcar
        // `echo htmlspecialchars($_GET['x'])` como XSS (falso positivo).
        if (in_array($fname, $GLOBALS['__XSS_SANITIZERS__'], true)) {
            $result['sanitized'] = true;
            return $result;
        }

        // Si NO es un sanitizador conocido, propaga el taint de sus
        // argumentos hacia arriba: `echo strtoupper($_GET['x'])` sigue
        // siendo XSS porque strtoupper no escapa nada.
        foreach ($expr->args as $arg) {
            $argResolved = resolve_expr($arg->value);
            $argIsTainted = ($argResolved['kind'] === 'user_input')
                || (!empty($argResolved['dynamic_sources'] ?? []));
            if ($argIsTainted) {
                $result['dynamic_sources'] = array_merge(
                    $result['dynamic_sources'] ?? [],
                    $argResolved['kind'] === 'user_input' ? [$argResolved] : ($argResolved['dynamic_sources'] ?? [])
                );
            }
            // Nota: la propagación vía variable simple ($x tainted pasada
            // como argumento) se resuelve en TaintCollector::annotate_with_taint,
            // que sí conoce $this->tainted_vars — aquí en resolve_expr (función
            // libre, sin acceso a esa tabla) solo podemos ver el caso
            // user_input directo o dynamic_sources ya resueltos.
        }
        return $result;
    }
    return ['kind' => 'expr:' . (new \ReflectionClass($expr))->getShortName(), 'value' => null];
}

class TaintCollector extends NodeVisitorAbstract {
    public array $tainted_vars = [];
    public array $calls = [];
    public array $assignments = [];

    public function enterNode(Node $node) {
        if ($node instanceof Node\Expr\Assign && $node->var instanceof Node\Expr\Variable && is_string($node->var->name)) {
            $varname = '$' . $node->var->name;
            $resolved = resolve_expr($node->expr);

            $this->assignments[] = [
                'var' => $varname,
                'line' => node_line($node),
                'source_kind' => $resolved['kind'],
                'source_value' => $resolved['value'],
            ];

            if ($resolved['kind'] === 'user_input') {
                $this->tainted_vars[$varname] = $resolved['value'];
            } elseif ($resolved['kind'] === 'variable' && isset($this->tainted_vars[$resolved['value']])) {
                $this->tainted_vars[$varname] = $this->tainted_vars[$resolved['value']];
            } elseif (!empty($resolved['dynamic_sources'] ?? [])) {
                foreach ($resolved['dynamic_sources'] as $src) {
                    if ($src['kind'] === 'user_input') {
                        $this->tainted_vars[$varname] = $src['value'];
                        break;
                    }
                    if ($src['kind'] === 'variable' && isset($this->tainted_vars[$src['value']])) {
                        $this->tainted_vars[$varname] = $this->tainted_vars[$src['value']];
                        break;
                    }
                }
            }
        }

        if ($node instanceof Node\Expr\FuncCall && $node->name instanceof Node\Name) {
            $args = [];
            foreach ($node->args as $arg) {
                $resolved = resolve_expr($arg->value);
                $args[] = $this->annotate_with_taint($resolved);
            }
            $this->calls[] = [
                'name' => $node->name->toString(),
                'line' => node_line($node),
                'type' => 'function',
                'args' => $args,
            ];
        }
        if ($node instanceof Node\Expr\MethodCall && $node->name instanceof Node\Identifier) {
            $args = [];
            foreach ($node->args as $arg) {
                $resolved = resolve_expr($arg->value);
                $args[] = $this->annotate_with_taint($resolved);
            }
            $this->calls[] = [
                'name' => $node->name->toString(),
                'line' => node_line($node),
                'type' => 'method',
                'args' => $args,
            ];
        }

        // echo/print no son llamadas a función en el AST de PHP (son
        // construcciones del lenguaje), así que se capturan aparte y se
        // normalizan como una "llamada" sintética a __echo__ / __print__.
        // Esto es lo que habilita la detección de XSS: si $_GET/$_POST
        // llega directo a un echo sin pasar por htmlspecialchars()/similar,
        // el valor queda reflejado tal cual en el HTML de salida.
        if ($node instanceof Node\Stmt\Echo_) {
            foreach ($node->exprs as $expr) {
                $resolved = resolve_expr($expr);
                $this->calls[] = [
                    'name' => '__echo__',
                    'line' => node_line($node),
                    'type' => 'language_construct',
                    'args' => [$this->annotate_with_taint($resolved)],
                ];
            }
        }
        if ($node instanceof Node\Expr\Print_) {
            $resolved = resolve_expr($node->expr);
            $this->calls[] = [
                'name' => '__print__',
                'line' => node_line($node),
                'type' => 'language_construct',
                'args' => [$this->annotate_with_taint($resolved)],
            ];
        }
        return null;
    }

    private function annotate_with_taint($resolved) {
        // Un resultado marcado 'sanitized' (viene de htmlspecialchars/etc.,
        // ver resolve_expr) NUNCA se considera tainted, sin importar qué
        // más diga el resto de la estructura — el saneamiento es la razón
        // de ser de esa función.
        if (!empty($resolved['sanitized'])) {
            return $resolved;
        }
        if ($resolved['kind'] === 'variable' && isset($this->tainted_vars[$resolved['value']])) {
            $resolved['tainted'] = true;
            $resolved['taint_source'] = $this->tainted_vars[$resolved['value']];
        } elseif ($resolved['kind'] === 'user_input') {
            $resolved['tainted'] = true;
            $resolved['taint_source'] = $resolved['value'];
        } elseif (!empty($resolved['dynamic_sources'] ?? [])) {
            foreach ($resolved['dynamic_sources'] as $src) {
                $srcTainted = $src['kind'] === 'user_input'
                    || ($src['kind'] === 'variable' && isset($this->tainted_vars[$src['value']]));
                if ($srcTainted) {
                    $resolved['tainted'] = true;
                    $resolved['taint_source'] = $src['kind'] === 'user_input' ? $src['value'] : $this->tainted_vars[$src['value']];
                    break;
                }
            }
        }
        return $resolved;
    }
}

try {
    $parser = (new ParserFactory())->createForNewestSupportedVersion();
    $ast = $parser->parse($code);

    $traverser = new NodeTraverser();
    $collector = new TaintCollector();
    $traverser->addVisitor($collector);
    $traverser->traverse($ast);

    echo json_encode([
        'ok' => true,
        'tainted_vars' => $collector->tainted_vars,
        'assignments' => $collector->assignments,
        'calls' => $collector->calls,
    ], JSON_UNESCAPED_SLASHES);
} catch (Error $e) {
    echo json_encode(['ok' => false, 'error' => $e->getMessage()]);
} catch (\Throwable $e) {
    echo json_encode(['ok' => false, 'error' => 'Error interno: ' . $e->getMessage()]);
}
