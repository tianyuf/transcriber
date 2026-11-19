"""Route vision requests through OpenRouter or a provider's native HTTP API.

Native models use provider:model syntax. OpenRouter model IDs keep their usual owner/model form.
"""
from __future__ import annotations

import base64
import os

import httpx

from . import openrouter
from .openrouter import APIError, FatalAPIError, SPEND

# Stable examples, not automatic "latest" aliases. Other vision-capable model IDs can be supplied.
DIRECT_EXAMPLES = {
    "gemini": "gemini:gemini-3.1-flash-lite",
    "grok": "grok:grok-4.3",
    "openai": "openai:gpt-6-luna",
    "anthropic": "anthropic:claude-haiku-5-5",
}
PROVIDER_ENV = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "grok": ("XAI_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
}
PREFIXES = {"gemini": "gemini", "google": "gemini", "grok": "grok", "xai": "grok",
            "openai": "openai", "anthropic": "anthropic"}
BASE_ENV = {
    "gemini": ("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1"),
    "grok": ("XAI_BASE_URL", "https://api.x.ai/v1"),
    "openai": ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "anthropic": ("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
}
# Standard, uncached, lowest-prompt-tier USD/token rates checked 2026-11-18.
# Unknown custom models return no estimate; large prompts and other service tiers cost more.
NATIVE_PRICING = {
    "gemini:gemini-3.1-flash-lite": (0.25 / 1e6, 1.50 / 1e6),
    "grok:grok-4.3": (1.25 / 1e6, 2.50 / 1e6),
    "openai:gpt-6-luna": (0.10 / 1e6, 0.50 / 1e6),
    "anthropic:claude-haiku-5-5": (0.10 / 1e6, 0.50 / 1e6),
}
# Automatic failure recovery uses non-Chinese model families on OpenRouter so the normal
# OpenRouter credential works for both models. The first distinct family is selected.
DEFAULT_FALLBACK_MODEL = "google/gemini-3.1-flash-lite"
DEFAULT_FALLBACKS = (
    DEFAULT_FALLBACK_MODEL,
    "openai/gpt-6-luna",
    "anthropic/claude-haiku-5.5",
    "x-ai/grok-4.3",
)
RETRY_STATUSES = {408, 409, 425, 429}


def resolve_model(name: str) -> str:
    """Resolve an OpenRouter alias or canonicalize a native provider:model ID."""
    name = name.strip()
    if not name:
        raise SystemExit("model name cannot be empty")
    prefix, sep, model = name.partition(":")
    if sep and prefix.lower() == "openrouter":
        return openrouter.resolve_model(model)
    if sep and prefix.lower() in PREFIXES:
        if not model:
            raise SystemExit(f"native model syntax is {prefix}:MODEL; see `transcriber models`")
        return f"{PREFIXES[prefix.lower()]}:{model}"
    return openrouter.resolve_model(name)


def split_model(name: str) -> tuple[str, str]:
    prefix, sep, model = name.partition(":")
    if sep and prefix in PREFIXES.values():
        return prefix, model
    return "openrouter", name


def model_identity(name: str) -> str:
    """Identify the underlying family/model across native and OpenRouter routing."""
    provider, model = split_model(name)
    if provider != "openrouter":
        return f"{provider}:{model}"
    owner, sep, model_id = model.partition("/")
    owner = {"google": "gemini", "x-ai": "grok"}.get(owner, owner)
    return f"{owner}:{model_id}" if sep else model


def default_fallback(models: list[str]) -> str:
    """Choose the first non-Chinese fallback that differs from every selected model."""
    used = {model_identity(model) for model in models}
    for candidate in DEFAULT_FALLBACKS:
        if model_identity(candidate) not in used:
            return candidate
    raise SystemExit("no distinct default fallback is available; specify --fallback-model")


def credentials_for(models: list[str], credentials: dict[str, str] | None = None,
                    deny_data_collection: bool = False) -> dict[str, str]:
    """Fill a shared credential mapping without displaying or persisting native keys."""
    result = {} if credentials is None else credentials
    selected = [(name, *split_model(name)) for name in models]
    if deny_data_collection:
        native = [name for name, provider, _ in selected if provider != "openrouter"]
        if native:
            raise SystemExit("--deny-data-collection is enforceable only through OpenRouter; "
                             f"replace native model(s) {', '.join(native)} with OpenRouter models")
    for name, provider, _ in selected:
        if provider in result:
            continue
        if provider == "openrouter":
            from .auth import require_key
            result[provider] = require_key()
            continue
        from .auth import stored_key
        key, _ = stored_key(provider)
        if key:
            result[provider] = key
        else:
            names = " or ".join(PROVIDER_ENV[provider])
            raise SystemExit(f"{name} requires {names} in the environment or `transcriber auth login --provider "
                             f"{provider}`. Transcriber never prints the key")
    return result


def fetch_pricing(name: str) -> tuple[float, float] | None:
    provider, model = split_model(name)
    if provider == "openrouter":
        return openrouter.fetch_pricing(model)
    return NATIVE_PRICING.get(name)


def supports_temperature(name: str) -> bool:
    """Whether call_vision will actually send the retry temperature to this model."""
    provider, model = split_model(name)
    if provider == "openrouter" or provider == "grok":
        return True
    if provider == "openai":
        return model == "gpt-6-luna"
    if provider == "gemini":
        return model == "gemini-3.1-flash-lite"
    return model not in ("claude-haiku-5-5", "claude-sonnet-5-5")


def _error_message(resp: httpx.Response) -> str:
    try:
        error = resp.json().get("error", {})
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        return str(error)[:300]
    except Exception:  # noqa: BLE001
        return resp.text[:300].replace("\n", " ")


def _post(client: httpx.Client, provider: str, url: str, payload: dict, headers: dict, timeout: float) -> httpx.Response:
    resp = client.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code in RETRY_STATUSES or resp.status_code >= 500:
        raise APIError(f"{provider} HTTP {resp.status_code}: {_error_message(resp)}")
    if resp.status_code >= 400:
        raise FatalAPIError(f"{provider} API error HTTP {resp.status_code}: {_error_message(resp)}")
    return resp


def _json(resp: httpx.Response, provider: str) -> dict:
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise APIError(f"{provider} returned malformed JSON: {e}") from e
    if not isinstance(data, dict):
        raise APIError(f"{provider} returned malformed JSON: expected an object")
    return data


def _record_usage(input_tokens: int, output_tokens: int) -> None:
    SPEND.add({"prompt_tokens": input_tokens, "completion_tokens": output_tokens})


def _responses(client: httpx.Client, provider: str, model: str, prompt: str, b64: str, mime: str,
               timeout: float, temperature: float, key: str) -> tuple[str, str | None]:
    """OpenAI and xAI Responses APIs; both take image data URLs and return output items."""
    env, default = BASE_ENV[provider]
    base = os.environ.get(env, default).rstrip("/")
    payload = {
        "model": model,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"},
        ]}],
        "max_output_tokens": 8192,
        "store": False,
    }
    if (provider, model) in {("openai", "gpt-6-luna"), ("grok", "grok-4.3")}:
        payload["reasoning"] = {"effort": "none"}
    if provider == "grok" or model == "gpt-6-luna":
        payload["temperature"] = temperature
    resp = _post(client, provider, f"{base}/responses", payload,
                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout)
    data = _json(resp, provider)
    usage = data.get("usage") or {}
    _record_usage(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))
    texts = []
    refused = False
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text":
                texts.append(str(part.get("text") or ""))
            elif part.get("type") == "refusal":
                refused = True
    if refused:
        return "", "refusal"
    status = data.get("status")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        text = "".join(texts)
        if reason in ("max_output_tokens", "max_messages"):
            return text, "length"
        if reason == "content_filter":
            return "", "refusal"
        raise APIError(f"{provider} incomplete response: {reason or 'unknown reason'}", partial=text or None,
                       degenerate=reason != "max_time_limit")
    if status != "completed":
        raise APIError(f"{provider} response status {status or 'unknown'}: {str(data.get('error') or '')[:160]}")
    return "".join(texts), "stop"


