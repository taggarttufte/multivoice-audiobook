"""
Heuristic dialogue attribution.

Splits text into [{"speaker": str, "text": str, "conf": "HIGH"|"MED"|"LOW"|"UNK"}].
- Narration is always {speaker: "narrator", conf: "HIGH"}.
- Dialogue is tagged via:
    HIGH: explicit tag found (preceding "Name said," or following "said Name")
    MED:  alternation rule (clear back-and-forth, infer next speaker)
    LOW:  best-guess narrator fallback
    UNK:  bare dialogue with no anchor at all

The UNK/LOW segments are the ones an LLM pass would tune later.
"""
from __future__ import annotations

import re
from typing import Iterable

# Verbs that signal a dialogue tag (same set as cast_builder).
SPEECH_VERBS = (
    r"said|asked|replied|whispered|shouted|muttered|exclaimed|"
    r"cried|called|answered|continued|added|murmured|hissed|growled|"
    r"sighed|laughed|grinned|protested|insisted|repeated|noted|remarked|"
    r"yelled|screamed|gasped|stammered|mumbled|snapped|barked|spoke"
)

# Tag patterns
PAT_AFTER  = re.compile(rf"^\s*[,:]?\s*([A-Z][a-z]{{2,}})\s+(?:{SPEECH_VERBS})\b")
PAT_AFTER_INVERTED = re.compile(rf"^\s*[,:]?\s*(?:{SPEECH_VERBS})\s+([A-Z][a-z]{{2,}})\b")
PAT_BEFORE = re.compile(rf"\b([A-Z][a-z]{{2,}})\s+(?:{SPEECH_VERBS})\s*[,:]?\s*$")

# Quotes — accept smart quotes (left/right double) and straight ".
OPEN_QUOTES  = "“\""
CLOSE_QUOTES = "”\""


def _segment_quotes(text: str) -> list[tuple[str, str]]:
    """Split text into alternating [(kind, text), ...] where kind in {'N','D'}.
    Quotes are kept as part of the dialogue segment for fidelity to TTS."""
    segments: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        # Find next opening quote
        j = -1
        for ch in OPEN_QUOTES:
            k = text.find(ch, i)
            if k != -1 and (j == -1 or k < j):
                j = k
        if j == -1:
            tail = text[i:]
            if tail.strip():
                segments.append(("N", tail))
            break
        if j > i:
            segments.append(("N", text[i:j]))
        # Find matching closing quote
        k = -1
        for ch in CLOSE_QUOTES:
            kk = text.find(ch, j + 1)
            if kk != -1 and (k == -1 or kk < k):
                k = kk
        if k == -1:
            # Unclosed — treat rest as dialogue
            segments.append(("D", text[j:]))
            break
        segments.append(("D", text[j:k + 1]))
        i = k + 1
    return segments


def _find_explicit_speaker(prev_n: str, next_n: str, known: set[str]) -> str | None:
    """Look for 'Name said' / 'said Name' in surrounding narration.
    Only accept names in the known cast; this filters out random capitalized
    words (chapter starts, place names, etc.)."""
    m = PAT_BEFORE.search(prev_n)
    if m and m.group(1) in known:
        return m.group(1)
    m = PAT_AFTER.match(next_n)
    if m and m.group(1) in known:
        return m.group(1)
    m = PAT_AFTER_INVERTED.match(next_n)
    if m and m.group(1) in known:
        return m.group(1)
    return None


def attribute(text: str, known_chars: Iterable[str]) -> list[dict]:
    """Main entrypoint. Returns ordered list of segments with attribution."""
    known = set(known_chars)
    raw = _segment_quotes(text)

    out: list[dict] = []

    for idx, (kind, t) in enumerate(raw):
        if kind == "N":
            out.append({"speaker": "narrator", "text": t, "conf": "HIGH"})
            continue

        # Dialogue. Find context.
        prev_n = raw[idx - 1][1] if idx > 0     and raw[idx - 1][0] == "N" else ""
        next_n = raw[idx + 1][1] if idx + 1 < len(raw) and raw[idx + 1][0] == "N" else ""

        speaker = _find_explicit_speaker(prev_n, next_n, known)
        if speaker:
            out.append({"speaker": speaker, "text": t, "conf": "HIGH"})
            continue

        # Try alternation. Look at the last 2 confident dialogue speakers
        # before this one (skipping narrator).
        recent = [s["speaker"] for s in reversed(out)
                  if s["speaker"] != "narrator" and s["conf"] in ("HIGH", "MED")][:2]
        if len(recent) == 2 and recent[0] != recent[1]:
            # recent[0] = most recent. The one before that probably speaks next.
            out.append({"speaker": recent[1], "text": t, "conf": "MED"})
            continue

        # If we have ANY recent confident speaker, lean toward them at LOW.
        if recent:
            out.append({"speaker": recent[0], "text": t, "conf": "LOW"})
            continue

        # No anchor at all
        out.append({"speaker": "?", "text": t, "conf": "UNK"})

    return out


def stats(segments: list[dict]) -> dict:
    """Quick summary of attribution confidence — useful for QA."""
    by_conf: dict[str, int] = {}
    by_speaker: dict[str, int] = {}
    for s in segments:
        by_conf[s["conf"]] = by_conf.get(s["conf"], 0) + 1
        by_speaker[s["speaker"]] = by_speaker.get(s["speaker"], 0) + 1
    return {"by_conf": by_conf, "by_speaker": by_speaker, "total": len(segments)}
