"""
Build demo audio clips from *Pride and Prejudice* (public domain, Project Gutenberg).

Renders three short passages, trimmed with ffmpeg, to show the pipeline end-to-end:

  opening.mp3   Chapter I  — the famous "It is a truth universally acknowledged..."
  lucas.mp3     Chapter VI — Elizabeth / Darcy / Bingley / Caroline at Lucas Lodge
  proposal.mp3  Chapter XXXIV — Darcy's first proposal

Cast is auto-detected by the same heuristic as cast_builder.py, with a few
major-character overrides so Elizabeth and Darcy get consistent voices.

Usage:
    python demos/build_demo.py --epub demos/pg1342.epub

Output files live next to this script. Each full chapter is rendered then
trimmed — the MP3 you ship is only the first ~30-60 s of audio.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from attribution import attribute                    # noqa: E402
from cast_builder import detect_speakers, infer_gender, auto_assign_voices  # noqa: E402
from html_to_marked_text import _walk                # noqa: E402
from render_batch import (                           # noqa: E402
    _FFMPEG, chunk_text, concat_audio_bytes, get_xai_key,
    resolve_voice, tts_chunk,
)
from text_preprocess import preprocess_for_tts       # noqa: E402


# (chapter_roman, out_name, max_chars_rendered, trim_seconds, description)
SAMPLES = [
    ("I",      "opening",  6000, 75, "Famous opening line + Mr./Mrs. Bennet dialogue"),
    ("VI",     "lucas",    6500, 60, "Elizabeth, Darcy and Bingley at Lucas Lodge"),
    ("XXXIV",  "proposal", 7000, 75, "Darcy's first proposal"),
]

MAJOR_OVERRIDES = {
    "Elizabeth":   {"gender": "F", "voice": "eve"},
    "Darcy":       {"gender": "M", "voice": "rex"},
    "Mr. Darcy":   {"gender": "M", "voice": "rex"},  # how Austen most often names him
    "Jane":        {"gender": "F", "voice": "ara"},
    "Bingley":     {"gender": "M", "voice": "leo"},
    "Mr. Bingley": {"gender": "M", "voice": "leo"},
    # Bennet parents — only the parents in this novel; daughters use given names.
    "Mr. Bennet":  {"gender": "M", "voice": "leo"},
    "Mrs. Bennet": {"gender": "F", "voice": "ara"},
}


def extract_book_text(epub_path: Path) -> str:
    """Concat all body xhtml, preserving italics as <emphasis>."""
    parts: list[str] = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            if any(s in name.lower() for s in ("cover", "toc", "wrap")):
                continue
            soup = BeautifulSoup(zf.read(name), "html.parser")
            for el in soup(["script", "style"]):
                el.decompose()
            body = soup.find("body") or soup
            parts.append(_walk(body))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def split_chapters(full_text: str) -> dict[str, str]:
    """Split on 'CHAPTER <ROMAN>.' headings, return {roman: chapter_text}."""
    chunks = re.split(r"(?i)CHAPTER\s+([IVXLCDM]+)\.?", full_text)
    # chunks = [pre-chapter-text, roman_1, body_1, roman_2, body_2, ...]
    chapters: dict[str, str] = {}
    for i in range(1, len(chunks) - 1, 2):
        roman = chunks[i].upper()
        body = chunks[i + 1].strip()
        if len(body) > 500:  # skip TOC entries
            chapters[roman] = body
    return chapters


def build_cast(full_text: str) -> dict:
    """Auto-detect characters + gender, then apply major-role overrides."""
    speakers = detect_speakers(full_text, min_count=2)
    characters: dict[str, dict] = {}
    for name, count in speakers.most_common(20):
        characters[name] = {
            "gender": infer_gender(full_text, name),
            "voice": None,
            "speak_count": count,
        }
    characters = auto_assign_voices(characters)
    # Lock in user's intent for the most-recognized characters.
    for name, info in MAJOR_OVERRIDES.items():
        if name in characters:
            characters[name].update(info)
        else:
            characters[name] = {**info, "speak_count": 0}
    return {
        "narrator": "sal",
        "minor_pool": ["eve", "ara", "leo", "rex"],
        "characters": characters,
    }


def render_segments_to_bytes(text: str, cast: dict, api_key: str,
                             parallel: int = 6) -> bytes:
    """Attribute -> voice-map -> parallel TTS -> ffmpeg concat -> return bytes."""
    segments = attribute(text, cast)

    flat: list[tuple[str, str]] = []
    for seg in segments:
        voice = resolve_voice(seg["speaker"], cast)
        for part in chunk_text(seg["text"]):
            if part.strip():
                flat.append((part, voice))

    results: list[bytes | None] = [None] * len(flat)
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(tts_chunk, t, v, api_key): i
                for i, (t, v) in enumerate(flat)}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            results[i] = fut.result()
            done += 1
            if done % 5 == 0 or done == len(flat):
                print(f"    {done}/{len(flat)}")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp = Path(f.name)
    concat_audio_bytes([b for b in results if b], tmp)
    return tmp


def trim(in_path: Path, out_path: Path, seconds: int) -> None:
    if not _FFMPEG:
        raise RuntimeError("ffmpeg not found; required to trim demo clips")
    # Re-encode rather than -c copy: guarantees the cut point is on a frame
    # boundary and the file doesn't play past the requested length.
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error",
         "-i", str(in_path), "-t", str(seconds),
         "-c:a", "libmp3lame", "-q:a", "4",
         str(out_path)],
        check=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub", type=Path,
                    default=Path(__file__).parent / "pg1342.epub")
    ap.add_argument("--parallel", type=int, default=6)
    args = ap.parse_args()

    if not args.epub.exists():
        sys.exit(f"EPUB not found: {args.epub}\n"
                 "Download Pride and Prejudice EPUB3 from "
                 "https://www.gutenberg.org/ebooks/1342.epub3.images")

    api_key = get_xai_key()
    out_dir = Path(__file__).parent
    pronunciations = ROOT / "examples" / "pronunciations.example.json"

    print(f"[*] Reading {args.epub.name}...")
    full_text = extract_book_text(args.epub)
    print(f"    {len(full_text):,} chars")

    print("[*] Splitting into chapters...")
    chapters = split_chapters(full_text)
    print(f"    {len(chapters)} chapters found")

    print("[*] Building cast...")
    cast = build_cast(full_text)
    print(f"    {len(cast['characters'])} characters "
          f"(majors locked: {', '.join(MAJOR_OVERRIDES)})")

    total_chars = 0
    for roman, name, max_chars, trim_seconds, desc in SAMPLES:
        if roman not in chapters:
            print(f"[!] Chapter {roman} not found — skipping")
            continue
        snippet = chapters[roman][:max_chars]
        snippet = preprocess_for_tts(snippet, pronunciations_path=pronunciations)
        total_chars += len(snippet)

        print(f"\n[*] Chapter {roman} -> {name}.mp3 ({desc})")
        print(f"    rendering {len(snippet):,} chars, trimming to {trim_seconds}s")
        tmp_full = render_segments_to_bytes(
            snippet, cast, api_key, parallel=args.parallel,
        )
        demo_path = out_dir / f"{name}.mp3"
        trim(tmp_full, demo_path, trim_seconds)
        tmp_full.unlink(missing_ok=True)
        print(f"    [OK] {demo_path.name} ({demo_path.stat().st_size:,} bytes)")

    cost = total_chars * 4.20 / 1_000_000
    print(f"\n[DONE] Estimated cost: ${cost:.2f} "
          f"(excluding already-cached chunks in tts_cache/)")


if __name__ == "__main__":
    main()
