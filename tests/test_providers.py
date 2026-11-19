import base64
import json

import httpx
import pytest

from transcriber import auth, cli, providers
from transcriber.openrouter import APIError, FatalAPIError, SPEND


@pytest.fixture(autouse=True)
def reset_usage():
    SPEND.cost = 0.0
    SPEND.input_tokens = SPEND.output_tokens = SPEND.calls = SPEND.cost_calls = 0


def request_call(name, response, credential=None):
    provider, _ = providers.split_model(name)
    captured = {}

    def handler(request):
        captured["request"] = request
        captured["json"] = json.loads(request.content)
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = providers.call_vision(client, {provider: credential or "test-key"}, name, "Transcribe",
                                       b"fake-image", "image/png", 1)
    return result, captured


def test_model_resolution_distinguishes_native_and_openrouter():
    assert providers.resolve_model("flash") == "qwen/qwen3.7-flash"
    assert providers.resolve_model("openai/gpt-4.1-mini") == "openai/gpt-4.1-mini"
    assert providers.resolve_model("openai:gpt-6-luna") == "openai:gpt-6-luna"
    assert providers.resolve_model("google:gemini-3.1-flash-lite") == "gemini:gemini-3.1-flash-lite"
    assert providers.resolve_model("xai:grok-4.3") == "grok:grok-4.3"
    assert providers.resolve_model("openrouter:gemini") == "google/gemini-3.1-flash-lite"
    with pytest.raises(SystemExit, match="native model syntax"):
        providers.resolve_model("openai:")


def test_native_credentials_come_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("XAI_API_KEY", "secret-grok")
    result = providers.credentials_for(["openai:gpt-6-luna", "grok:grok-4.3"])
    assert result == {"openai": "secret-openai", "grok": "secret-grok"}


def test_mixed_openrouter_and_native_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setattr(auth, "require_key", lambda: "secret-openrouter")
    result = providers.credentials_for(["flash", "openai:gpt-6-luna"])
    assert result == {"openrouter": "secret-openrouter", "openai": "secret-openai"}


def test_missing_native_credential_names_environment_variable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(auth, "stored_key", lambda provider: (None, ""))
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        providers.credentials_for(["anthropic:claude-haiku-5-5"])


def test_native_privacy_flag_fails_closed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    with pytest.raises(SystemExit, match="only through OpenRouter"):
        providers.credentials_for(["openai:gpt-6-luna"], deny_data_collection=True)


@pytest.mark.parametrize("name,base_env,base_url", [
    ("openai:gpt-6-luna", "OPENAI_BASE_URL", "https://openai.test/v1"),
    ("grok:grok-4.3", "XAI_BASE_URL", "https://xai.test/v1"),
])
def test_responses_api_requests(monkeypatch, name, base_env, base_url):
    monkeypatch.setenv(base_env, base_url)
    response = httpx.Response(200, json={
        "status": "completed",
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "hidden"}]},
            {"type": "message", "content": [
                {"type": "output_text", "text": "Dear "},
                {"type": "output_text", "text": "Madame,"},
            ]},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 12},
    })
    (text, finish), captured = request_call(name, response)
    payload = captured["json"]
    request = captured["request"]
    assert text == "Dear Madame," and finish == "stop"
    assert request.url == f"{base_url}/responses"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert payload["store"] is False and payload["reasoning"] == {"effort": "none"}
    assert payload["max_output_tokens"] == 8192 and payload["temperature"] == 0.0
    content = payload["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "Transcribe"}
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert base64.b64decode(content[1]["image_url"].split(",", 1)[1]) == b"fake-image"
    assert SPEND.input_tokens == 100 and SPEND.output_tokens == 12 and SPEND.cost_calls == 0


def test_responses_truncation_and_refusal():
    partial = httpx.Response(200, json={
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
    })
    assert request_call("openai:gpt-6-luna", partial)[0] == ("partial", "length")
    refusal = httpx.Response(200, json={
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "cannot help"}]}],
    })
    assert request_call("openai:gpt-6-luna", refusal)[0] == ("", "refusal")


def test_gemini_interactions_request_and_response(monkeypatch):
    monkeypatch.setenv("GEMINI_BASE_URL", "https://gemini.test/v1")
    response = httpx.Response(200, json={
        "status": "completed",
        "steps": [
            {"type": "thought", "content": [{"type": "text", "text": "ignore"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "Page text"}]},
        ],
        "usage": {"total_input_tokens": 90, "total_output_tokens": 8, "total_thought_tokens": 3},
    })
    (text, finish), captured = request_call("gemini:gemini-3.1-flash-lite", response)
    payload = captured["json"]
    assert text == "Page text" and finish == "stop"
    assert captured["request"].url == "https://gemini.test/v1/interactions"
    assert captured["request"].headers["x-goog-api-key"] == "test-key"
    assert payload["store"] is False and payload["generation_config"] == {
        "max_output_tokens": 8192, "thinking_level": "minimal", "temperature": 0.0,
    }
    assert payload["input"][1] == {
        "type": "image", "data": base64.b64encode(b"fake-image").decode(), "mime_type": "image/png",
    }
    assert SPEND.input_tokens == 90 and SPEND.output_tokens == 11


def test_gemini_incomplete_preserves_partial_text():
    response = httpx.Response(200, json={
        "status": "incomplete",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "partial"}]}],
    })
    assert request_call("gemini:gemini-3.1-flash-lite", response)[0] == ("partial", "length")


