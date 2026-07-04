from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.schemas.ai_enrichment import AIAssetEnrichmentResult, build_openai_strict_json_schema
from app.services.asset_preview import sanitize_error_message

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


class OpenAIProviderError(RuntimeError):
    pass


def call_openai_vision(
    prompt: str,
    image_paths: list[Path],
    model: str | None = None,
    timeout_seconds: float = 60.0,
) -> AIAssetEnrichmentResult:
    settings = get_settings()
    if not settings.openai_api_key:
        raise OpenAIProviderError("OPENAI_API_KEY is not configured")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise OpenAIProviderError("openai package is not installed") from exc

    selected_model = model or settings.ai_model or DEFAULT_OPENAI_MODEL
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for image_path in image_paths:
        content.append({"type": "input_image", "image_url": image_to_data_url(image_path)})

    try:
        client = OpenAI(
            api_key=settings.openai_api_key,
            timeout=timeout_seconds,
            max_retries=2,
        )
        response = client.responses.create(
            model=selected_model,
            input=[{"role": "user", "content": content}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "asset_enrichment",
                    "schema": build_openai_strict_json_schema(AIAssetEnrichmentResult),
                    "strict": True,
                }
            },
        )
        output_text = extract_output_text(response)
        return AIAssetEnrichmentResult.model_validate_json(output_text)
    except Exception as exc:
        raise OpenAIProviderError(sanitize_provider_error(str(exc))) from exc


def image_to_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def extract_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    if hasattr(response, "model_dump"):
        payload = response.model_dump()
    else:
        payload = response
    return json.dumps(payload)


def sanitize_provider_error(message: str) -> str:
    settings = get_settings()
    sanitized = sanitize_error_message(message)
    if settings.openai_api_key:
        sanitized = sanitized.replace(settings.openai_api_key, "[openai_api_key]")
    sanitized = re.sub(r"sk-[A-Za-z0-9_-]+", "[openai_api_key]", sanitized)
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [redacted]", sanitized)
    return sanitized[:700] or "AI provider failed"
