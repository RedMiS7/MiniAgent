from copy import deepcopy

from jsonschema import Draft202012Validator

from .base import Tool, ToolError


class ToolRegistry:
    def __init__(self):
        self._tools = {}
        self._definitions = {}
        self._validators = {}

    def register(self, tool: Tool) -> None:
        definition = deepcopy(tool.definition)
        if definition.name in self._tools:
            raise ValueError(f"Duplicate tool name: {definition.name}")
        schema = definition.parameters
        Draft202012Validator.check_schema(schema)
        self._check_refs(schema)
        self._tools[definition.name] = tool
        self._definitions[definition.name] = definition
        self._validators[definition.name] = Draft202012Validator(schema)

    @staticmethod
    def _check_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("$ref", "$dynamicRef") and not child.startswith("#"):
                    raise ValueError("Tool schemas must use local references.")
                ToolRegistry._check_refs(child)
        elif isinstance(value, list):
            for child in value:
                ToolRegistry._check_refs(child)

    def definitions(self):
        return tuple(deepcopy(d) for d in self._definitions.values())

    def resolve(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError("unknown_tool", "Tool is not registered.")
        return self._tools[name]

    def validate(self, name: str, arguments: dict) -> None:
        if not self._validators[name].is_valid(arguments):
            raise ToolError("invalid_arguments", "Arguments do not match the tool schema.")
