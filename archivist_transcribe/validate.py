"""Check a transcription against a second, independent model and build the proofreading app.

Model A's run (normally `output/`) is the base text. B (default `output-b/`) is a second
transcription of the same pages by a different model with the same profile and prompt;
it is run automatically if missing. Optionally C (`-c output-c`) breaks ties on the pages
that A and B disagree about.

Why two models: one model can drop a whole line without a trace, while another may silently
"correct" the source. Using a faithful model as A and an independent one as B catches both.
Never ask the same model to check its own output; it tends to agree with itself.
"""
from __future__ import annotations

import html
import json
import statistics
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import review, transcribe
from .compare import PageCheck, check_page, content_len, is_blank, norms, tokenize
from .profiles import notation_for
from .sources import Source

console = Console()

DEFAULT_B = {"cjk": "qwen/qwen2.5-vl-72b-instruct"}  # tested on Chinese archives
DEFAULT_B_OTHER = "google/gemini-2.5-flash"  # an independent model family for everything else
DEFAULT_C = "qwen/qwen3-vl-235b-a22b-instruct"
DROP_CHARS = {"cjk": 8, "editorial": 25}  # a B-only span this long counts as a dropped line


def read_run(out: Path, src: Source) -> dict:
    p = out / src.stem / "run.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def read_page(out: Path, src: Source, i: int) -> str | None:
    f = transcribe.page_file(transcribe.pages_dir(out, src), i)
    return f.read_text(encoding="utf-8") if f.exists() else None


def ensure_run(src: Source, out: Path, settings: transcribe.Settings, pages: list[int], api_key: str) -> None:
    pdir = transcribe.pages_dir(out, src)
    missing = [i for i in pages if not transcribe.page_file(pdir, i).exists()]
    if not missing:
        return
    console.print(f"[cyan]{len(missing)} page(s) missing in {out}/; transcribing them with {settings.model}[/]")
    spec = ",".join(str(i + 1) for i in missing)
    transcribe.run([src], settings, out, api_key, pages_spec=spec)


