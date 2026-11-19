import json

import pytest

from transcriber import transcribe
from transcriber.openrouter import APIError, FatalAPIError


class DummySource:
    kind = "pdf"
    name = "scan.pdf"
    stem = "scan"
    files = []

    def render(self, index, dpi):
        return b"image", "image/png"

    def label(self, index):
        return f"page {index + 1}"

    def page_count(self):
        return 1

    def image_tokens(self, index, dpi):
        return 1000


def settings(retries=2, fallback="fallback"):
    return transcribe.Settings(
        model="primary", fallback_model=fallback, profile="printed", language=None,
        layout="paragraphs", diplomatic=False, dpi=150, workers=1, retries=retries,
        timeout=1, deny_data_collection=False,
    )


def install_models(monkeypatch, behavior):
    calls = []

    def fake_call(client, credentials, model, prompt, image, mime, timeout, temperature, deny):
        calls.append(model)
        result = behavior(model, calls.count(model))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(transcribe, "call_vision", fake_call)
    monkeypatch.setattr(transcribe.time, "sleep", lambda seconds: None)
    return calls


def test_fallback_runs_per_page_after_primary_retries(monkeypatch):
    calls = install_models(monkeypatch, lambda model, attempt: (_ for _ in ()).throw(APIError("unavailable"))
                                  if model == "primary" else ("fallback text", "stop"))
    result = transcribe.transcribe_page(DummySource(), 0, settings(), {}, lambda message: None)
    assert calls == ["primary", "primary", "fallback"]
    assert result.text == "fallback text" and result.model == "fallback"
    assert result.attempts == 3 and result.used_fallback


def test_primary_success_does_not_call_fallback(monkeypatch):
    calls = install_models(monkeypatch, lambda model, attempt: ("primary text", "stop"))
    result = transcribe.transcribe_page(DummySource(), 0, settings(), {}, lambda message: None)
    assert calls == ["primary"] and result.model == "primary" and not result.used_fallback


def test_fatal_primary_error_can_fall_back(monkeypatch):
    calls = install_models(monkeypatch, lambda model, attempt: FatalAPIError("unknown model")
                           if model == "primary" else ("recovered", "stop"))
    result = transcribe.transcribe_page(DummySource(), 0, settings(), {}, lambda message: None)
    assert calls == ["primary", "fallback"] and result.text == "recovered"


def test_longest_partial_is_kept_if_all_models_fail(monkeypatch):
    def behavior(model, attempt):
        if model == "primary":
            return "partial primary text", "length"
        raise APIError("still unavailable")

    calls = install_models(monkeypatch, behavior)
    result = transcribe.transcribe_page(DummySource(), 0, settings(retries=1), {}, lambda message: None)
    assert calls == ["primary", "fallback"]
    assert result.truncated and result.model == "primary" and not result.used_fallback
    assert result.text.startswith("partial primary text") and result.attempts == 2


def test_fatal_fallback_error_still_preserves_primary_partial(monkeypatch):
    def behavior(model, attempt):
        if model == "primary":
            return "partial primary text", "length"
        raise FatalAPIError("fallback credential rejected")

    install_models(monkeypatch, behavior)
    result = transcribe.transcribe_page(DummySource(), 0, settings(retries=1), {}, lambda message: None)
    assert result.truncated and result.model == "primary"
    assert result.text.startswith("partial primary text")


def test_fixed_sampling_model_skips_identical_retries(monkeypatch):
    def behavior(model, attempt):
        if model.startswith("anthropic:"):
            raise APIError("repetition loop", degenerate=True)
        return "recovered", "stop"

    calls = install_models(monkeypatch, behavior)
    s = settings(retries=3)
    s.model = "anthropic:claude-haiku-5-5"
    result = transcribe.transcribe_page(DummySource(), 0, s, {}, lambda message: None)
    assert calls == ["anthropic:claude-haiku-5-5", "fallback"]
    assert result.text == "recovered" and result.attempts == 2


def test_run_log_records_the_model_that_produced_the_page(tmp_path):
    s = settings()
    result = transcribe.PageResult(index=0, text="recovered", model="fallback", attempts=3,
                                   seconds=1.2, used_fallback=True)
    log = transcribe.RunLog(tmp_path / "run.json")
    log.record(DummySource(), s, result)
    data = json.loads((tmp_path / "run.json").read_text())
    page = data["pages"]["1"]
    assert data["fallback_model"] == "fallback"
    assert page["model"] == "fallback" and page["fallback_from"] == "primary"
    assert page["used_fallback"] and page["attempts"] == 3


def test_estimate_includes_worst_case_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "fetch_pricing", lambda model: {
        "primary": (1 / 1e6, 2 / 1e6),
        "fallback": (3 / 1e6, 4 / 1e6),
    }[model])
    total, cost = transcribe.estimate([DummySource()], settings(), tmp_path, None, False)
    # 1,600 estimated input tokens and 700 output tokens for each model.
    assert total == 1 and cost == pytest.approx((1600 * 4 + 700 * 6) / 1e6)
