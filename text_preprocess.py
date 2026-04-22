"""
TTS text preprocessing: pronunciation dictionary, Roman numerals, abbreviations.

Run before sending text to a TTS engine to fix common readability problems
(weird proper noun pronunciations, "Chapter XII" being read letter-by-letter,
"Mr." being read as the letters M-R, etc.).

The pronunciation dictionary is user-editable per book. See
`examples/pronunciations.example.json` for the schema.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping


# --- Roman numerals ---------------------------------------------------------

_ROMAN_VALUES = [
    ("M", 1000), ("CM", 900), ("D", 500), ("CD", 400),
    ("C", 100),  ("XC", 90),  ("L", 50),  ("XL", 40),
    ("X", 10),   ("IX", 9),   ("V", 5),   ("IV", 4), ("I", 1),
]

# Words that commonly precede a Roman numeral in books.
_ROMAN_CONTEXT_WORDS = (
    r"Chapter|Chap\.?|Volume|Vol\.?|Part|Book|Section|Episode|Act|Scene"
)
# Anchored: <context word> <ROMAN>
_ROMAN_ANCHORED_RE = re.compile(
    rf"\b({_ROMAN_CONTEXT_WORDS})\s+([IVXLCDM]{{1,8}})\b",
    re.IGNORECASE,
)


def roman_to_int(s: str) -> int:
    """Return the integer value of a Roman numeral, or 0 if invalid."""
    s = s.upper()
    if not s or any(c not in "IVXLCDM" for c in s):
        return 0
    result, i = 0, 0
    while i < len(s):
        for sym, val in _ROMAN_VALUES:
            if s.startswith(sym, i):
                result += val
                i += len(sym)
                break
        else:
            return 0
    return result


def convert_roman_numerals(text: str) -> str:
    """Convert Roman numerals to Arabic ONLY when anchored to a context word
    like 'Chapter', 'Volume', 'Part'. This avoids false positives on
    common English words made of Roman letters (I, MIX, DID, LID, ...)."""
    def _replace(m: re.Match) -> str:
        prefix = m.group(1)
        roman = m.group(2)
        n = roman_to_int(roman)
        return f"{prefix} {n}" if n else m.group(0)
    return _ROMAN_ANCHORED_RE.sub(_replace, text)


# --- Abbreviations ----------------------------------------------------------

DEFAULT_ABBREVIATIONS = {
    "Mr.":   "Mister",
    "Mrs.":  "Misses",
    "Ms.":   "Miss",
    "Dr.":   "Doctor",
    "St.":   "Saint",
    "Sr.":   "Senior",
    "Jr.":   "Junior",
    "Prof.": "Professor",
    "vs.":   "versus",
    "etc.":  "et cetera",
    "e.g.":  "for example",
    "i.e.":  "that is",
}


def expand_abbreviations(text: str, abbrevs: Mapping[str, str] | None = None) -> str:
    """Replace common abbreviations with their spoken expansion."""
    abbrevs = abbrevs if abbrevs is not None else DEFAULT_ABBREVIATIONS
    # Sort longest-first so "Mrs." beats "Mr." on overlapping matches.
    for abbr in sorted(abbrevs, key=len, reverse=True):
        # Match the abbreviation when it's followed by whitespace or end-of-string
        # (avoids munging things mid-word, but allows trailing period as part of abbr).
        pattern = re.escape(abbr) + r"(?=\s|$|[\"'\)\]])"
        text = re.sub(pattern, abbrevs[abbr], text)
    return text


# --- Pronunciation dictionary ----------------------------------------------

def load_pronunciations(path: Path) -> dict[str, str]:
    """Load a JSON dict of {original: replacement} from disk. Returns {} if missing."""
    if path is None or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def apply_pronunciations(
    text: str,
    pronunciations: Mapping[str, str],
    case_sensitive: bool = True,
) -> str:
    """Word-boundary replacement of each key with its value.

    Sorted longest-first so multi-word phrases ("Liaris Freese") are matched
    before single words ("Liaris"). Case-sensitive by default since most
    target words are proper nouns.
    """
    if not pronunciations:
        return text
    flags = 0 if case_sensitive else re.IGNORECASE
    items = sorted(pronunciations.items(), key=lambda kv: -len(kv[0]))
    for original, replacement in items:
        pattern = r"\b" + re.escape(original) + r"\b"
        text = re.sub(pattern, replacement, text, flags=flags)
    return text


# --- Pipeline ---------------------------------------------------------------

def preprocess_for_tts(
    text: str,
    pronunciations_path: Path | None = None,
    expand_abbrev: bool = True,
    convert_roman: bool = True,
) -> str:
    """Run the full preprocessing pipeline. Order matters: abbreviations
    first (so 'Mr.' becomes 'Mister' before any later steps see the period),
    then Roman numerals, then user pronunciations last (they win)."""
    if expand_abbrev:
        text = expand_abbreviations(text)
    if convert_roman:
        text = convert_roman_numerals(text)
    if pronunciations_path is not None:
        text = apply_pronunciations(text, load_pronunciations(pronunciations_path))
    return text


# --- Diff helper ------------------------------------------------------------

def diff_summary(original: str, processed: str, max_examples: int = 20) -> str:
    """Report what changed between original and processed text. Useful for
    eyeballing whether preprocessing did the right thing before spending TTS $."""
    if original == processed:
        return "(no changes)"
    # Walk word-by-word and surface differences.
    orig_words = original.split()
    proc_words = processed.split()
    if len(orig_words) != len(proc_words):
        return (
            f"length changed: {len(orig_words)} -> {len(proc_words)} words "
            f"({len(processed) - len(original):+d} chars)"
        )
    diffs = []
    for o, p in zip(orig_words, proc_words):
        if o != p and (o, p) not in {(d_o, d_p) for d_o, d_p in diffs}:
            diffs.append((o, p))
            if len(diffs) >= max_examples:
                break
    return "\n".join(f"  {o!r:30} -> {p!r}" for o, p in diffs)