def _gemini(client: httpx.Client, model: str, prompt: str, b64: str, mime: str,
            timeout: float, temperature: float, key: str) -> tuple[str, str | None]:
    env, default = BASE_ENV["gemini"]
    base = os.environ.get(env, default).rstrip("/")
    generation = {"max_output_tokens": 8192}
    if model == "gemini-3.1-flash-lite":
        generation["temperature"] = temperature
    if model in ("gemini-3.1-flash-lite", "gemini-3.6-flash"):
        generation["thinking_level"] = "minimal"
    elif model == "gemini-3.8-flash":
        generation["thinking_level"] = "low"  # minimal is rejected by this model
    payload = {
        "model": model,
        "input": [
            {"type": "text", "text": prompt},
            {"type": "image", "data": b64, "mime_type": mime},
        ],
        "generation_config": generation,
        "store": False,
    }
    resp = _post(client, "gemini", f"{base}/interactions", payload,
                 {"x-goog-api-key": key, "Content-Type": "application/json"}, timeout)
    data = _json(resp, "gemini")
    usage = data.get("usage") or {}
    output_tokens = int(usage.get("total_output_tokens") or 0) + int(usage.get("total_thought_tokens") or 0)
    _record_usage(int(usage.get("total_input_tokens") or 0), output_tokens)
    texts = []
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for part in step.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
    status = data.get("status")
    if status == "completed":
        return "".join(texts), "stop"
    if status in ("incomplete", "requires_continuation"):
        return "".join(texts), "length"
    raise APIError(f"gemini interaction status {status or 'unknown'}: {str(data.get('errors') or '')[:160]}")


