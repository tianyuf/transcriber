"""Transcribe a source page by page.

Each page image is sent to a vision model with the profile's prompt and retried with backoff.
Finished pages are written to <out>/<stem>/pages/page_NNNN.md the moment they complete, so an
interrupted run resumes where it stopped, and <out>/<stem>.md is rebuilt after every page.
<out>/<stem>/run.json records, for every page, the model, prompt fingerprint and time as provenance.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from . import __version__
from .openrouter import SPEND, APIError, FatalAPIError, money
from .profiles import BLANK, build_prompt, notation_for, prompt_id
from .providers import call_vision, credentials_for, fetch_pricing, supports_temperature
from .sources import Source

console = Console()
PROMPT_TOKENS = 600
OUTPUT_TOKENS_PER_PAGE = 700  # a typical page of typescript; refined from finished pages
REPEAT_RE = re.compile(r"(.{8,80}?)(?:\s*\1){2,}", re.S)  # 8+ chars repeated 3+ times in a row
RULE_CHARS = set("-=_.·•…:|*~—–")  # typed rules, dot leaders, Markdown table rules


def looks_looped(text: str) -> bool:
    """A model stuck repeating itself. Rows of one character (□□□, ......) and table rules are
    legitimately on the page and do not count; a model stuck on them hits the length limit,
    which is caught separately."""
    for m in REPEAT_RE.finditer(text):
        chars = set(m.group(1)) - set(" \t\n")
        if len(chars) > 1 and not chars <= RULE_CHARS:
            return True
    return False


def clean_markdown(text: str) -> str:
    text = text.strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, flags=re.S)  # a wrapping code fence
    return m.group(1).strip() if m else text


@dataclass
class Settings:
    model: str
    profile: str
    language: str | None
    layout: str
    diplomatic: bool
    dpi: int
    workers: int
    retries: int
    timeout: float
    deny_data_collection: bool
    notation: str | None = None  # None: the profile's default
    fallback_model: str | None = None

    @property
    def prompt(self) -> str:
        return build_prompt(self.profile, self.language, self.layout, self.diplomatic, self.notation)

    @property
    def notation_name(self) -> str:
        return notation_for(self.profile, self.notation).name

    @property
    def models(self) -> list[str]:
        return [self.model] + ([self.fallback_model] if self.fallback_model and self.fallback_model != self.model else [])


@dataclass
class PageResult:
    index: int
    text: str
    model: str
    attempts: int
    seconds: float
    truncated: bool = False
    used_fallback: bool = False


def transcribe_page(src: Source, index: int, s: Settings, credentials: dict[str, str], log) -> PageResult:
    """Transcribe one page, trying the fallback only after the primary cannot produce usable text."""
    started = time.monotonic()
    image, mime = src.render(index, s.dpi)
    prompt = s.prompt
    truncated_mark = notation_for(s.profile, s.notation).truncated
    last_err: BaseException | None = None
    best_partial: tuple[str, str] | None = None
    attempts = 0
    models = s.models
    with httpx.Client() as client:
        for model_index, model in enumerate(models):
            temperature = 0.0
            for attempt in range(1, s.retries + 1):
                attempts += 1
                try:
                    raw, finish = call_vision(client, credentials, model, prompt, image, mime, s.timeout, temperature,
                                              s.deny_data_collection)
                    text = clean_markdown(raw)
                    if not text:
                        raise APIError("empty response from model")
                    if finish in ("length", "max_tokens"):
                        raise APIError("response cut off at the length limit", partial=text, degenerate=True)
                    if finish in ("content_filter", "refusal"):
                        raise APIError("model refused the page", degenerate=True)
                    if looks_looped(text):
                        raise APIError("repetition loop in response", degenerate=True)
                    return PageResult(index=index, text=text, model=model, attempts=attempts,
                                      seconds=time.monotonic() - started, used_fallback=model != s.model)
                except FatalAPIError as e:
                    last_err = e
                    if model_index == len(models) - 1:
                        if best_partial:
                            log(f"[yellow]{src.label(index)}[/] {model} failed: {e!s:.160}; "
                                f"preserving partial output from {best_partial[0]}")
                            break
                        raise
                    break
                except (APIError, httpx.HTTPError) as e:
                    last_err = e
                    note = ""
                    if isinstance(e, APIError):
                        if e.partial and len(e.partial) > len(best_partial[1] if best_partial else ""):
                            best_partial = (model, e.partial)
                        if e.degenerate:  # the same request at temperature 0 repeats the same loop
                            if supports_temperature(model):
                                temperature = min(0.8, temperature + 0.3)
                                note = f", temperature -> {temperature:.1f}"
                            else:
                                log(f"[yellow]{src.label(index)}[/] {model} failed: {e!s:.120}; "
                                    "sampling is fixed, skipping identical retries")
                                break
                    if attempt == s.retries:
                        break
                    delay = min(30.0, 2 ** (attempt - 1) + random.uniform(0, 1))
                    log(f"[yellow]{src.label(index)}[/] {model} attempt {attempt}/{s.retries} failed: "
                        f"{e!s:.120} - retrying in {delay:.1f}s{note}")
                    time.sleep(delay)
            if model_index < len(models) - 1:
                log(f"[yellow]{src.label(index)}[/] {model} failed; falling back to {models[model_index + 1]}: "
                    f"{last_err!s:.160}")
    if best_partial:  # better a visibly incomplete page than none; validate flags the marker
        model, text = best_partial
        return PageResult(index=index, text=text.rstrip() + "\n\n" + truncated_mark, model=model, attempts=attempts,
                          seconds=time.monotonic() - started, truncated=True, used_fallback=model != s.model)
    raise APIError(f"{src.label(index)} failed with {', '.join(models)} after {attempts} attempts: {last_err}")


def parse_pages(spec: str | None, n_pages: int) -> list[int]:
    """'1,3,5-9' (1-based, inclusive) -> sorted 0-based indexes; None means every page."""
    if not spec:
        return list(range(n_pages))
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if part:
            lo, _, hi = part.partition("-")
            chosen.update(i - 1 for i in range(int(lo), int(hi or lo) + 1) if 1 <= i <= n_pages)
    return sorted(chosen)


def pages_dir(out: Path, src: Source) -> Path:
    return out / src.stem / "pages"


def page_file(pdir: Path, index: int) -> Path:
    return pdir / f"page_{index + 1:04d}.md"


class RunLog:
    """<out>/<stem>/run.json: settings and per-page provenance, updated as pages finish."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pages": {}}

    def record(self, src: Source, s: Settings, r: PageResult) -> None:
        with self.lock:
            self.data.update({
                "source": src.name, "tool": f"transcriber {__version__}",
                "profile": s.profile, "notation": s.notation_name, "language": s.language, "layout": s.layout,
                "diplomatic": s.diplomatic, "fallback_model": s.fallback_model,
            })
            self.data["pages"][str(r.index + 1)] = {
                "model": r.model, "prompt_id": prompt_id(s.prompt), "dpi": s.dpi,
                "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "attempts": r.attempts, "truncated": r.truncated, "used_fallback": r.used_fallback,
                **({"fallback_from": s.model} if r.used_fallback else {}),
                **({"image": src.files[r.index].name} if src.kind == "images" else {}),
            }
            self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")


