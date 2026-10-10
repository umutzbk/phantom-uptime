"""Match every figure in an answer back to a value some tool actually returned.

This used to live twice: once in the browser, once in validate_traces.py. Two
implementations of a correctness check will drift, and the browser copy meant
anyone calling the API directly got no check at all. One implementation, on the
server, used by the live loop, by replay, and by the offline gate.

What it can do: catch a figure that no tool produced.
What it cannot do: catch a wrong claim built out of real figures. A superlative
is the example: "the largest contributor is CHG-0034" contains only true
numbers and is still false. That needs a different check, and knowing the
difference is the point.
"""
from __future__ import annotations

import re
from typing import Any

MONTHS = (r"january|february|march|april|may|june|july|august|september|october|"
          r"november|december")

# A figure may be rounded. It may not be a different number.
TOLERANCE_ABS = 0.02
TOLERANCE_REL = 0.012

FIGURE_RE = re.compile(
    r"(?:£\s?(\d[\d,]*\.?\d*)(?:\s?(k|m)(?![A-Za-z]))?)"       # money
    r"|(?:(\d[\d,]*\.?\d*)\s*%)"                               # percent
    r"|(?:(\d[\d,]*\.?\d*)\s*(kwh|mwh|pp|percentage points|minutes|kw))"
    # the lookbehind matters: without it "4.71 points" yields a second,
    # imaginary figure of 71 taken from the decimal part
    r"|(?:(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\b)"         # grouped thousands
    # two digits or more, or anything with a decimal part: 3.96 and 0.40 are
    # the headline figures and a two-digit rule would skip them
    r"|(?:(?<![\d.])(\d{2,}(?:\.\d+)?|\d\.\d+)\b)",
    re.I)


def pool_from(obj: Any, out: set[float] | None = None) -> set[float]:
    """Every number anywhere in a tool result, plus the forms a writer would
    reasonably quote it in: rounded, to one decimal, as a complement, as
    thousands."""
    if out is None:
        out = set()
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        v = float(obj)
        out.update({v, round(v), round(v, 1), round(100.0 - v, 2), round(v / 1000.0, 1)})
        return out
    if isinstance(obj, dict):
        for value in obj.values():
            pool_from(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            pool_from(value, out)
    elif isinstance(obj, str):
        for m in re.findall(r"-?\d+\.?\d*", obj):
            try:
                out.add(float(m))
            except ValueError:
                pass
    return out


def _mask(text: str) -> str:
    """Blank the spans that are not claims about the data, keeping the length so
    offsets into the original still hold."""
    def blank(m: re.Match) -> str:
        return " " * len(m.group(0))

    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", blank, text)
    t = re.sub(r"\b\d{1,2}\s*(?:to|and|-)\s*\d{1,2}\s+(?:" + MONTHS + r")", blank, t, flags=re.I)
    t = re.sub(r"\b\d{1,2}\s+(?:" + MONTHS + r")\b", blank, t, flags=re.I)
    t = re.sub(r"\b(?:" + MONTHS + r")\s+\d{1,2}\b", blank, t, flags=re.I)
    t = re.sub(r"\b(?:19|20)\d{2}\b", blank, t)
    # identifiers are names, not figures: CHG-0034 is not the number 34
    t = re.sub(r"\b[A-Z]{2,4}-\d+\b", blank, t)
    # nor is a clock time
    t = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", blank, t)
    return t


def figures_in(text: str) -> list[dict]:
    """Figures a director would repeat, with their position in the original."""
    masked = _mask(text)
    found: list[dict] = []
    for m in FIGURE_RE.finditer(masked):
        raw = next((g for g in m.groups() if g and re.match(r"^\d", g)), None)
        if raw is None:
            continue
        value = float(raw.replace(",", ""))
        suffix = (m.group(2) or "").lower()
        if suffix == "k":
            value *= 1000
        elif suffix == "m":
            value *= 1_000_000
        found.append({"text": text[m.start():m.end()].strip(),
                      "value": value, "start": m.start(), "end": m.end()})
    return found


def matches(value: float, pool: set[float]) -> bool:
    return any(abs(value - p) <= max(TOLERANCE_ABS, abs(p) * TOLERANCE_REL)
               for p in pool)


def check(answer: str, tool_results: list[Any]) -> dict:
    """The verdict the UI renders and the offline gate asserts on."""
    if not answer or not tool_results:
        return {"checked": 0, "unsourced": [], "tools": len(tool_results or [])}
    pool: set[float] = set()
    for result in tool_results:
        pool_from(result, pool)
    figures = figures_in(answer)
    unsourced = [f for f in figures if not matches(f["value"], pool)]
    return {"checked": len(figures), "unsourced": unsourced,
            "tools": len(tool_results)}