def test_anthropic_messages_request_and_response(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.test/v1")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "workspace-test")
    response = httpx.Response(200, json={
        "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": "ignore"}, {"type": "text", "text": "Page text"}],
        "usage": {"input_tokens": 50, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 4,
                  "output_tokens": 8},
    })
    (text, finish), captured = request_call("anthropic:claude-haiku-5-5", response)
    payload = captured["json"]
    request = captured["request"]
    assert text == "Page text" and finish == "end_turn"
    assert request.url == "https://anthropic.test/v1/messages"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["anthropic-version"] == "2026-10-21"
    assert request.headers["anthropic-workspace-id"] == "workspace-test"
    assert payload["thinking"] == {"type": "disabled"} and "temperature" not in payload
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[1] == {"type": "text", "text": "Transcribe"}
    assert base64.b64decode(content[0]["source"]["data"]) == b"fake-image"
    assert SPEND.input_tokens == 59 and SPEND.output_tokens == 8


def test_anthropic_truncation_and_refusal():
    truncated = httpx.Response(200, json={
        "stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}],
    })
    assert request_call("anthropic:claude-haiku-5-5", truncated)[0] == ("partial", "length")
    refusal = httpx.Response(200, json={"stop_reason": "refusal", "content": []})
    assert request_call("anthropic:claude-haiku-5-5", refusal)[0] == ("", "refusal")


def test_unfinished_responses_are_not_accepted_as_complete():
    response = httpx.Response(200, json={
        "status": "incomplete", "incomplete_details": {"reason": "max_prompt_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
    })
    with pytest.raises(APIError, match="max_prompt_tokens"):
        request_call("openai:gpt-6-luna", response)
    response = httpx.Response(200, json={
        "stop_reason": "pause_turn", "content": [{"type": "text", "text": "partial"}],
    })
    with pytest.raises(APIError, match="pause_turn"):
        request_call("anthropic:claude-haiku-5-5", response)


def test_non_object_json_is_a_retryable_api_error():
    with pytest.raises(APIError, match="malformed JSON"):
        request_call("openai:gpt-6-luna", httpx.Response(200, json=[]))
    with pytest.raises(APIError, match="malformed JSON"):
        request_call("qwen/qwen3.7-flash", httpx.Response(200, content=b"not json"))
    with pytest.raises(APIError, match="malformed JSON"):
        request_call("qwen/qwen3.7-flash", httpx.Response(200, json=[]))


def test_unknown_anthropic_stop_reason_is_not_success():
    response = httpx.Response(200, json={"content": [{"type": "text", "text": "unexpected"}]})
    with pytest.raises(APIError, match="stop reason"):
        request_call("anthropic:claude-haiku-5-5", response)


def test_retryable_and_fatal_http_errors():
    with pytest.raises(APIError, match="HTTP 429"):
        request_call("openai:gpt-6-luna", httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(FatalAPIError, match="HTTP 401"):
        request_call("openai:gpt-6-luna", httpx.Response(401, json={"error": {"message": "bad key"}}))


def test_native_price_estimates_and_unknown_custom_models():
    assert providers.fetch_pricing("openai:gpt-6-luna") == (0.1 / 1e6, 0.5 / 1e6)
    assert providers.fetch_pricing("grok:grok-4.3") == (1.25 / 1e6, 2.5 / 1e6)
    assert providers.fetch_pricing("gemini:gemini-3.1-flash-lite") == (0.25 / 1e6, 1.5 / 1e6)
    assert providers.fetch_pricing("anthropic:claude-haiku-5-5") == (0.1 / 1e6, 0.5 / 1e6)
    assert providers.fetch_pricing("openai:custom-vision-model") is None


def test_temperature_capability_matches_native_payload_rules():
    assert providers.supports_temperature("openai:gpt-6-luna")
    assert providers.supports_temperature("grok:grok-4.3")
    assert providers.supports_temperature("gemini:gemini-3.1-flash-lite")
    assert providers.supports_temperature("qwen/qwen3.7-flash")
    assert not providers.supports_temperature("anthropic:claude-haiku-5-5")
    assert not providers.supports_temperature("openai:custom-reasoning-model")


def test_models_command_includes_openrouter_and_native(monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_pricing", lambda model: (1 / 1e6, 2 / 1e6))
    assert cli.cmd_models(None) == 0
    output = capsys.readouterr().out
    assert "OpenRouter" in output and "Direct provider APIs" in output
    for provider in ("gemini", "grok", "openai", "anthropic"):
        assert provider in output


def test_default_fallback_is_non_chinese_and_distinct():
    assert providers.default_fallback(["flash"]) == "google/gemini-3.1-flash-lite"
    assert providers.default_fallback(["google/gemini-3.1-flash-lite"]) == "openai/gpt-6-luna"
    # Native Gemini and OpenRouter Gemini are the same underlying model for fallback selection.
    assert providers.default_fallback(["gemini:gemini-3.1-flash-lite"]) == "openai/gpt-6-luna"
    assert providers.default_fallback(["google/gemini-3.1-flash-lite", "openai/gpt-6-luna"]) == \
        "anthropic/claude-haiku-5.5"


def test_default_and_disabled_fallback_settings():
    args = cli.build_parser().parse_args(["transcribe", "scan.pdf"])
    assert cli.settings_from(args).fallback_model == "google/gemini-3.1-flash-lite"
    args = cli.build_parser().parse_args(["transcribe", "scan.pdf", "--no-fallback"])
    assert cli.settings_from(args).fallback_model is None


def test_fallback_must_differ_from_primary():
    args = cli.build_parser().parse_args(["transcribe", "scan.pdf", "--model", "flash", "--fallback-model", "flash"])
    with pytest.raises(SystemExit, match="must be different"):
        cli.settings_from(args)
