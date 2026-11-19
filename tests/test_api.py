import importlib
import json

import pymupdf
import pytest

import transcriber


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(path)
    doc.close()
    return path


def test_transcribe_file_returns_structured_result(monkeypatch, pdf, tmp_path):
    module = importlib.import_module("transcriber.transcribe")
    seen = {}
    monkeypatch.setattr(module, "estimate", lambda *args: (1, 0.001))

    def fake_run(sources, settings, out, credentials, pages, force):
        seen["settings"] = settings
        run_log = out / "sample" / "run.json"
        run_log.parent.mkdir(parents=True)
        run_log.write_text(json.dumps({"pages": {"1": {"model": settings.model}}}))
        (out / "sample.md").write_text("text")
        return 1, 0

    monkeypatch.setattr(module, "run", fake_run)
    result = transcriber.transcribe_file(pdf, output=tmp_path / "result")
    assert seen["settings"].fallback_model == "google/gemini-3.1-flash-lite"
    assert result.ok and result.pages_done == 1 and result.pages_failed == 0
    assert result.markdown == tmp_path / "result" / "sample.md"
    assert result.run_log == tmp_path / "result" / "sample" / "run.json"
    assert result.models == ("qwen/qwen3.7-flash",)


def test_transcribe_file_can_disable_fallback(monkeypatch, pdf, tmp_path):
    module = importlib.import_module("transcriber.transcribe")
    monkeypatch.setattr(module, "estimate", lambda *args: (0, 0.0))
    seen = {}
    monkeypatch.setattr(module, "run", lambda sources, settings, *args: seen.setdefault("settings", settings) and (0, 0))
    transcriber.transcribe_file(pdf, output=tmp_path, use_fallback=False)
    assert seen["settings"].fallback_model is None


def test_transcribe_file_converts_system_exit(monkeypatch, pdf, tmp_path):
    module = importlib.import_module("transcriber.transcribe")
    monkeypatch.setattr(module, "estimate", lambda *args: (1, 0.001))
    monkeypatch.setattr(module, "run", lambda *args: (_ for _ in ()).throw(SystemExit("missing credential")))
    with pytest.raises(transcriber.TranscriberError, match="missing credential"):
        transcriber.transcribe_file(pdf, output=tmp_path)


def test_transcribe_file_enforces_unknown_max_cost(monkeypatch, pdf, tmp_path):
    module = importlib.import_module("transcriber.transcribe")
    monkeypatch.setattr(module, "estimate", lambda *args: (1, None))
    with pytest.raises(transcriber.TranscriberError, match="price is unknown"):
        transcriber.transcribe_file(pdf, output=tmp_path, max_cost=1)


def test_validate_file_returns_artifact_paths(monkeypatch, pdf, tmp_path):
    module = importlib.import_module("transcriber.validate")
    monkeypatch.setattr(module, "validate", lambda *args, **kwargs: 3)
    result = transcriber.validate_file(
        pdf, dir_a=tmp_path / "a", dir_b=tmp_path / "b", offline=True, use_fallback=False,
    )
    assert result.flagged_pages == 3
    assert result.merged == tmp_path / "a" / "sample.merged.md"
    assert result.comparison == tmp_path / "a" / "sample.comparison.html"
    assert result.review == tmp_path / "a" / "sample.review.html"
