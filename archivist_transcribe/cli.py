"""archivist-transcribe: transcribe scanned archival documents and check the result.

  archivist-transcribe auth login                 sign in to OpenRouter in the browser (once per computer)
  archivist-transcribe inspect FILE --sample 1-3  look before you spend: text layer, cost, page images
  archivist-transcribe transcribe FILE            transcribe every page (resumable)
  archivist-transcribe validate FILE              check against a second model, build the proofreading app
  archivist-transcribe models                     recommended models and what they cost

FILE can be a PDF, an image, or a folder of images (one photo per page).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, auth
from .openrouter import DEFAULT_MODEL, MODEL_ALIASES, MODEL_NOTES, fetch_pricing, resolve_model
from .profiles import PROFILES
from .sources import Source


def cmd_auth(a) -> int:
    if a.action == "login":
        auth.login(headless=a.headless, limit=a.limit, expiration=a.expiration, no_browser=a.no_browser)
        return 0
    if a.action == "status":
        return auth.status()
    auth.logout()
    return 0


def cmd_models(a) -> int:
    print(f"default: {DEFAULT_MODEL}\n")
    for alias, model in MODEL_ALIASES.items():
        p = fetch_pricing(model)
        price = f"${p[0] * 1e6:.2f} in / ${p[1] * 1e6:.2f} out per M tokens" if p else "price unavailable"
        print(f"{alias:<7} {model:<36} {price}\n        {MODEL_NOTES.get(alias, '')}")
    print("\nAny other OpenRouter model id with image input works too: https://openrouter.ai/models?input_modalities=image")
    return 0


def cmd_inspect(a) -> int:
    from . import triage
    for p in a.sources:
        src = Source(Path(p))
        if a.show_text:
            triage.show_text(src, a.pages)
        else:
            triage.inspect(src, a.sample, Path(a.out) if a.out else None)
    return 0


def settings_from(a):
    from .transcribe import Settings
    return Settings(model=resolve_model(a.model), profile=a.profile, language=a.language, layout=a.layout,
                    diplomatic=a.diplomatic, dpi=a.dpi, workers=a.workers, retries=a.retries, timeout=a.timeout,
                    deny_data_collection=a.deny_data_collection, notation=a.notation)


def cmd_transcribe(a) -> int:
    from . import transcribe
    sources = [Source(Path(p)) for p in a.sources]
    s = settings_from(a)
    out = Path(a.out)
    for src in sources:  # resuming with different settings would mix two kinds of pages
        run = out / src.stem / "run.json"
        if run.exists() and not a.force:
            prev = json.loads(run.read_text(encoding="utf-8"))
            if (prev.get("profile"), prev.get("notation"), prev.get("language"), prev.get("layout")) != \
                    (s.profile, s.notation_name, s.language, s.layout):
                print(f"warning: {run} was made with profile={prev.get('profile')}, language={prev.get('language')}, "
                      f"layout={prev.get('layout')}; this run uses profile={s.profile}, language={s.language}, "
                      f"layout={s.layout}. Use --force to redo every page, or a different -o folder.", file=sys.stderr)
    _, cost = transcribe.estimate(sources, s, out, a.pages, a.force)
    if a.estimate:
        return 0
    if a.max_cost is not None and cost is not None and cost > a.max_cost:
        print(f"estimated cost {cost:.2f} USD is above --max-cost {a.max_cost:.2f}; nothing was sent.", file=sys.stderr)
        return 3
    key = auth.require_key()
    ok, failed = transcribe.run(sources, s, out, key, a.pages, a.force)
    if failed:
        print(f"done with {failed} failed page(s); run the same command again to retry only those", file=sys.stderr)
        return 1
    print(f"done: {ok} pages")
    return 0


def cmd_validate(a) -> int:
    from . import validate
    src = Source(Path(a.source))
    key = None if a.offline else auth.require_key()
    n_bad = validate.validate(
        src, Path(a.dir_a), Path(a.dir_b), key, model_b=resolve_model(a.model_b) if a.model_b else None,
        dir_c=Path(a.dir_c) if a.dir_c else None, model_c=resolve_model(a.model_c), c_all=a.c_all,
        workers=a.workers, threshold=a.threshold, drop_chars=a.drop_chars, conflict_rate=a.conflict_rate,
        show_all=a.all)
    return 1 if n_bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archivist-transcribe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"archivist-transcribe {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("auth", help="sign in to OpenRouter, check or remove the stored key")
    pa.add_argument("action", choices=["login", "status", "logout"])
    pa.add_argument("--headless", action="store_true", help="no local callback: paste the code shown by OpenRouter")
    pa.add_argument("--no-browser", action="store_true", help="print the sign-in address instead of opening a browser")
    pa.add_argument("--limit", help="suggest a spending limit in USD for the new key, e.g. 10")
    pa.add_argument("--expiration", choices=["1-hour", "1-day", "7-days", "30-days", "90-days", "180-days", "1-year", "never"],
                    help="suggest an expiry for the new key")
    pa.set_defaults(func=cmd_auth)

    pm = sub.add_parser("models", help="recommended models and prices")
    pm.set_defaults(func=cmd_models)

    pi = sub.add_parser("inspect", help="page count, text layer, cost estimate, sample page images")
    pi.add_argument("sources", nargs="+")
    pi.add_argument("--sample", help="save these pages as JPEGs to look at, e.g. 1,5,10-12")
    pi.add_argument("--out", help="folder for the sample images (default: inspect/<name>/)")
    pi.add_argument("--show-text", action="store_true", help="print the PDF's existing text layer")
    pi.add_argument("--pages", help="pages for --show-text (default 1-3)")
    pi.set_defaults(func=cmd_inspect)

    pt = sub.add_parser("transcribe", help="transcribe page by page (resumable)")
    pt.add_argument("sources", nargs="+", help="PDF(s), image(s) or folder(s) of images")
    pt.add_argument("-o", "--out", default="output", help="output folder (default: output)")
    pt.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"model id or alias (default: {DEFAULT_MODEL}; see `archivist-transcribe models`)")
    pt.add_argument("--profile", default="printed", choices=list(PROFILES), help="kind of document (default: printed)")
    pt.add_argument("--language", help="expected language(s) and script, e.g. 'German (Fraktur)', 'Ottoman Turkish in Arabic script'")
    pt.add_argument("--notation", choices=["editorial", "cjk"],
                    help="how doubt is marked: editorial [illegible] [word?] (default) or cjk □ 〔字〕 "
                         "(default for --profile cjk; use with --profile handwritten for Chinese manuscripts)")
    pt.add_argument("--layout", default="paragraphs", choices=["paragraphs", "lines"],
                    help="join wrapped lines into paragraphs (default) or keep every line break")
    pt.add_argument("--diplomatic", action="store_true", help="keep long s, ligatures and abbreviation marks as written")
    pt.add_argument("-w", "--workers", type=int, default=4, help="pages in parallel (default: 4)")
    pt.add_argument("--dpi", type=int, default=150, help="PDF render resolution (default 150; 200 for small or faint print)")
    pt.add_argument("--retries", type=int, default=5)
    pt.add_argument("--timeout", type=float, default=180.0, help="seconds per request (default 180)")
    pt.add_argument("--pages", help="only these pages, e.g. 1,3,5-9")
    pt.add_argument("--force", action="store_true", help="redo pages that already have output")
    pt.add_argument("--estimate", action="store_true", help="print the cost estimate and stop")
    pt.add_argument("--max-cost", type=float, help="stop before sending anything if the estimate exceeds this many USD")
    pt.add_argument("--deny-data-collection", action="store_true",
                    help="only use providers that do not store or train on the pages")
    pt.set_defaults(func=cmd_transcribe)

    pv = sub.add_parser("validate", help="check against a second model and build the proofreading app")
    pv.add_argument("source")
    pv.add_argument("-a", "--dir-a", default="output", help="the run to check, used as base text (default: output)")
    pv.add_argument("-b", "--dir-b", default="output-b", help="folder for the second model's run (default: output-b)")
    pv.add_argument("--model-b", help="second model (default: gemini for most profiles, 72b for cjk)")
    pv.add_argument("-c", "--dir-c", help="folder for a third model used as tiebreaker on flagged pages, e.g. output-c")
    pv.add_argument("--model-c", default="235b", help="tiebreaker model (default: 235b)")
    pv.add_argument("--c-all", action="store_true", help="run the tiebreaker on every page, not only flagged ones")
    pv.add_argument("-w", "--workers", type=int, default=4)
    pv.add_argument("--threshold", type=float, default=0.9, help="flag pages with agreement below this (default 0.9)")
    pv.add_argument("--drop-chars", type=int, help="a B-only span this many letters long counts as a dropped line "
                                                    "(default 25, or 8 characters for cjk)")
    pv.add_argument("--conflict-rate", type=float, default=0.02, help="flag pages where disagreements exceed this share of words")
    pv.add_argument("--all", action="store_true", help="include unflagged pages in the static comparison page")
    pv.add_argument("--offline", action="store_true", help="compare existing runs only; call no model")
    pv.set_defaults(func=cmd_validate)
    return p


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
