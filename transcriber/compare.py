"""Align two (or three) independent transcriptions of a page and mark where they differ.

There is no ground truth for a scan, so agreement between independent models is the check.
Text is split into tokens: every CJK character (and every □) is one token, and in other
scripts every word is one token. Punctuation, spacing and Markdown are ignored when
comparing: they are formatting, not reading. Unicode compatibility forms are folded (long
s to s, ligatures to letters, full-width to ASCII), so only real differences in reading count.

Model A's text is the base. Where B differs, A's reading is kept and a marker is added:
    {A|B}        the models read this differently (〔A|B〕 in the CJK notation)
    {A|}  {|B}   a short span only one of them has
    {+B: ...}    a long span only B has: a line A probably dropped (【补B:…】)
With a third model C, each disagreement is put to a vote; markers remain only where C does
not side with A or B, or where only B (not C) saw a missing line.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .profiles import BLANK, Notation

CJK_CLASS = (
    "⺀-⿿々-〇〡-〩぀-ヿ㄀-ㇿ㐀-䶿一-鿿"
    "ꀀ-꓏가-힯豈-﫿\U00020000-\U0003ffff฀-๿□"
)
WORD = rf"(?:(?![{CJK_CLASS}])[^\W_])+"
TOKEN_RE = re.compile(rf"[{CJK_CLASS}]|{WORD}(?:['’]{WORD})*")
IS_CJK = re.compile(rf"[{CJK_CLASS}]")
ILLEGIBLE_NORM = "\x00"  # never equal to a real reading
APOLOGY_RE = re.compile(
    r"I'm sorry|I am sorry|I cannot|I can't|as an AI|unable to (?:transcribe|read)|"
    r"抱歉|无法(?:识别|转录|看清)|作为(?:一个)?(?:AI|人工智能|语言模型)|"
    r"je ne peux pas|ich kann nicht|no puedo|non posso", re.I)


@dataclass
class Tok:
    norm: str
    start: int
    end: int


def tokenize(raw: str, notation: Notation) -> list[Tok]:
    """Tokens of a page. Each illegible marker ([illegible], □) is one token that never matches
    a real reading; an uncertain marker's words ([Lowell?]) are compared as ordinary words."""
    ill = [m.span() for m in re.finditer(re.escape(notation.illegible), raw)]
    toks = [Tok(ILLEGIBLE_NORM, a, b) for a, b in ill]
    for m in TOKEN_RE.finditer(raw):
        s, e = m.span()
        if not any(a <= s < b for a, b in ill):
            toks.append(Tok(unicodedata.normalize("NFKC", m.group()), s, e))
    toks.sort(key=lambda t: t.start)
    return toks


def norms(toks: list[Tok]) -> list[str]:
    return [t.norm for t in toks]


def join_tokens(words: list[str]) -> str:
    """Tokens back into readable text: CJK characters run together, other words take spaces."""
    out = ""
    for w in words:
        if out and not (IS_CJK.match(out[-1]) or IS_CJK.match(w[0])):
            out += " "
        out += w
    return out


def content_len(words: list[str]) -> int:
    return sum(len(w) for w in words)


@dataclass
class PageCheck:
    page: int
    a: str | None
    b: str | None
    c: str | None = None
    merged: str = ""
    voted: int = 0  # disagreements settled by C's majority vote
    ratio: float = 0.0
    conflicts: int = 0
    dropped: int = 0  # long B-only spans: probable dropped lines in A
    a_only: int = 0  # long A-only spans (usually image descriptions)
    uncertain: int = 0  # markers written by model A itself
    flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.flags

    @property
    def severity(self) -> float:
        return self.dropped * 10 + self.conflicts + self.uncertain + (100 if self.a is None or self.b is None else 0)


def c_boundaries(na: list[str], nc: list[str]) -> tuple[list[int], list[int]]:
    """Map each boundary between A's tokens to C: lo/hi are the earliest/latest C index it
    corresponds to; they differ only where C has an insertion at that boundary."""
    n = len(na)
    lo: list = [None] * (n + 1)
    hi: list = [None] * (n + 1)

    def put(i: int, j: int) -> None:
        lo[i] = j if lo[i] is None else min(lo[i], j)
        hi[i] = j if hi[i] is None else max(hi[i], j)

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, na, nc, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1 + 1):
                put(i1 + k, j1 + k)
        else:
            put(i1, j1)
            put(i2, j2)
            for k in range(i1 + 1, i2):
                put(k, j1)
    return lo, hi


def similar(x: list[str], y: list[str]) -> bool:
    return x == y or (bool(x) and bool(y) and SequenceMatcher(None, x, y, autojunk=False).ratio() >= 0.8)


def at_line_start(raw: str, p: int) -> bool:
    """Only spaces between the previous line break (or the start) and position p."""
    k = raw.rfind("\n", 0, p)
    return raw[k + 1:p].strip() == ""


