"""Small strict JSON Schema validator; only the subset used by our schemas."""
import re


class ValidationError(ValueError):
    pass


def validate(value, schema, path="$", *, depth=0):
    if depth > 20:
        raise ValidationError("JSON nesting limit")
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                validate(value, candidate, path, depth=depth + 1)
                matches += 1
            except ValidationError:
                pass
        if matches != 1:
            raise ValidationError(f"{path}: expected exactly one allowed variant")
        return
    typ = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if typ in types and type(value) is not types[typ]:
        raise ValidationError(f"{path}: expected {typ}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path}: outside allow-list")
    if typ == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise ValidationError(f"{path}: missing required field")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise ValidationError(f"{path}: unknown field")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], path + "." + key, depth=depth + 1)
    if typ == "array":
        if len(value) > schema.get("maxItems", 100):
            raise ValidationError(f"{path}: too many items")
        for item in value:
            validate(item, schema["items"], path + "[]", depth=depth + 1)
    if typ == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4000):
            raise ValidationError(f"{path}: invalid length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValidationError(f"{path}: invalid characters or target")


def obj(**properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def choice(*values):
    return {"type": "string", "enum": list(values)}


TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
USER = {"type": "string", "pattern": r"(?:alice|bob|carol|sdtest_[a-z0-9_]{1,40})"}
TEMP_USER = {"type": "string", "pattern": r"sdtest_[a-z0-9_]{1,40}"}
PEER = {"type": "string", "pattern": r"agent-[a-z0-9-]{1,48}"}
