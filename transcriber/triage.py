"""`transcriber inspect`: look at a source before spending anything on it.

Reports page count and size, whether a PDF already has a text layer (and whether it looks
usable), a cost estimate for the default model, and can save a few page images so a person
or an agent can look at the script, hand and condition before choosing a profile.
"""
from __future__ import annotations

import re
from pathlib import Path

from rich.console import Console

from .openrouter import DEFAULT_MODEL, fetch_pricing, money
from .sources import Source
from .transcribe import OUTPUT_TOKENS_PER_PAGE, PROMPT_TOKENS, parse_pages

console = Console()
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
WORDLIKE = re.compile(r"^[^\W\d_]{2,}$")


def text_quality(text: str) -> tuple[int, float, float]:
    """(characters, share of CJK characters, share of word-like tokens among non-CJK tokens)."""
    chars = len(re.sub(r"\s", "", text))
    cjk = len(CJK.findall(text))
    toks = [t for t in re.split(r"\s+", text) if t and not CJK.search(t)]
    good = sum(1 for t in toks if WORDLIKE.match(t.strip(".,;:!?\"'()[]")))
    return chars, (cjk / chars if chars else 0.0), (good / len(toks) if toks else 0.0)


def inspect(src: Source, sample: str | None, out_dir: Path | None, dpi: int = 110) -> None:
    n = src.page_count()
    console.print(f"[bold]{src.name}[/]: {'PDF' if src.kind == 'pdf' else 'images'}, {n} page(s)")
    probe = sorted(set([0, n // 2, n - 1]) | set(range(min(n, 3))))
    if src.kind == "pdf":
        stats = [text_quality(src.text_layer(i)) for i in probe]
        with_text = [s for s in stats if s[0] > 50]
        if not with_text:
            console.print("text layer: none (image-only scan) -> needs transcription")
        else:
            chars = sum(s[0] for s in with_text) / len(with_text)
            cjk = sum(s[1] for s in with_text) / len(with_text)
            wordlike = sum(s[2] for s in with_text) / len(with_text)
            verdict = ("looks clean; consider using it directly (`transcriber inspect --show-text`) "
                       "and transcribing only bad pages" if (cjk > 0.5 or wordlike > 0.8)
                       else "looks garbled (typical of old OCR) -> transcription will help")
            console.print(f"text layer: on {len(with_text)}/{len(probe)} sampled pages, ~{chars:.0f} chars/page, "
                          f"{cjk:.0%} CJK, {wordlike:.0%} word-like tokens: {verdict}")
            if cjk > 0.3:
                console.print("suggested profile: [bold]cjk[/]")
    tokens = sum(src.image_tokens(i, 150) for i in probe) / len(probe)
    price = fetch_pricing(DEFAULT_MODEL)
    if price:
        per_page = (tokens + PROMPT_TOKENS) * price[0] + OUTPUT_TOKENS_PER_PAGE * price[1]
        console.print(f"estimate with {DEFAULT_MODEL}: about {money(per_page)}/page, {money(per_page * n)} for all {n} "
                      f"pages (validation with a second model typically costs 2 to 10 times as much again)")
    if sample:
        out_dir = out_dir or Path("inspect") / src.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in parse_pages(sample, n):
            pix = src._pixmap(i, dpi, 1600)
            f = out_dir / f"page_{i + 1:04d}.jpg"
            f.write_bytes(pix.tobytes("jpeg", jpg_quality=80))
            console.print(f"saved {f}")


def show_text(src: Source, pages: str | None) -> None:
    n = src.page_count()
    for i in parse_pages(pages or "1-3", n):
        console.rule(src.label(i))
        console.print(src.text_layer(i).strip() or "(no text layer)", markup=False, highlight=False)
