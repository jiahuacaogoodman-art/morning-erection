"""Shared, versioned action contract; bridge IDs and screenshot coordinates are distinct."""
from wakecore.protocol.jsonschema_lite import Registry

STRING = {"type": "string", "maxLength": 10000}
INDEX = {"type": "string", "pattern": "^[0-9]+$", "description": "Numeric index from the latest observed accessibility tree. Never use the element's ID/name."}
COORD = {"type": "number", "minimum": 0}


def action_schema(properties, required, **extra):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required, **extra}


SCHEMAS = {
    "click": action_schema({"element_index": INDEX, "x": COORD, "y": COORD,
        "click_count": {"type": "integer", "minimum": 1, "maximum": 3}, "mouse_button": {"enum": ["left", "right", "middle"]}}, [],
        oneOf=[{"required": ["element_index"]}, {"required": ["x", "y"]}]),
    "drag": action_schema({key: COORD for key in ("from_x", "from_y", "to_x", "to_y")}, ["from_x", "from_y", "to_x", "to_y"]),
    "scroll": action_schema({"element_index": INDEX, "direction": {"enum": ["up", "down", "left", "right"]}, "pages": {"type": "number", "minimum": .1, "maximum": 10}}, ["element_index", "direction"]),
    "press_key": action_schema({"key": {"type": "string", "maxLength": 40, "pattern": "^[A-Za-z0-9_]+(\\+[A-Za-z0-9_]+)*$"}}, ["key"]),
    "type_text": action_schema({"text": STRING}, ["text"]),
    "set_value": action_schema({"element_index": INDEX, "value": STRING}, ["element_index", "value"]),
    "select_text": action_schema({"element_index": INDEX, "text": STRING, "prefix": STRING, "suffix": STRING, "selection": {"enum": ["text", "cursor_before", "cursor_after"]}}, ["element_index", "text"]),
    "perform_secondary_action": action_schema({"element_index": INDEX, "action": STRING}, ["element_index", "action"]),
}


def validate_action(method, arguments):
    if method not in SCHEMAS:
        raise ValueError("未知电脑操作。请使用观察结果中的 actions 参数说明。")
    Registry(SCHEMAS).validate(arguments, method)
    return arguments