def front_matter(src: Source, run: dict, n_pages: int, missing: int) -> str:
    pages = run.get("pages", {})
    models = sorted({p["model"] for p in pages.values()})
    prompts = sorted({p["prompt_id"] for p in pages.values()})
    dates = sorted(p["transcribed_at"][:10] for p in pages.values())
    lines = [
        "---",
        f"source: {json.dumps(src.name, ensure_ascii=False)}",
        f"pages: {n_pages}",
        f"pages_transcribed: {n_pages - missing}",
        f"method: machine transcription from page images ({run.get('tool', 'transcriber')})",
        f"models: {json.dumps(models)}",
        f"profile: {run.get('profile', '')}",
        f"notation: {run.get('notation', '')}",
    ]
    if run.get("language"):
        lines.append(f"language: {json.dumps(run['language'], ensure_ascii=False)}")
    lines += [
        f"prompt_id: {json.dumps(prompts)}",
        f"transcribed: {dates[0] + (' to ' + dates[-1] if dates and dates[-1] != dates[0] else '') if dates else ''}",
        "status: unreviewed machine transcription",
        "---",
    ]
    return "\n".join(lines) + "\n"


def stitch(src: Source, out: Path, n_pages: int) -> int:
    pdir = pages_dir(out, src)
    run_path = out / src.stem / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.exists() else {"pages": {}}
    parts, missing = [], 0
    for i in range(n_pages):
        f = page_file(pdir, i)
        if f.exists():
            body = f.read_text(encoding="utf-8").strip()
        else:
            body = "> [!WARNING] page not transcribed yet"
            missing += 1
        marker = f"page {i + 1}" + (f" · {src.files[i].name}" if src.kind == "images" else "")
        parts.append(f"\n\n<!-- {marker} -->\n\n{body}\n")
    text = front_matter(src, run, n_pages, missing) + f"\n# {src.stem}\n" + "".join(parts)
    (out / f"{src.stem}.md").write_text(text, encoding="utf-8")
    return missing


