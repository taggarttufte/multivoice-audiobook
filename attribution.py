"""
Heuristic dialogue attribution.

Splits text into [{"speaker": str, "text": str, "conf": "HIGH"|"MED"|"LOW"|"UNK"}].
- Narration is always {speaker: "narrator", conf: "HIGH"}.
- Dialogue is tagged via:
    HIGH: explicit tag found ("Name said," / "said Name") — including titled
          forms like "Mr. Bennet said".
    MED:  alternation rule (clear back-and-forth), OR pronoun-style tag
          ("said his lady", "she replied") resolved to most-recent speaker
          of matching gender.
    LOW:  best-guess fallback to most recent named speaker.
    UNK:  bare dialogue with no anchor at all.

The UNK/LOW segments are the ones an LLM pass would tune later.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

# Verbs that signal a dialogue tag.
SPEECH_VERBS = (
    r"said|asked|replied|whispered|shouted|muttered|exclaimed|"
    r"cried|called|answered|continued|added|murmured|hissed|growled|"
    r"sighed|laughed|grinned|protested|insisted|repeated|noted|remarked|"
    r"yelled|screamed|gasped|stammered|mumbled|snapped|barked|spoke|"
    r"returned|observed|rejoined|went on|put in"
)

# Honorific titles that can prefix a surname (Mr. Bennet, Lady Catherine, ...).
TITLES = (
    r"Mr|Mrs|Miss|Mme|Mlle|Lord|Lady|Sir|Dame|Dr|Doctor|"
    r"Father|Brother|Sister|Captain|Colonel|General|Major|Lieutenant"
)

# A speaker name: optional title + capitalized surname.
_NAME = rf"(?:(?:{TITLES})\.?\s+)?[A-Z][a-z]{{2,}}"

# Tag patterns: explicit named speaker.
PAT_AFTER          = re.compile(rf"^\s*[,:]?\s*({_NAME})\s+(?:{SPEECH_VERBS})\b")
PAT_AFTER_INVERTED = re.compile(rf"^\s*[,:]?\s*(?:{SPEECH_VERBS})\s+({_NAME})\b")
PAT_BEFORE         = re.compile(rf"\b({_NAME})\s+(?:{SPEECH_VERBS})\s*[,:]?\s*$")

# Pronoun-style tags. Order matters — longer phrases first so "his wife"
# wins over plain "his". Captured group is the pronoun phrase.
_PRONOUNS = r"his\s+wife|his\s+lady|her\s+husband|her\s+lord|she|he"
PAT_PRONOUN_AFTER       = re.compile(rf"^\s*[,:]?\s*({_PRONOUNS})\s+(?:{SPEECH_VERBS})\b", re.IGNORECASE)
PAT_PRONOUN_BEFORE      = re.compile(rf"\b({_PRONOUNS})\s+(?:{SPEECH_VERBS})\s*[,:]?\s*$", re.IGNORECASE)
PAT_PRONOUN_VERB_FIRST  = re.compile(rf"^\s*[,:]?\s*(?:{SPEECH_VERBS})\s+({_PRONOUNS})\b", re.IGNORECASE)

# Quotes — accept smart quotes (left/right double) and straight ".
OPEN_QUOTES  = "“\""
CLOSE_QUOTES = "”\""

_STRIP_TITLE_RE = re.compile(rf"^(?:{TITLES})\.?\s+")


def _pronoun_gender(phrase: str) -> str | None:
    p = phrase.lower().strip()
    if "she" in p or "lady" in p or ("wife" in p and "husband" not in p):
        return "F"
    if "lord" in p or "husband" in p:
        return "M"
    if p == "he":
        return "M"
    return None


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
            segments.append(("D", text[j:]))
            break
        segments.append(("D", text[j:k + 1]))
        i = k + 1
    return segments


def _resolve_capture(captured: str, known: set[str]) -> str | None:
    """Match captured speaker text against the cast. Try the full capture
    first ("Mr. Darcy"), then surname-only ("Darcy")."""
    if captured in known:
        return captured
    stripped = _STRIP_TITLE_RE.sub("", captured).strip()
    if stripped and stripped != captured and stripped in known:
        return stripped
    return None


# Words after a speech verb that signal INDIRECT speech (not a quote tag):
# "Name replied that he had not", "Mr. Bennet asked how she fared".
# Only treat the leading "Name verb" as a dialogue tag if it's NOT followed
# by one of these.
_INDIRECT_AFTER_VERB = re.compile(
    r"^\s*(?:that|how|what|whether|if|when|where|why|whom|who)\b",
    re.IGNORECASE,
)


def _is_indirect(narration: str, match_end: int) -> bool:
    return bool(_INDIRECT_AFTER_VERB.match(narration[match_end:match_end + 30]))


def _find_explicit_speaker(prev_n: str, next_n: str, known: set[str]) -> str | None:
    """Look for 'Name said' / 'said Name' in surrounding narration.
    Only accept names in the known cast (filters random capitalized words).
    Reject indirect speech ('Mr. Bennet replied that he had not')."""
    m = PAT_BEFORE.search(prev_n)
    if m:
        sp = _resolve_capture(m.group(1), known)
        if sp:
            return sp
    m = PAT_AFTER.match(next_n)
    if m and not _is_indirect(next_n, m.end()):
        sp = _resolve_capture(m.group(1), known)
        if sp:
            return sp
    m = PAT_AFTER_INVERTED.match(next_n)
    if m and not _is_indirect(next_n, m.end()):
        sp = _resolve_capture(m.group(1), known)
        if sp:
            return sp
    return None


def _find_pronoun_phrase(prev_n: str, next_n: str) -> str | None:
    """Look for pronoun-style tags ('she said', 'his wife replied', 'said she').
    Returns the matched pronoun phrase verbatim ('his wife', 'she', etc.)."""
    for n in (prev_n, next_n):
        m = (PAT_PRONOUN_BEFORE.search(n)
             or PAT_PRONOUN_AFTER.match(n)
             or PAT_PRONOUN_VERB_FIRST.match(n))
        if m:
            return m.group(1)
    return None


_MR_RE  = re.compile(r"\bMr\.?\s+([A-Z][a-z]+)\b")
_MRS_RE = re.compile(r"\b(?:Mrs|Miss|Lady)\.?\s+([A-Z][a-z]+)\b")


_NAME_FINDER_RE = re.compile(rf"\b({_NAME})\b")


def _resolve_pronoun_speaker(phrase: str, out: list[dict],
                             characters: Mapping[str, dict],
                             text_male_surnames: set[str],
                             text_female_surnames: set[str]) -> str | None:
    """Resolve a pronoun-tagged dialogue ('she said', 'his wife replied') to
    a specific speaker.

    Search order:
      1) Spousal pairing using a pre-scan of the whole chunk: if "his wife"
         appears anywhere and there's a "Mr. X" anywhere in this chunk plus
         a "Mrs. X" in the cast, that's the speaker.
      2) Most recent confident speaker matching gender.
      3) Scene-local: any matching-gender character mentioned in recent
         narration.
      4) Cast-wide fallback: most-prominent character of that gender.
    """
    p = phrase.lower().strip()
    gender = _pronoun_gender(phrase)
    if not gender:
        return None

    known = set(characters.keys())

    # 1) Spousal pairing
    if "wife" in p or "lady" in p:
        for surname in text_male_surnames:
            for candidate in (f"Mrs. {surname}", f"Lady {surname}"):
                if candidate in known and characters[candidate].get("gender") == "F":
                    return candidate
    if "husband" in p or "lord" in p:
        for surname in text_female_surnames:
            if f"Mr. {surname}" in known and characters[f"Mr. {surname}"].get("gender") == "M":
                return f"Mr. {surname}"

    # 2) Recent confident speaker
    seen: set[str] = set()
    for s in reversed(out):
        sp = s["speaker"]
        if sp in ("narrator", "?", "unknown") or sp in seen:
            continue
        seen.add(sp)
        if s["conf"] in ("HIGH", "MED") and characters.get(sp, {}).get("gender") == gender:
            return sp

    # 3) Titled character present in this chunk (from pre-scan).
    # Prefer this over book-wide fallback so "he said" in a Bennet scene
    # resolves to Mr. Bennet, not the most-prominent male book-wide.
    title = "Mr." if gender == "M" else "Mrs."
    surnames_in_text = text_male_surnames if gender == "M" else text_female_surnames
    for surname in surnames_in_text:
        candidate = f"{title} {surname}"
        if candidate in known and characters[candidate].get("gender") == gender:
            return candidate

    # 4) Any matching-gender character mentioned in recent narration.
    recent_narrations = [s["text"] for s in out if s["speaker"] == "narrator"][-6:]
    for n in reversed(recent_narrations):
        for m in _NAME_FINDER_RE.finditer(n):
            sp = _resolve_capture(m.group(1), known)
            if sp and characters.get(sp, {}).get("gender") == gender:
                return sp

    # 5) Cast-wide fallback
    candidates = [(name, info.get("speak_count", 0))
                  for name, info in characters.items()
                  if info.get("gender") == gender]
    if candidates:
        return max(candidates, key=lambda kv: kv[1])[0]
    return None


def attribute(text: str, cast_or_chars) -> list[dict]:
    """Main entrypoint. Returns ordered list of segments with attribution.

    `cast_or_chars` accepts either a full cast dict (with 'characters' key)
    or a plain iterable of character names. Passing the full cast unlocks
    pronoun-tag resolution by gender."""
    if isinstance(cast_or_chars, dict) and "characters" in cast_or_chars:
        characters = cast_or_chars["characters"]
        known = set(characters.keys())
    else:
        characters = {name: {} for name in cast_or_chars}
        known = set(cast_or_chars)

    raw = _segment_quotes(text)

    # Pre-scan: which titled names appear in the entire chunk, and how often?
    # Ordered by descending frequency so the most-mentioned character in this
    # scene wins when we're guessing from a pronoun.
    from collections import Counter as _Counter
    _m_counts = _Counter(m.group(1) for m in _MR_RE.finditer(text))
    _f_counts = _Counter(m.group(1) for m in _MRS_RE.finditer(text))
    text_male_surnames   = [s for s, _ in _m_counts.most_common()]
    text_female_surnames = [s for s, _ in _f_counts.most_common()]

    # Pre-scan the text body for any character names mentioned. Lets us detect
    # 2-person scenes and force alternation when dialogue runs untagged.
    present_chars: set[str] = set()
    for m in _NAME_FINDER_RE.finditer(text):
        sp = _resolve_capture(m.group(1), known)
        if sp:
            present_chars.add(sp)

    out: list[dict] = []

    for idx, (kind, t) in enumerate(raw):
        if kind == "N":
            out.append({"speaker": "narrator", "text": t, "conf": "HIGH"})
            continue

        prev_n = raw[idx - 1][1] if idx > 0     and raw[idx - 1][0] == "N" else ""
        next_n = raw[idx + 1][1] if idx + 1 < len(raw) and raw[idx + 1][0] == "N" else ""

        # 1) Explicit named speaker — highest confidence.
        speaker = _find_explicit_speaker(prev_n, next_n, known)
        if speaker:
            out.append({"speaker": speaker, "text": t, "conf": "HIGH"})
            continue

        # 2) Pronoun-style tag — resolve via spousal pairing / recent speakers.
        phrase = _find_pronoun_phrase(prev_n, next_n)
        if phrase and characters:
            sp = _resolve_pronoun_speaker(
                phrase, out, characters,
                text_male_surnames, text_female_surnames,
            )
            if sp:
                out.append({"speaker": sp, "text": t, "conf": "MED"})
                continue

        # 3) Alternation. Last 2 confident dialogue speakers were A, B → next is A.
        recent = [s["speaker"] for s in reversed(out)
                  if s["speaker"] != "narrator" and s["conf"] in ("HIGH", "MED")][:2]
        if len(recent) == 2 and recent[0] != recent[1]:
            out.append({"speaker": recent[1], "text": t, "conf": "MED"})
            continue

        # 3b) Two-person scene: only 1 recent speaker, but exactly 2 characters
        # are mentioned in the chunk. Untagged dialogue alternates to the other.
        if len(present_chars) == 2 and recent and recent[0] in present_chars:
            other = next(iter(present_chars - {recent[0]}))
            out.append({"speaker": other, "text": t, "conf": "MED"})
            continue

        # 4) Lean toward most recent named speaker, low confidence.
        if recent:
            out.append({"speaker": recent[0], "text": t, "conf": "LOW"})
            continue

        # 5) No anchor.
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
