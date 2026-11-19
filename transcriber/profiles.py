"""Transcription profiles (what kind of document) and notations (how doubt is marked).

A profile adds rules to a shared, language-neutral core prompt. A notation is the set of
markers the model and the validator write for things a reader must check. Two notations:

  editorial (default)  [illegible]  [word?]  {A|B}  {+B: missed line}   familiar to editors of
                                                                         European-language sources
  cjk                  □  〔字〕  〔A|B〕  【补B:漏行】                    one box per illegible character

Whichever is used, every marker means "check this against the scan", and the review app
turns each one into a decision.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Notation:
    name: str
    illegible: str  # one per unreadable word (editorial) or character (cjk)
    unc_open: str
    unc_close: str
    conf_open: str
    conf_close: str
    drop_open: str  # followed by B or BC, then drop_sep
    drop_sep: str
    drop_close: str
    truncated: str
    # one regex for the review app; groups: 1 drop who, 2 drop text, 3 conflict inner, 4 uncertain text;
    # a match with none of them is an illegible marker
    mark_re: re.Pattern
    uncertain_re: re.Pattern

    def uncertain(self, s: str) -> str:
        return f"{self.unc_open}{s}{self.unc_close}"

    def conflict(self, *opts: str) -> str:
        return self.conf_open + "|".join(opts) + self.conf_close

    def drop(self, who: str, text: str) -> str:
        return f"{self.drop_open}{who}{self.drop_sep}{text}{self.drop_close}"

    def count_uncertain(self, text: str) -> int:
        return text.count(self.illegible) + len(self.uncertain_re.findall(text))

    def js(self) -> dict:
        """What the review app needs to write unresolved markers back out."""
        return {"ill": self.illegible, "unc": [self.unc_open, self.unc_close],
                "conf": [self.conf_open, self.conf_close],
                "drop": [self.drop_open, self.drop_sep, self.drop_close]}


EDITORIAL = Notation(
    name="editorial",
    illegible="[illegible]",
    unc_open="[", unc_close="?]",
    conf_open="{", conf_close="}",
    drop_open="{+", drop_sep=": ", drop_close="}",
    truncated="[transcription cut off here; the rest of the page is missing]",
    mark_re=re.compile(r"\{\+(BC?): ([^{}]*)\}|\{([^{}+^][^{}]*\|[^{}]*|\|[^{}]*)\}|\[([^\[\]]{1,60})\?\]|\[illegible\]"),
    uncertain_re=re.compile(r"\[[^\[\]]{1,60}\?\]"),
)

CJK = Notation(
    name="cjk",
    illegible="□",
    unc_open="〔", unc_close="〕",
    conf_open="〔", conf_close="〕",
    drop_open="【补", drop_sep=":", drop_close="】",
    truncated="〔转录在此截断，后文缺失〕",
    mark_re=re.compile(r"【补(BC?):([^【】]*)】|〔([^〔〕]*\|[^〔〕]*)〕|〔([^〔〕|，。]{1,20})〕|□"),
    uncertain_re=re.compile(r"〔[^〔〕|，。]{1,20}〕"),  # not the truncation notice, which has a comma
)

NOTATIONS = {"editorial": EDITORIAL, "cjk": CJK}

BLANK = "(blank page)"


CORE = """You are a meticulous archival transcriber. Transcribe all the text on this page image into Markdown, exactly as it appears.

