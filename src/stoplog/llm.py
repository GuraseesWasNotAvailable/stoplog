"""One small interface over two providers: OpenAI (Responses API) and Anthropic (Messages API).

Both calls use the provider's structured-output helper, so what comes back is already a
validated Pydantic object, never free text that has to be scraped.
"""

from __future__ import annotations

import base64
from typing import Protocol, TypeVar

from pydantic import BaseModel

from .config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The model call failed or returned nothing usable. `retryable` says whether to try again."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class LLM(Protocol):
    provider: str

    def structured(
        self,
        *,
        model: str,
        system: str,
        text: str,
        schema: type[T],
        image_jpeg: bytes | None = None,
        effort: str = "high",
    ) -> T: ...


class OpenAILLM:
    provider = "openai"

    def __init__(self, api_key: str, image_detail: str = "high", client=None):
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, max_retries=3, timeout=180)
        self.client = client
        self.image_detail = image_detail

    def structured(self, *, model, system, text, schema, image_jpeg=None, effort="high"):
        import openai

        content: list[dict] = [{"type": "input_text", "text": text}]
        if image_jpeg is not None:
            data_url = "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode("ascii")
            content.append({"type": "input_image", "image_url": data_url, "detail": self.image_detail})
        try:
            resp = self.client.responses.parse(
                model=model,
                instructions=system,
                input=[{"role": "user", "content": content}],
                text_format=schema,
                max_output_tokens=16000,
                store=False,
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as exc:
            raise LLMError(f"OpenAI rejected the request: {exc}", retryable=False) from exc
        except openai.BadRequestError as exc:
            raise LLMError(f"OpenAI bad request: {exc}", retryable=False) from exc
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
            raise LLMError(f"OpenAI temporarily unavailable: {exc}", retryable=True) from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"OpenAI error {exc.status_code}: {exc}", retryable=exc.status_code >= 500) from exc
        except Exception as exc:  # e.g. the output failed schema validation
            raise LLMError(f"OpenAI response could not be parsed: {exc}") from exc

        if getattr(resp, "incomplete_details", None):
            reason = getattr(resp.incomplete_details, "reason", None)
            raise LLMError(f"OpenAI response incomplete ({reason})", retryable=reason != "content_filter")
        parsed = resp.output_parsed
        if parsed is None:
            refusal = _openai_refusal(resp)
            if refusal:
                raise LLMError(f"OpenAI refused: {refusal}", retryable=False)
            raise LLMError("OpenAI returned no structured output")
        return parsed


def _openai_refusal(resp) -> str | None:
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return getattr(part, "refusal", "refused")
    return None


class AnthropicLLM:
    provider = "anthropic"

    # Server-side refusal fallback: if a safety classifier declines, the API re-runs the
    # request on a suitable fallback model inside the same call.
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, api_key: str, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=300)
        self.client = client

    def structured(self, *, model, system, text, schema, image_jpeg=None, effort="high"):
        import anthropic

        content: list[dict] = []
        if image_jpeg is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image_jpeg).decode("utf-8"),
                    },
                }
            )
        content.append({"type": "text", "text": text})
        try:
            resp = self.client.beta.messages.parse(
                model=model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=schema,
                output_config={"effort": effort},
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as exc:
            raise LLMError(f"Anthropic rejected the request: {exc}", retryable=False) from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic bad request: {exc}", retryable=False) from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise LLMError(f"Anthropic temporarily unavailable: {exc}", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic error {exc.status_code}: {exc}", retryable=exc.status_code >= 500) from exc
        except Exception as exc:  # e.g. the output failed schema validation
            raise LLMError(f"Anthropic response could not be parsed: {exc}") from exc

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"Anthropic refused (category: {category})", retryable=False)
        if resp.stop_reason == "max_tokens":
            raise LLMError("Anthropic response hit max_tokens before finishing")
        if resp.parsed_output is None:
            raise LLMError("Anthropic returned no structured output")
        return resp.parsed_output


def make_llm(settings: Settings) -> LLM:
    provider = settings.resolved_provider()
    if provider == "openai":
        return OpenAILLM(settings.openai_api_key or "", image_detail=settings.image_detail)
    return AnthropicLLM(settings.anthropic_api_key or "")