def highlight(text: str, notation) -> str:
    """Escape merged text and colour the review markers."""
    out, pos = [], 0
    for m in notation.mark_re.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        cls = "drop" if m.group(1) else "conf" if m.group(3) is not None else "unc"
        out.append(f'<mark class="{cls}">{html.escape(m.group())}</mark>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def write_merged(src: Source, checks: list[PageCheck], labels: dict, notation, out: Path) -> None:
    legend = {
        "editorial": "[illegible] unreadable word · [word?] uncertain reading · {A|B} the models disagree "
                     "(A|B|C with a tiebreaker) · {A|} {|B} a short span only one model has · "
                     "{+B: …} a line only B saw (A probably dropped it; {+BC: …} when C saw it too)",
        "cjk": "□ 无法辨认的字（每字一个）；〔字〕模型没有把握的推测；〔A|B〕两个模型读法不同（A|B，三模型时为A|B|C）；"
               "【补B:…】仅模型B转录出的内容，A可能漏行；【补BC:…】B、C均转录出而A缺的内容",
    }[notation.name]
    who = " · ".join(f"{k}: {v}" for k, v in labels.items())
    parts = [f"# {src.stem}\n\n<!-- review markers: {legend} -->\n<!-- {who} -->\n"]
    for pc in sorted(checks, key=lambda c: c.page):
        parts.append(f"\n\n<!-- page {pc.page} -->\n\n{pc.merged}\n")
    out.write_text("".join(parts), encoding="utf-8")


def write_static(src: Source, checks: list[PageCheck], labels: dict, notation, out: Path, only_flagged: bool) -> None:
    rows = []
    for pc in checks:
        if only_flagged and pc.ok:
            continue
        flags = ", ".join(pc.flags) or "ok"
        rows.append(f"""
<section class="{'bad' if pc.flags else 'good'}">
<h2>{html.escape(src.label(pc.page - 1).capitalize())} <small>agreement {pc.ratio:.3f} · {html.escape(flags)}</small></h2>
<div class="cols"><div class="img"><img loading="lazy" src="{src.preview(pc.page - 1)}" alt=""></div>
<pre class="txt">{highlight(pc.merged, notation)}</pre></div>
</section>""")
    n_bad = sum(1 for c in checks if c.flags)
    mean = statistics.fmean(c.ratio for c in checks) if checks else 0
    who = " · ".join(f"{k}: {html.escape(v)}" for k, v in labels.items())
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(src.stem)} · comparison</title>
<style>
body{{font-family:system-ui,-apple-system,"Noto Sans","PingFang SC","Noto Sans CJK SC",sans-serif;margin:0;padding:16px;background:#f6f6f6;color:#222}}
h1{{font-size:18px}} h2{{font-size:15px;margin:0 0 8px}} h2 small{{font-weight:normal;color:#666;margin-left:8px}}
section{{background:#fff;border-radius:8px;padding:12px;margin-bottom:16px;border-left:6px solid #3a3}}
section.bad{{border-left-color:#d33}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.img img{{width:100%;height:auto;border:1px solid #ddd}}
.txt{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.8;margin:0;max-height:1000px;overflow:auto;background:#fafafa;padding:8px;border:1px solid #eee}}
mark.conf{{background:#ffe08a}} mark.drop{{background:#b6f0c2}} mark.unc{{background:#ffb3b3}}
@media (max-width:900px){{.cols{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>{html.escape(src.name)}</h1>
<p>{len(checks)} pages · mean agreement {mean:.3f} · {n_bad} flagged · worst first{' · flagged pages only' if only_flagged else ''}</p>
<p>{who}<br><mark class="conf">disagreement</mark> <mark class="drop">line only B saw</mark> <mark class="unc">model A was unsure</mark></p>
{''.join(rows)}
</body></html>"""
    out.write_text(page, encoding="utf-8")


def validate(src: Source, dir_a: Path, dir_b: Path, api_key: str | None, *, model_b: str | None = None,
             dir_c: Path | None = None, model_c: str = DEFAULT_C, c_all: bool = False, workers: int = 4,
             threshold: float = 0.9, drop_chars: int | None = None, conflict_rate: float = 0.02,
             show_all: bool = False) -> int:
    """Returns the number of flagged pages."""
    n = src.page_count()
    if dir_a == dir_b:
        raise SystemExit("A and B must be different folders")
    run_a = read_run(dir_a, src)
    if not transcribe.pages_dir(dir_a, src).exists() or not run_a:
        raise SystemExit(f"no transcription found in {dir_a}/{src.stem}/; run `archivist transcribe` first")
    profile = run_a.get("profile", "printed")
    notation = notation_for(profile, run_a.get("notation"))
    model_b = model_b or DEFAULT_B.get(notation.name, DEFAULT_B_OTHER)
    a_models = sorted({p["model"] for p in run_a["pages"].values()})
    if model_b in a_models:
        console.print(f"[yellow]warning: B uses the same model as A ({model_b}); a model tends to agree with itself.[/]")
    base = dict(profile=profile, language=run_a.get("language"), layout=run_a.get("layout", "paragraphs"),
                diplomatic=run_a.get("diplomatic", False), dpi=150, workers=workers, retries=5, timeout=180,
                deny_data_collection=False, notation=notation.name)
    first_page = next(iter(run_a["pages"].values()))
    base["dpi"] = first_page.get("dpi", 150)
    done_a = [i for i in range(n) if read_page(dir_a, src, i) is not None]
    if api_key:
        ensure_run(src, dir_b, transcribe.Settings(model=model_b, **base), done_a, api_key)
    drop = drop_chars or DROP_CHARS[notation.name]

    texts_a = [read_page(dir_a, src, i) for i in range(n)]
    texts_b = [read_page(dir_b, src, i) for i in range(n)]
    lengths = [content_len(norms(tokenize(t, notation))) for t in texts_a + texts_b if t and not is_blank(t)]
    median_len = statistics.median(lengths) if lengths else 0
    kw = dict(threshold=threshold, drop_chars=drop, conflict_rate=conflict_rate, looks_looped=transcribe.looks_looped)
    pages = [i for i in range(n) if texts_a[i] is not None or texts_b[i] is not None]
    checks = [check_page(i, texts_a[i], texts_b[i], notation, median_len, **kw) for i in pages]
    labels = {"A": f"{dir_a} ({', '.join(a_models)})", "B": f"{dir_b} ({model_b})"}

    if dir_c:
        if dir_c in (dir_a, dir_b):
            raise SystemExit("C must be a third folder")
        want = [c.page - 1 for c in checks] if c_all else [c.page - 1 for c in checks if not c.ok]
        if api_key:
            ensure_run(src, dir_c, transcribe.Settings(model=model_c, **base), want, api_key)
        by_page = {c.page - 1: k for k, c in enumerate(checks)}
        n_three = 0
        for i in want:
            c_text = read_page(dir_c, src, i)
            if c_text is not None:
                checks[by_page[i]] = check_page(i, texts_a[i], texts_b[i], notation, median_len, c=c_text, **kw)
                n_three += 1
        labels["C"] = f"{dir_c} ({model_c})"
        console.print(f"[cyan]three-way merge on {n_three} page(s) with C = {dir_c} ({model_c})[/]")

    checks.sort(key=lambda c: (c.ok, -c.severity, c.ratio))
    table = Table(title=f"{src.name}: A vs B" + (" + C" if dir_c else ""))
    for col in ("page", "agree", "dropped?", "disagree", "voted", "uncertain", "flags"):
        table.add_column(col, justify="left" if col == "flags" else "right")
    for c in checks:
        if not c.ok:
            style = "red" if c.dropped or c.a is None or c.b is None or c.ratio < threshold - 0.1 else "yellow"
            table.add_row(str(c.page), f"{c.ratio:.3f}", str(c.dropped), str(c.conflicts),
                          str(c.voted) if c.c else "-", str(c.uncertain), ", ".join(c.flags), style=style)
    if table.row_count:
        console.print(table)
    n_bad = sum(1 for c in checks if not c.ok)
    mean = statistics.fmean(c.ratio for c in checks) if checks else 0
    console.print(f"[{'green' if not n_bad else 'yellow'}]{len(checks)} pages · mean agreement {mean:.3f} · "
                  f"{sum(c.dropped for c in checks)} possible dropped lines · {sum(c.conflicts for c in checks)} disagreements · "
                  f"{sum(c.uncertain for c in checks)} uncertain marks"
                  + (f" · {sum(c.voted for c in checks)} settled by vote" if dir_c else "") + f" · {n_bad} pages flagged[/]")

    merged = dir_a / f"{src.stem}.merged.md"
    static = dir_a / f"{src.stem}.comparison.html"
    app = dir_a / f"{src.stem}.review.html"
    write_merged(src, checks, labels, notation, merged)
    write_static(src, checks, labels, notation, static, only_flagged=not show_all)
    review.write_review(src, checks, labels, notation, app)
    console.print(f"proofread here: {app}\nmerged draft:   {merged}\ncomparison:     {static}")
    return n_bad