Rules:
- Language: transcribe every passage in the language and script it is written in. Never translate, transliterate or romanize.{language}
- Faithful to the original: write what is there. Do not summarize, complete, correct misspellings or misprints, expand abbreviations, or modernize spelling, grammar or word forms. Keep a typo as typed and an old spelling as spelled.
{layout}
- Punctuation and capitalization exactly as in the source.
{letterforms}
{notation}
- Headers, footers, page numbers, stamps, file numbers and marginal notes: transcribe them as they are, after the main text, each on its own line.
- Images, seals and drawings: on a line of its own, write ![image]() followed by a short description. Tables: use a Markdown table.
- Output only the transcription: no explanation, preface, commentary or code fences.
- If the page has no text at all, output exactly: (blank page)"""

LAYOUT = {
    "paragraphs": "- Paragraphs, not lines: where a line ends only because it reached the edge of the page or column, continue the same paragraph on one line (rejoin a word hyphenated across a line end; join Chinese, Japanese or Korean text with no space). Start a new line only where the source really does: a new paragraph, a heading, a list item, a line of verse, an address or letterhead line, a salutation, a closing or signature, a date line, a table row. Leave a blank line between paragraphs. Leave out no text.",
    "lines": "- Lines as in the source: keep every line break exactly where it is on the page, one output line per line of text, and keep end-of-line hyphens. Leave out no text.",
}

LETTERFORMS = {
    False: "- Letterforms: write long s (ſ) as s and split typographic ligatures into their letters, but keep everything else (spelling, accents, umlauts, old characters such as þ or ȝ) as it is.",
    True: "- Letterforms (diplomatic): keep long s (ſ), ligatures, superscript letters and abbreviation marks exactly as they appear, using the closest Unicode characters.",
}

NOTATION_RULES = {
    "editorial": "- Doubt: for each word you cannot read at all, write [illegible]. For a word you can make out but are unsure of, write your best guess followed by a question mark in square brackets, e.g. [Lowell?]. Mark rather than guess.",
    "cjk": "- Doubt: write □ for each character you cannot read at all (one □ per character). For a character or word you can make out but are unsure of, write your best guess inside 〔〕, e.g. 〔总〕 or 〔Shanghai〕. Mark rather than guess.",
}

PROFILE_RULES = {
    "printed": "",
    "handwritten": """
Handwriting:
- Read the hand carefully; letters, diaries and notebooks often use personal abbreviations: keep them as written, do not expand them.
- Struck-through text: transcribe it inside ~~ ~~ where it is still legible, e.g. ~~Tuesday~~; write ~~{illegible}~~ where it is not.
- Words added above or below the line or in the margin: insert them where they belong, inside {^ }, e.g. {^not}.
- Underlined words: keep them plain unless the underlining is clearly emphasis, then use _italics_.
- Different hands (a later annotation, a stamp, a clerk's note): put them on their own line, introduced with [other hand:].""",
    "cjk": """
Chinese, Japanese and Korean:
- Keep simplified and traditional characters, variant characters (异体字) and old forms exactly as written: if it prints 象, write 象, not 像; if a typist wrote 百 for 面, write 百.
- Use full-width punctuation (，。：；？！“”《》) in CJK text and ordinary punctuation in Western-language text.
- Read vertical text top to bottom, columns right to left, and write it out as horizontal lines.""",
}

PROFILES = {
    "printed": "printed or typed material in any language: books, newspapers, typescripts, carbon copies, forms",
    "handwritten": "manuscripts in any language: letters, diaries, notebooks, drafts, annotations",
    "cjk": "Chinese, Japanese or Korean material, printed, typed or mimeographed (油印本), with CJK markers □ 〔〕 【补】",
}


def notation_for(profile: str, notation: str | None = None) -> Notation:
    """The notation asked for, else the profile's default (cjk for the cjk profile)."""
    if notation:
        return NOTATIONS[notation]
    return CJK if profile == "cjk" else EDITORIAL


def build_prompt(profile: str, language: str | None = None, layout: str = "paragraphs",
                 diplomatic: bool = False, notation: str | None = None) -> str:
    if profile not in PROFILES:
        raise SystemExit(f"unknown profile {profile!r}; choose from {', '.join(PROFILES)}")
    n = notation_for(profile, notation)
    lang = f" The document is expected to be in: {language}." if language else ""
    prompt = CORE.format(language=lang, layout=LAYOUT[layout], letterforms=LETTERFORMS[diplomatic],
                         notation=NOTATION_RULES[n.name])
    return prompt + PROFILE_RULES[profile].replace("{illegible}", n.illegible)


def prompt_id(prompt: str) -> str:
    """Short fingerprint recorded with every run, so a transcription can be tied to its exact prompt."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]