def estimate(sources: list[Source], s: Settings, out: Path, pages_spec: str | None, force: bool) -> tuple[int, float | None]:
    """Print the pages still to do and their likely cost, including worst-case fallback use.

    Returns (pages, dollars or None). The dollar figure excludes additional retry charges.
    """
    tin = tout = total = 0
    for src in sources:
        n = src.page_count()
        pdir = pages_dir(out, src)
        todo = [i for i in parse_pages(pages_spec, n) if force or not page_file(pdir, i).exists()]
        done = [len(f.read_text(encoding="utf-8")) for f in pdir.glob("page_*.md")] if pdir.exists() else []
        per_page = round(sum(done) / len(done) / 2) if len(done) >= 3 else OUTPUT_TOKENS_PER_PAGE
        tin += sum(src.image_tokens(i, s.dpi) for i in todo) + PROMPT_TOKENS * len(todo)
        tout += per_page * len(todo)
        total += len(todo)

    def show(model: str, fallback: bool = False) -> float | None:
        line = (f"fallback, if every page needs it: {total} page(s) with {model}" if fallback
                else f"estimate: {total} page(s) to transcribe with {model}")
        line += f", ~{tin:,} input + ~{tout:,} output tokens"
        price = fetch_pricing(model)
        if price is None:
            console.print(f"{line}; price unknown for this model")
            return None
        cost = tin * price[0] + tout * price[1]
        console.print(f"{line} -> about [bold]{money(cost)}[/] "
                      f"({money(cost / total if total else 0)}/page; retries cost extra)")
        return cost

    cost = show(s.model)
    if s.fallback_model and s.fallback_model != s.model:
        fallback_cost = show(s.fallback_model, fallback=True)
        cost = cost + fallback_cost if cost is not None and fallback_cost is not None else None
    return total, cost


def run(sources: list[Source], s: Settings, out: Path, credentials: dict[str, str] | None = None,
        pages_spec: str | None = None, force: bool = False) -> tuple[int, int]:
    """Transcribe; returns (pages done, pages failed) across all sources."""
    credentials = credentials_for(s.models, credentials, s.deny_data_collection)
    total_ok = total_failed = 0
    console.print(f"model: [bold]{s.model}[/]  profile: {s.profile}  workers: {s.workers}  dpi: {s.dpi}")
    if s.fallback_model and s.fallback_model != s.model:
        console.print(f"fallback: [bold]{s.fallback_model}[/] (per failed page; up to {s.retries} attempts per model)")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                  TimeElapsedColumn(), TimeRemainingColumn(), console=console) as progress:
        log = progress.console.log
        for src in sources:
            n = src.page_count()
            pdir = pages_dir(out, src)
            pdir.mkdir(parents=True, exist_ok=True)
            runlog = RunLog(out / src.stem / "run.json")
            wanted = parse_pages(pages_spec, n)
            todo = [i for i in wanted if force or not page_file(pdir, i).exists()]
            task = progress.add_task(f"[bold]{src.name}", total=len(wanted), completed=len(wanted) - len(todo))
            log(f"[cyan]{src.name}[/]: {n} pages, {len(wanted)} selected, {len(wanted) - len(todo)} already done")
            failed = 0
            with ThreadPoolExecutor(max_workers=s.workers) as pool:
                futures = {pool.submit(transcribe_page, src, i, s, credentials, log): i for i in todo}
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        r = fut.result()
                    except SystemExit:
                        pool.shutdown(cancel_futures=True)
                        raise
                    except Exception as e:  # noqa: BLE001 - keep going; a re-run retries only failures
                        failed += 1
                        log(f"[red]{src.label(i)} FAILED[/]: {e!s:.200}")
                    else:
                        page_file(pdir, i).write_text(r.text + "\n", encoding="utf-8")
                        runlog.record(src, s, r)
                        tries = "" if r.attempts == 1 else f", {r.attempts} attempts"
                        state = "[yellow]saved TRUNCATED[/]" if r.truncated else "[green]ok[/]"
                        fallback = f", fallback model {r.model}" if r.used_fallback else ""
                        log(f"{src.label(i)} {state} {len(r.text)} chars in {r.seconds:.1f}s{tries}{fallback}")
                    stitch(src, out, n)
                    progress.advance(task)
            missing = stitch(src, out, n)
            log(f"wrote {out / (src.stem + '.md')} ({n - missing}/{n} pages)")
            not_done = sum(1 for i in wanted if not page_file(pdir, i).exists())
            total_ok += len(wanted) - not_done
            total_failed += not_done
    if SPEND.calls:
        message = (f"API usage: {SPEND.calls} call(s): {SPEND.input_tokens:,} input + "
                   f"{SPEND.output_tokens:,} output tokens")
        if SPEND.cost_calls:
            message += f"; OpenRouter-reported cost [bold]{money(SPEND.cost)}[/] on {SPEND.cost_calls} call(s)"
        if SPEND.cost_calls < SPEND.calls:
            message += "; calls without an OpenRouter cost report are not included"
        console.print(message)
    return total_ok, total_failed


def is_blank(text: str) -> bool:
    return BLANK in text or "(空白页)" in text
