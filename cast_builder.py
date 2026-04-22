"""
Scan an EPUB to build a cast file for multi-voice rendering:
  - Detect candidate character names from dialogue tags ("X said")
  - Infer gender from pronoun proximity around each name
  - Auto-assign Grok voices: rex/leo for M, eve/ara for F (sal = narrator)
  - Top characters get fixed assignments; minor characters use a hashed pool
  - User can edit the resulting cast_<book_id>.json before render

Run:
  python cast_builder.py --epub <path> --book-id <id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup


# Verbs that strongly indicate a dialogue tag.
SPEECH_VERBS = (
    r"said|asked|replied|whispered|shouted|muttered|exclaimed|"
    r"cried|called|answered|continued|added|murmured|hissed|growled|"
    r"sighed|laughed|grinned|protested|insisted|repeated|noted|remarked|"
    r"yelled|screamed|gasped|stammered|mumbled|snapped|barked|spoke"
)

# "Name said" or "said Name" patterns
PAT_NAME_VERB = re.compile(rf"\b([A-Z][a-z]{{2,}})\s+(?:{SPEECH_VERBS})\b")
PAT_VERB_NAME = re.compile(rf"\b(?:{SPEECH_VERBS})\s+([A-Z][a-z]{{2,}})\b")

# Words that look like proper nouns but aren't characters.
NAME_BLACKLIST = {
    "The", "A", "An", "And", "But", "Or", "So", "Then", "Now", "Here",
    "There", "When", "What", "Why", "How", "Who", "Where", "After", "Before",
    "Once", "Just", "Suddenly", "Finally", "Even", "Still", "Still", "Yet",
    "However", "Indeed", "Perhaps", "Maybe", "Quietly", "Softly", "Yes", "No",
    "Mister", "Miss", "Mrs", "Lord", "Lady", "Sir", "Captain",
    "God", "Gods",  # tend to over-trigger in fantasy
    "Chapter", "Volume", "Part", "Book", "Prologue", "Epilogue",
    "Hey", "Well", "Oh", "Ah", "Eh", "Huh", "Ha", "Of",
    "I", "He", "She", "It", "We", "They", "You", "His", "Her", "Their",
}

# Pronoun → gender hint
MALE_PRONOUNS   = {"he", "him", "his", "himself"}
FEMALE_PRONOUNS = {"she", "her", "hers", "herself"}

PRONOUN_RE = re.compile(r"\b(he|him|his|himself|she|her|hers|herself)\b", re.IGNORECASE)

# Voice pools (Grok TTS voices)
MALE_VOICES   = ["rex", "leo"]
FEMALE_VOICES = ["eve", "ara"]
NEUTRAL_VOICE = "sal"


def extract_full_text(epub_path: Path) -> str:
    """Concatenate text from all chapter xhtml files."""
    parts = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            soup = BeautifulSoup(zf.read(name), "html.parser")
            for el in soup(["script", "style"]):
                el.decompose()
            t = re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()
            if len(t) >= 200:
                parts.append(t)
    return " ".join(parts)


def detect_speakers(text: str, min_count: int = 2) -> Counter:
    """Find candidate speaker names + counts from dialogue tags."""
    counts: Counter[str] = Counter()
    for m in PAT_NAME_VERB.finditer(text):
        counts[m.group(1)] += 1
    for m in PAT_VERB_NAME.finditer(text):
        counts[m.group(1)] += 1
    # Filter blacklist + min-count threshold
    return Counter({n: c for n, c in counts.items() if n not in NAME_BLACKLIST and c >= min_count})


def infer_gender(text: str, name: str, window: int = 200) -> str:
    """Look at text within ±window chars of each occurrence of name; tally
    male vs female pronouns; return 'M', 'F', or 'unknown'."""
    male = female = 0
    for m in re.finditer(rf"\b{re.escape(name)}\b", text):
        s = max(0, m.start() - window)
        e = min(len(text), m.end() + window)
        for pm in PRONOUN_RE.finditer(text[s:e]):
            p = pm.group(1).lower()
            if p in MALE_PRONOUNS:   male += 1
            elif p in FEMALE_PRONOUNS: female += 1
    if not (male + female):
        return "unknown"
    if male > female * 2: return "M"
    if female > male * 2: return "F"
    return "unknown"


def auto_assign_voices(characters: dict[str, dict]) -> dict[str, dict]:
    """Assign fixed voices to top characters by frequency, by gender pool.
    The very top male and female get rex / eve respectively (those tend to
    be the protagonist + female lead in most novels)."""
    by_count = sorted(characters.items(), key=lambda kv: -kv[1]["speak_count"])

    # Rotate through gender pools by frequency rank.
    male_iter   = iter(MALE_VOICES)
    female_iter = iter(FEMALE_VOICES)

    for name, info in by_count:
        if info.get("voice"):
            continue
        if info["gender"] == "M":
            v = next(male_iter, None)
            if v: info["voice"] = v
        elif info["gender"] == "F":
            v = next(female_iter, None)
            if v: info["voice"] = v
        # else: leave unset; falls back to minor pool by hash at render time

    return characters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub", required=True, type=Path)
    ap.add_argument("--book-id", required=True)
    ap.add_argument("--out", type=Path, default=None,
                    help="output path; default: voice/cast_<book_id>.json")
    ap.add_argument("--min-count", type=int, default=2,
                    help="minimum tag occurrences to count as a character")
    args = ap.parse_args()

    out_path = args.out or (Path(__file__).parent / f"cast_{args.book_id}.json")

    print(f"[*] Scanning {args.epub.name}...")
    text = extract_full_text(args.epub)
    print(f"    extracted {len(text):,} chars")

    speakers = detect_speakers(text, min_count=args.min_count)
    print(f"[*] Found {len(speakers)} candidate characters (>= {args.min_count} dialogue tags)")

    characters: dict[str, dict] = {}
    for name, count in speakers.most_common():
        gender = infer_gender(text, name)
        characters[name] = {"gender": gender, "voice": None, "speak_count": count}
        print(f"    {name:20s}  {gender:8s}  tags={count}")

    characters = auto_assign_voices(characters)

    cast = {
        "narrator": NEUTRAL_VOICE,
        "minor_pool": ["eve", "ara", "leo", "rex"],   # used for unmapped speakers
        "characters": characters,
    }

    # Don't clobber a user-edited file — only write if absent.
    if out_path.exists():
        print(f"\n[!] {out_path.name} already exists — saving suggestion to {out_path.name}.suggested instead")
        out_path = out_path.with_suffix(".json.suggested")

    out_path.write_text(json.dumps(cast, indent=2), encoding="utf-8")
    print(f"\n[OK] Cast file: {out_path}")
    print("    Review and edit voice assignments / fix gender if wrong, then run render.")


if __name__ == "__main__":
    main()
