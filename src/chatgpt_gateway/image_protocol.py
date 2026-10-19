"""Explicit image compatibility subset; no image-model or parameter substitution."""

import base64
import binascii
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from .config import Settings
from .errors import invalid
from .protocol import provider_preferences, unknown_fields

IMAGE_MODEL = "codex/imagegen"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def image_type(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (
                image.format not in IMAGE_TYPES
                or image.width * image.height > MAX_IMAGE_PIXELS
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError("Unsupported image")
            kind = IMAGE_TYPES[image.format]
            image.verify()
            return kind
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise invalid("Supply a valid static PNG, JPEG or WebP image.", "image") from error


def reference_image(value: object) -> bytes:
    if not isinstance(value, str) or ";base64," not in value:
        raise invalid(
            "Reference images must be base64 data URLs; remote URLs are unsupported.", "image"
        )
    header, encoded = value.split(";base64,", 1)
    if header not in {f"data:{kind}" for kind in IMAGE_TYPES.values()}:
        raise invalid("Reference images must be PNG, JPEG or WebP data URLs.", "image")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise invalid("Invalid base64 reference image.", "image") from error
    if len(data) > MAX_IMAGE_BYTES or image_type(data) != header.removeprefix("data:"):
        raise invalid("Reference image size or media type is invalid.", "image")
    return data


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    references: tuple[bytes, ...] = ()
    data_policy_override: bool = False


def image_request(body: dict, settings: Settings, *, edit: bool = False) -> ImageRequest:
    unknown_fields(
        body,
        {
            "model",
            "prompt",
            "image",
            "n",
            "size",
            "quality",
            "output_format",
            "response_format",
            "background",
            "stream",
            "provider",
        },
    )
    if body.get("model") != IMAGE_MODEL:
        raise invalid("Only codex/imagegen is supported: Codex's default image model.", "model")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise invalid("prompt must contain between 1 and 32000 characters.", "prompt")
    if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        raise invalid("Codex image requests support exactly one output image.", "n")
    for name, default in {"size": "auto", "quality": "auto", "background": "auto"}.items():
        if body.get(name) not in (None, default):
            raise invalid(
                f"{name} supports only auto; Codex controls its image tool settings.", name
            )
    if body.get("output_format") not in (None, "png"):
        raise invalid("Only PNG output is supported.", "output_format")
    if body.get("response_format") not in (None, "b64_json"):
        raise invalid("Only inline b64_json output is supported.", "response_format")
    if body.get("stream") is not None and body["stream"] is not False:
        raise invalid("Image streaming is unsupported.", "stream")
    raw = body.get("image", [])
    raw = raw if isinstance(raw, list) else [raw]
    if len(raw) > 5 or (edit and not raw):
        raise invalid("Edits require 1 to 5 reference images; generation accepts up to 5.", "image")
    override = provider_preferences(
        body.get("provider"),
        accept_chatgpt_data_controls=settings.accept_chatgpt_data_controls,
        provider_name="openai",
        data_controls="the signed-in Codex account",
    )
    return ImageRequest(prompt, tuple(reference_image(value) for value in raw), override)


def image_chat_request(body: dict, settings: Settings) -> ImageRequest:
    unknown_fields(body, {"model", "messages", "modalities", "stream", "n", "provider"})
    if body.get("modalities") not in (["image"], ["image", "text"], ["text", "image"]):
        raise invalid('Request image output with modalities:["image","text"].', "modalities")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise invalid(
            "Supply user messages containing the image prompt and references.", "messages"
        )
    text, images = [], []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            raise invalid("The image chat subset accepts user messages only.", "messages")
        unknown_fields(message, {"role", "content"})
        content = message.get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(parts, list):
            raise invalid("Invalid image prompt content.", "messages")
        for part in parts:
            if not isinstance(part, dict):
                raise invalid("Invalid image prompt part.", "messages")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                unknown_fields(part, {"type", "text"})
                text.append(part["text"])
            elif part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
                unknown_fields(part, {"type", "image_url"})
                unknown_fields(part["image_url"], {"url"})
                images.append(part["image_url"].get("url"))
            else:
                raise invalid("Image content supports text and image_url parts only.", "messages")
    return image_request(
        {
            "model": body.get("model"),
            "prompt": "\n\n".join(text),
            "image": images,
            "provider": body.get("provider"),
            "stream": body.get("stream"),
            "n": body.get("n", 1),
        },
        settings,
    )
