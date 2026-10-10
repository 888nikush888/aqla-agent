"""JSON Schema validation copies; literal instance data is never rewritten."""
import copy

LITERALS = frozenset({"const", "enum", "default", "example", "examples"})


def nullable_schema(node):
    if isinstance(node, list):
        return [nullable_schema(value) for value in node]
    if not isinstance(node, dict):
        return node
    result = {key: copy.deepcopy(value) if key in LITERALS else nullable_schema(value)
              for key, value in node.items() if key != "nullable"}
    if node.get("nullable") is True:
        definitions = {key: result.pop(key) for key in ("$defs", "definitions", "$id", "$schema", "$anchor", "$dynamicAnchor") if key in result}
        return {**definitions, "anyOf": [result, {"type": "null"}]}
    return result


def diagnostic_bool(args):
    """Normalize only the documented boolean spellings before capable hooks."""
    value = args.get("diagnostics_only", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError("diagnostics_only requires true or false; null and malformed values are invalid")


def external_refs(node):
    if isinstance(node, list):
        return any(external_refs(value) for value in node)
    if not isinstance(node, dict):
        return False
    return any(isinstance(node.get(key), str) and not node[key].startswith("#") for key in ("$ref", "$dynamicRef", "$recursiveRef")) or any(
        external_refs(value) for key, value in node.items() if key not in LITERALS)
