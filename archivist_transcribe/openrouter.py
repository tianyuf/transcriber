"""Talking to OpenRouter: model names, prices, the chat call, and what calls actually cost."""
from __future__ import annotations

import base64
import os
import threading

import httpx

# OPENROUTER_BASE_URL lets the test suite point the tool at a local mock server.
BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai").rstrip("/")
API = f"{BASE_URL}/api/v1"

DEFAULT_MODEL = "qwen/qwen3.7-flash"
MODEL_ALIASES = {
    "flash": "qwen/qwen3.7-flash",
    "30b": "qwen/qwen3-vl-30b-a3b-instruct",
    "72b": "qwen/qwen2.5-vl-72b-instruct",
    "235b": "qwen/qwen3-vl-235b-a22b-instruct",
    "gemini": "google/gemini-2.5-flash",
    "gemma": "google/gemma-4-26b-a4b-it",
}
# What each alias is good for, shown by `archivist models`.
MODEL_NOTES = {
    "flash": "default: fast and very cheap, good on print and typescript in most languages; served by Alibaba, may refuse politically sensitive pages",
    "30b": "fallback when flash refuses a page; faithful, rarely 'corrects' the source",
    "72b": "second opinion for validation on CJK material",
    "235b": "strongest Qwen; slow, use as tiebreaker (validate -c) or for hard pages only",
    "gemini": "independent second opinion for validation; strong on typed and handwritten European languages",
    "gemma": "cheap, served outside China",
}


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


class APIError(Exception):
    """A failure worth retrying (rate limit, server error, malformed or empty response)."""

    def __init__(self, msg: str, partial: str | None = None, degenerate: bool = False):
        super().__init__(msg)
        self.partial = partial  # truncated text worth keeping if every attempt fails
        self.degenerate = degenerate  # truncation or looping: an identical retry would repeat it


class FatalAPIError(SystemExit):
    """Bad key, bad model id, no credit: retrying will not help."""


class Spend:
    """What the API calls actually cost, as OpenRouter reports it (failed attempts included)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def add(self, usage: dict | None) -> None:
        if not usage:
            return
        with self.lock:
            self.calls += 1
            self.cost += float(usage.get("cost") or 0)
            self.input_tokens += int(usage.get("prompt_tokens") or 0)
            self.output_tokens += int(usage.get("completion_tokens") or 0)


SPEND = Spend()


def fetch_pricing(model: str) -> tuple[float, float] | None:
    """(dollars per input token, per output token) from OpenRouter's public model list."""
    try:
        data = httpx.get(f"{API}/models", timeout=20).json()["data"]
    except Exception:  # noqa: BLE001 - an estimate is a nicety, never a reason to stop
        return None
    for m in data:
        if m.get("id") == model:
            price = m.get("pricing") or {}
            try:
                return float(price.get("prompt") or 0), float(price.get("completion") or 0)
            except (TypeError, ValueError):
                return None
    return None


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json()["error"]["message"])[:300]
    except Exception:  # noqa: BLE001
        return resp.text[:300].replace("\n", " ")


FATAL_HINTS = {
    401: "OpenRouter rejected the key. Run `archivist auth login` to sign in again.",
    402: "The OpenRouter account is out of credit or over its key's limit. Add credit at https://openrouter.ai/settings/credits.",
    403: "OpenRouter refused the request (moderation, or a provider or data policy that excludes every provider of this model).",
    404: "No provider serves this model under the current settings: check the model id (`archivist models`) or your OpenRouter privacy settings.",
}


def call_vision(client: httpx.Client, api_key: str, model: str, prompt: str, image: bytes, mime: str,
                timeout: float, temperature: float = 0.0, deny_data_collection: bool = False) -> tuple[str, str | None]:
    """Send one page image with the prompt. Returns (text, finish_reason)."""
    b64 = base64.b64encode(image).decode("ascii")
    payload: dict = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "temperature": temperature,
        "max_tokens": 8192,
        "usage": {"include": True},  # OpenRouter then reports what the call cost
        # Transcription needs no thinking: reasoning models otherwise spend most of their
        # (billed, slow) output on it. Ignored by models that do not reason.
        "reasoning": {"enabled": False},
    }
    if deny_data_collection:
        payload["provider"] = {"data_collection": "deny"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "archivist-transcribe",
    }
    resp = client.post(f"{API}/chat/completions", json=payload, headers=headers, timeout=timeout)
    if resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500:
        raise APIError(f"HTTP {resp.status_code}: {_error_message(resp)}")
    if resp.status_code >= 400:
        hint = FATAL_HINTS.get(resp.status_code, "Check the model id and your OpenRouter account.")
        raise FatalAPIError(f"OpenRouter error HTTP {resp.status_code}: {_error_message(resp)}\n{hint}")
    data = resp.json()
    SPEND.add(data.get("usage"))
    if "error" in data:
        raise APIError(f"API error: {data['error']}")
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise APIError(f"Malformed response: {e}: {str(data)[:200]}")
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or "", choice.get("finish_reason")


def money(x: float) -> str:
    return f"${x:.4f}" if x < 0.1 else f"${x:.2f}"