def slide_inserts(ops: list[tuple], nb: list[str], tb: list[Tok], b_raw: str) -> list[list]:
    """An insertion of B-only tokens can often sit in several places with the same result
    (A 'x y', B 'x y z y' could be 'x y [z y]' or 'x [y z] y'). Prefer the place where B's
    inserted text starts a line, so a dropped line is reported as that whole line."""
    ops = [list(o) for o in ops]
    for idx, op in enumerate(ops):
        if op[0] != "insert" or idx == 0 or ops[idx - 1][0] != "equal":
            continue
        prev = ops[idx - 1]
        _, i1, _, j1, j2 = op
        best = 0
        if not at_line_start(b_raw, tb[j1].start):
            k = 1
            while k <= prev[2] - prev[1] and nb[j2 - k] == nb[j1 - k]:
                if at_line_start(b_raw, tb[j1 - k].start):
                    best = k
                    break
                k += 1
        if not best:
            continue
        prev[2] -= best
        prev[4] -= best
        op[1] -= best
        op[2] -= best
        op[3] -= best
        op[4] -= best
        nxt = ops[idx + 1] if idx + 1 < len(ops) else None
        if nxt and nxt[0] == "equal":
            nxt[1] -= best
            nxt[3] -= best
        else:
            ops.insert(idx + 1, ["equal", op[2], op[2] + best, op[4], op[4] + best])
    return [o for o in ops if not (o[0] == "equal" and o[1] == o[2])]


