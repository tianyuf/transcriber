"""Public, synchronous Python API for embedding Transcriber in other programs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .openrouter import DEFAULT_MODEL
from .providers import default_fallback, model_identity, resolve_model
from .sources import Source


class TranscriberError(RuntimeError):
    """A configuration, credential, cost-limit, or provider error raised through the public API."""


@dataclass(frozen=True)
class TranscriptionResult:
    source: Path
    output: Path
    markdown: Path
    run_log: Path
    pages_done: int
    pages_failed: int
    models: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.pages_failed == 0


@dataclass(frozen=True)
class ValidationResult:
    source: Path
    flagged_pages: int
    merged: Path
    comparison: Path
    review: Path


def transcribe_file(
    source: str | Path,
    *,
    output: str | Path = "output",
    model: str = DEFAULT_MODEL,
    fallback_model: str | None = None,
    use_fallback: bool = True,
    profile: str = "printed",
    language: str | None = None,
    notation: str | None = None,
    layout: str = "paragraphs",
    diplomatic: bool = False,
    dpi: int = 150,
    workers: int = 4,
    retries: int = 5,
    timeout: float = 180.0,
    pages: str | None = None,
    force: bool = False,
    max_cost: float | None = None,
    deny_data_collection: bool = False,
    credentials: Mapping[str, str] | None = None,
) -> TranscriptionResult:
    """Transcribe one PDF, image, or image folder and return structured output metadata.

    Model names accept OpenRouter aliases/IDs and native provider:model IDs. Credentials are
    resolved from the environment or system keychain unless supplied explicitly by provider name.
    """
    from . import transcribe

    if not use_fallback and fallback_model:
        raise TranscriberError("fallback_model cannot be combined with use_fallback=False")
    primary = resolve_model(model)
    fallback = None
    if use_fallback:
        fallback = resolve_model(fallback_model) if fallback_model else default_fallback([primary])
        if model_identity(primary) == model_identity(fallback):
            raise TranscriberError("fallback_model must differ from model")
    settings = transcribe.Settings(
        model=primary, fallback_model=fallback, profile=profile, language=language, notation=notation,
        layout=layout, diplomatic=diplomatic, dpi=dpi, workers=workers, retries=retries,
        timeout=timeout, deny_data_collection=deny_data_collection,
    )
    src = Source(Path(source))
    out = Path(output)
    try:
        total, cost = transcribe.estimate([src], settings, out, pages, force)
        if max_cost is not None and total and cost is None:
            raise TranscriberError("cannot enforce max_cost because a selected model's price is unknown")
        if max_cost is not None and cost is not None and cost > max_cost:
            raise TranscriberError(f"estimated cost {cost:.4f} USD exceeds max_cost {max_cost:.4f}")
        done, failed = transcribe.run([src], settings, out, dict(credentials or {}), pages, force)
    except SystemExit as e:
        raise TranscriberError(str(e) or "transcription failed") from e
    run_log = out / src.stem / "run.json"
    run_data = json.loads(run_log.read_text(encoding="utf-8")) if run_log.exists() else {}
    models = tuple(sorted({page["model"] for page in run_data.get("pages", {}).values()}))
    return TranscriptionResult(
        source=src.path, output=out, markdown=out / f"{src.stem}.md", run_log=run_log,
        pages_done=done, pages_failed=failed, models=models,
    )


def validate_file(
    source: str | Path,
    *,
    dir_a: str | Path = "output",
    dir_b: str | Path = "output-b",
    model_b: str | None = None,
    dir_c: str | Path | None = None,
    model_c: str = "235b",
    fallback_model: str | None = None,
    use_fallback: bool = True,
    offline: bool = False,
    workers: int = 4,
    threshold: float = 0.9,
    drop_chars: int | None = None,
    conflict_rate: float = 0.02,
    show_all: bool = False,
    credentials: Mapping[str, str] | None = None,
) -> ValidationResult:
    """Validate an existing transcription and return the generated review artifact paths."""
    from . import validate

    if not use_fallback and fallback_model:
        raise TranscriberError("fallback_model cannot be combined with use_fallback=False")
    src = Source(Path(source))
    a, b = Path(dir_a), Path(dir_b)
    c = Path(dir_c) if dir_c else None
    try:
        flagged = validate.validate(
            src, a, b, None if offline else dict(credentials or {}),
            model_b=resolve_model(model_b) if model_b else None, dir_c=c, model_c=resolve_model(model_c),
            fallback_model=resolve_model(fallback_model) if fallback_model else None,
            use_fallback=use_fallback, workers=workers, threshold=threshold, drop_chars=drop_chars,
            conflict_rate=conflict_rate, show_all=show_all,
        )
    except SystemExit as e:
        raise TranscriberError(str(e) or "validation failed") from e
    return ValidationResult(
        source=src.path, flagged_pages=flagged, merged=a / f"{src.stem}.merged.md",
        comparison=a / f"{src.stem}.comparison.html", review=a / f"{src.stem}.review.html",
    )