def _anthropic(client: httpx.Client, model: str, prompt: str, b64: str, mime: str,
               timeout: float, temperature: float, key: str) -> tuple[str, str | None]:
    env, default = BASE_ENV["anthropic"]
    base = os.environ.get(env, default).rstrip("/")
    payload = {
        "model": model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    }
    if model == "claude-haiku-5-5":
        payload["thinking"] = {"type": "disabled"}
    elif model == "claude-sonnet-5-5":
        payload["thinking"] = {"type": "between_tools"}
    else:
        payload["temperature"] = temperature
    # Current Anthropic API keys use Bearer authentication; x-api-key is the legacy alternative.
    headers = {"Authorization": f"Bearer {key}", "anthropic-version": "2026-10-21",
               "Content-Type": "application/json"}
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
        headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
    resp = _post(client, "anthropic", f"{base}/messages", payload, headers, timeout)
    data = _json(resp, "anthropic")
    usage = data.get("usage") or {}
    input_tokens = sum(int(usage.get(k) or 0) for k in
                       ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    _record_usage(input_tokens, int(usage.get("output_tokens") or 0))
    content = data.get("content")
    if not isinstance(content, list):
        raise APIError(f"anthropic malformed response: {str(data)[:200]}")
    if any(isinstance(part, dict) and part.get("type") == "refusal" for part in content):
        return "", "refusal"
    text = "".join(str(part.get("text") or "") for part in content
                   if isinstance(part, dict) and part.get("type") == "text")
    stop = data.get("stop_reason")
    if stop in ("end_turn", "stop_sequence"):
        return text, stop
    if stop in ("max_tokens", "model_context_window_exceeded"):
        return text, "length"
    if stop == "refusal":
        return "", "refusal"
    raise APIError(f"anthropic non-final or unknown stop reason: {stop or 'missing'}",
                   partial=text or None, degenerate=True)


def call_vision(client: httpx.Client, credentials: dict[str, str], name: str, prompt: str,
                image: bytes, mime: str, timeout: float, temperature: float = 0.0,
                deny_data_collection: bool = False) -> tuple[str, str | None]:
    """Send one page to the selected provider. Returns (text, normalized finish reason)."""
    provider, model = split_model(name)
    if provider == "openrouter":
        return openrouter.call_vision(client, credentials["openrouter"], model, prompt, image, mime,
                                      timeout, temperature, deny_data_collection)
    if deny_data_collection:
        raise FatalAPIError("native provider APIs cannot enforce --deny-data-collection; use OpenRouter")
    key = credentials.get(provider)
    if not key:
        raise FatalAPIError(f"missing credential for {provider}; set {PROVIDER_ENV[provider][0]}")
    b64 = base64.b64encode(image).decode("ascii")
    if provider == "gemini":
        return _gemini(client, model, prompt, b64, mime, timeout, temperature, key)
    if provider == "anthropic":
        return _anthropic(client, model, prompt, b64, mime, timeout, temperature, key)
    return _responses(client, provider, model, prompt, b64, mime, timeout, temperature, key)