def merge_page(a_raw: str, b_raw: str, pc: PageCheck, notation: Notation, drop_chars: int,
               c_raw: str | None = None) -> str:
    """A's text with review markers where B (and C) read differently."""
    ta, tb = tokenize(a_raw, notation), tokenize(b_raw, notation)
    na, nb = norms(ta), norms(tb)
    markers = [m.span() for m in notation.mark_re.finditer(a_raw)]
    nc: list[str] = []
    if c_raw is not None:
        nc = norms(tokenize(c_raw, notation))
        c_lo, c_hi = c_boundaries(na, nc)

    def c_reading(i1: int, i2: int) -> list[str] | None:
        if c_raw is None:
            return None
        return nc[c_lo[i1]:c_hi[i1]] if i1 == i2 else nc[c_hi[i1]:c_lo[i2]]

    def unwrap(s: str) -> str:
        """A's own doubt markers become plain readings inside a disagreement marker."""
        return notation.uncertain_re.sub(lambda m: m.group()[len(notation.unc_open):-len(notation.unc_close)], s)

    def b_text(j1: int, j2: int, whole_line: bool = False) -> str:
        end = tb[j2 - 1].end
        if whole_line:  # a restored line keeps its closing punctuation ("March 3." not "March 3")
            m = re.match(r"[^\s\w{}【】〔〕]+(?=\s|$)", b_raw[end:])
            if m:
                end += m.end()
        span = re.sub(r"\s+", " ", b_raw[tb[j1].start:end])
        return unwrap(span)

    out: list[str] = []
    pos = 0

    def a_span(i1: int, i2: int) -> tuple[int, int]:
        """Raw span of A's tokens i1..i2, widened to whole markers it cuts into (never behind pos)."""
        s, e = ta[i1].start, ta[i2 - 1].end
        for ms, me in markers:
            if ms < e and me > s:
                if ms >= pos:
                    s = min(s, ms)
                e = max(e, me)
        return s, e

    def emit_to(p: int) -> None:
        nonlocal pos
        if p > pos:
            out.append(a_raw[pos:p])
            pos = p

    def joins(left: str, right: str) -> bool:
        """Two neighbouring characters that need a space between them (word | word, not CJK)."""
        return left.isalnum() and right.isalnum() and not IS_CJK.match(left) and not IS_CJK.match(right)

    def insert_point(i1: int, j1: int, j2: int, text: str) -> tuple[int, str]:
        """Where text A lacks goes (before A's token i1), with the spacing it needs to read right."""
        gap = a_raw[pos:ta[i1].start] if i1 < len(ta) else a_raw[pos:]
        if i1 == len(ta) and at_line_start(b_raw, tb[j1].start):
            return len(a_raw.rstrip()), "\n" + text  # B has more lines after A's last one
        if "\n" in gap and at_line_start(b_raw, tb[j1].start):
            # B has it as (the start of) a line of its own: put it on its own line in A too
            p = pos + gap.rindex("\n") + 1
            b_after = b_raw[tb[j2 - 1].end:tb[j2].start if j2 < len(tb) else len(b_raw)]
            ends_line = "\n" in b_after and i1 < len(ta)
            return p, text + ("\n" if ends_line else (" " if joins(text[-1:], a_raw[p:p + 1]) else ""))
        if i1 < len(ta) and gap.strip() == "":
            p = ta[i1].start  # only whitespace before the next token: insert right before it
        else:
            p = pos  # punctuation follows the previous token: insert before the punctuation
        prev, nxt = (a_raw[p - 1] if p else ""), a_raw[p:p + 1]
        return p, (" " if joins(prev, text[:1]) else "") + text + (" " if joins(text[-1:], nxt) else "")

    ops = slide_inserts(SequenceMatcher(None, na, nb, autojunk=False).get_opcodes(), nb, tb, b_raw)
    for tag, i1, i2, j1, j2 in ops:
        x, y = na[i1:i2], nb[j1:j2]
        if tag == "equal":
            emit_to(ta[i2 - 1].end)
        elif tag == "replace":
            s, e = a_span(i1, i2)
            emit_to(s)
            body = unwrap(a_raw[s:e])
            pos = e
            c = c_reading(i1, i2)
            if c is not None and c == x:
                out.append(body)
                pc.voted += 1
            elif c is not None and c == y:
                out.append(b_text(j1, j2))
                pc.voted += 1
            else:
                out.append(notation.conflict(body, b_text(j1, j2)) if c is None
                           else notation.conflict(body, b_text(j1, j2), join_tokens(c)))
                pc.conflicts += 1
        elif tag == "delete":
            s, e = a_span(i1, i2)
            emit_to(s)
            chunk = a_raw[s:e]
            pos = e
            c = c_reading(i1, i2)
            if content_len(x) >= drop_chars:
                out.append(chunk)  # a long A-only span (an image description, say) stays as it is
                pc.a_only += 1
            elif c is not None and c == x:
                out.append(chunk)
                pc.voted += 1
            elif c == []:
                pc.voted += 1  # B and C both lack it: drop it
            else:
                body = unwrap(chunk)
                out.append(notation.conflict(body, "") if c is None else notation.conflict(body, "", join_tokens(c)))
                pc.conflicts += 1
        elif tag == "insert":
            c = c_reading(i1, i1)
            long = content_len(y) >= drop_chars
            text = b_text(j1, j2, whole_line=long)
            p, spaced = insert_point(i1, j1, j2, text)
            emit_to(p)
            lead, trail = spaced[:len(spaced) - len(spaced.lstrip(" \n"))], spaced[len(spaced.rstrip(" \n")):]
            if c is not None and similar(c, y):
                if long:
                    out.append(lead + notation.drop("BC", text) + trail)
                    pc.dropped += 1
                else:
                    out.append(spaced)
                    pc.voted += 1
            elif c == []:
                if long:  # only B saw it: maybe a line A dropped, maybe B invented it
                    out.append(lead + notation.drop("B", text) + trail)
                    pc.dropped += 1
                else:
                    pc.voted += 1  # A and C agree there is nothing here
            elif long:
                out.append(lead + notation.drop("B", text) + trail)
                pc.dropped += 1
            else:
                # the spacing goes inside the marker, so either choice reads correctly
                out.append(notation.conflict("", spaced) if c is None else notation.conflict("", spaced, join_tokens(c)))
                pc.conflicts += 1
    out.append(a_raw[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def is_blank(text: str) -> bool:
    return BLANK in text or "(空白页)" in text


def check_page(i: int, a: str | None, b: str | None, notation: Notation, median_len: float, *,
               threshold: float, drop_chars: int, conflict_rate: float, c: str | None = None,
               looks_looped=None) -> PageCheck:
    pc = PageCheck(i + 1, a, b, c)
    if a is None or b is None:
        pc.flags.append("missing in " + ("A" if a is None else "B"))
        pc.merged = (a or b or "").strip()
        return pc
    na, nb = norms(tokenize(a, notation)), norms(tokenize(b, notation))
    pc.ratio = SequenceMatcher(None, na, nb, autojunk=False).ratio() if (na or nb) else 1.0
    pc.uncertain = notation.count_uncertain(a)
    blank_a, blank_b = is_blank(a), is_blank(b)
    if blank_a and blank_b:
        pc.ratio, pc.merged = 1.0, a.strip()
        return pc
    if blank_a != blank_b:
        pc.flags.append("one model says blank")
    pc.merged = merge_page(a, b, pc, notation, drop_chars, c)
    if pc.dropped:
        pc.flags.append(f"{pc.dropped} dropped line(s)?")
    if pc.conflicts and (pc.conflicts >= 15 or pc.conflicts / max(len(na), 1) > conflict_rate):
        pc.flags.append(f"{pc.conflicts} disagreements")
    if pc.uncertain:
        pc.flags.append(f"{pc.uncertain} uncertain")
    if pc.ratio < threshold:
        pc.flags.append(f"agreement {pc.ratio:.2f}")
    la, lb = content_len(na), content_len(nb)
    if median_len and min(la, lb) < 0.4 * median_len:
        pc.flags.append("short page")
    for label, text in (("A", a), ("B", b)):
        if notation.truncated in text:
            pc.flags.append(f"truncated in {label}")
        if "```" in text:
            pc.flags.append(f"code fence in {label}")
        if APOLOGY_RE.search(text):
            pc.flags.append(f"refusal or apology in {label}")
        if looks_looped and looks_looped(text):
            pc.flags.append(f"repetition loop in {label}")
    return pc
