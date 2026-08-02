from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.schemas.ai_enrichment import AIAssetEnrichmentResult, build_openai_strict_json_schema
from app.services.asset_preview import sanitize_error_message

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_NVIDIA_MODEL = "meta/llama-3.1-70b-instruct"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
logger = logging.getLogger(__name__)


class OpenAIProviderError(RuntimeError):
    pass


def call_openai_vision(
    prompt: str,
    image_paths: list[Path],
    model: str | None = None,
    timeout_seconds: float = 60.0,
) -> AIAssetEnrichmentResult:
    settings = get_settings()
    nvidia_api_key = settings.nvidia_api_key
    if not nvidia_api_key and not settings.openai_api_key:
        raise OpenAIProviderError("NVIDIA_API_KEY or OPENAI_API_KEY is not configured")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise OpenAIProviderError("openai package is not installed") from exc

    nvidia_model = model or settings.ai_model or DEFAULT_NVIDIA_MODEL
    openai_model = model or settings.ai_model or DEFAULT_OPENAI_MODEL
    content = build_vision_chat_content(prompt, image_paths)
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "asset_enrichment",
            "schema": build_openai_strict_json_schema(AIAssetEnrichmentResult),
            "strict": True,
        },
    }

    try:
        if nvidia_api_key:
            try:
                nvidia_client = OpenAI(
                    api_key=nvidia_api_key,
                    base_url=NVIDIA_BASE_URL,
                    timeout=timeout_seconds,
                    max_retries=2,
                )
                response = nvidia_client.chat.completions.create(
                    model=nvidia_model,
                    messages=[{"role": "user", "content": content}],
                    response_format=response_format,
                )
                return AIAssetEnrichmentResult.model_validate_json(extract_chat_output_text(response))
            except Exception as exc:
                if not settings.openai_api_key:
                    raise
                logger.warning("NVIDIA API failed, falling back to OpenAI: %s", sanitize_provider_error(str(exc)))

        openai_client = OpenAI(
            api_key=settings.openai_api_key,
            timeout=timeout_seconds,
            max_retries=2,
        )
        response = openai_client.chat.completions.create(
            model=openai_model,
            messages=[{"role": "user", "content": content}],
            response_format=response_format,
        )
        return AIAssetEnrichmentResult.model_validate_json(extract_chat_output_text(response))
    except Exception as exc:
        raise OpenAIProviderError(sanitize_provider_error(str(exc))) from exc


def build_vision_chat_content(prompt: str, image_paths: list[Path]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_path in image_paths:
        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}})
    return content


def extract_chat_output_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content
    return extract_output_text(response)


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
    if settings.nvidia_api_key:
        sanitized = sanitized.replace(settings.nvidia_api_key, "[nvidia_api_key]")
    sanitized = re.sub(r"sk-[A-Za-z0-9_-]+", "[openai_api_key]", sanitized)
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [redacted]", sanitized)
    return sanitized[:700] or "AI provider failed"
