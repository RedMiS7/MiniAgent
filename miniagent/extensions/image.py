from miniagent.models import LLMError, ToolDefinition
from miniagent.models.image_backend import ImageBackend
from miniagent.tools import ToolContent, ToolError, ToolResult


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ToolError("invalid_image", "Expected a PNG, JPEG or WebP image.")


class ImageTool:
    definition = ToolDefinition(
        name="image",
        description=(
            "Inspect a workspace image or generate a PNG file. Defaults to the current model. "
            "If unsupported, choose an explicitly configured alternative from the result or decline. "
            "An explicit model selection applies only to this call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"enum": ["inspect", "generate"]},
                "prompt": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
                "output_path": {"type": "string", "minLength": 1},
                "model": {"type": "string", "minLength": 1},
            },
            "required": ["action", "prompt"], "additionalProperties": False,
            "oneOf": [
                {"properties": {"action": {"const": "inspect"}}, "required": ["path"],
                 "not": {"required": ["output_path"]}},
                {"properties": {"action": {"const": "generate"}}, "required": ["output_path"],
                 "not": {"required": ["path"]}},
            ],
        },
    )

    def __init__(self, models: dict[str, ImageBackend] | None = None):
        self.models = dict(models or {})

    def _failure(self, code, message, action, selected):
        alternatives = [
            name for name, backend in self.models.items()
            if name != selected and getattr(backend.capabilities, action)
        ]
        return ToolResult(
            False, (ToolContent("json", {"selected_model": selected, "available_models": alternatives}),),
            code, message,
        )

    async def execute(self, arguments, context):
        action = arguments["action"]
        selected = arguments.get("model", "current")
        backend = self.models.get(selected)
        if backend is None:
            return self._failure(
                "unsupported_feature", "Selected image model is not configured.", action, selected,
            )
        if not getattr(backend.capabilities, action):
            return self._failure(
                "unsupported_feature", "Selected model has no supported adapter capability for this action.",
                action, selected,
            )
        try:
            if action == "inspect":
                path = context.path(arguments["path"])
                with path.open("rb") as source:
                    data = source.read(context.max_image_bytes + 1)
                if len(data) > context.max_image_bytes:
                    raise ToolError("file_too_large", "Image exceeds the size limit.")
                text = await backend.inspect(data, _mime(data), arguments["prompt"])
                return ToolResult(True, (ToolContent("json", {
                    "model": selected, "text": text[:context.max_output_chars],
                    "truncated": len(text) > context.max_output_chars,
                }),))

            path = context.path(arguments["output_path"])
            if path.suffix.lower() != ".png":
                raise ToolError("invalid_arguments", "Generated images must use a .png path.")
            if path.exists():
                raise ToolError("already_exists", "Image output already exists.")
            if not path.parent.is_dir():
                raise ToolError("invalid_arguments", "Output directory must already exist.")
            data = await backend.generate(arguments["prompt"], context.max_image_bytes)
            if len(data) > context.max_image_bytes:
                raise ToolError("file_too_large", "Generated image exceeds the size limit.")
            if _mime(data) != "image/png":
                raise ToolError("invalid_image", "Generation returned a non-PNG image.")
            # Revalidate after the network operation; exclusive create avoids overwriting.
            path = context.path(arguments["output_path"])
            try:
                with path.open("xb") as output:
                    output.write(data)
            except FileExistsError:
                raise ToolError("already_exists", "Image output appeared during generation.") from None
            return ToolResult(True, (ToolContent("json", {
                "model": selected, "path": arguments["output_path"], "mime_type": "image/png",
                "bytes": len(data),
            }),))
        except LLMError as exc:
            return self._failure(exc.code, str(exc), action, selected)


def register(registry, models=None):
    registry.register(ImageTool(models))
