"""
Whisper-based STT diff for the multi-voice TTS pipeline.

For each chapter of a book, transcribe the rendered MP3 with faster-whisper
(word-level timestamps), bucket the transcribed words back into segments
using timing.json's per-segment [start, end] intervals, and report the
segments whose transcript diverges most from the source text.

Catches bug classes that pure duration anomaly misses:
  - Hallucinated phrases that ARE the right length but wrong content
  - Subtle word swaps / mispronunciations
  - Repeated phrases (transcript has duplication)
  - Skipped lines (transcript missing words)

Run:
    python qa_whisper.py library/danmachi_v13_mv
    python qa_whisper.py library/danmachi_v13_mv --model large-v3
    python qa_whisper.py library/danmachi_v13_mv --chapters 1,3 --top 30

Outputs:
    qa_whisper_<book_id>.json   full per-segment annotated report
    Console: top-N most divergent segments

Compute: faster-whisper large-v3 runs near realtime on a 12GB GPU. A 1h
chapter takes ~1-2 minutes. CPU fallback is ~10x slower; --model small or
medium are reasonable on CPU.
"""
from __future__ import annotations

import argparse
import json
import re
import string
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

# Windows console defaults to cp1252; transcripts contain zero-width spaces
# and other non-ASCII unicode. Reconfigure before any prints.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from faster_whisper import WhisperModel


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PUNCT_TBL = str.maketrans({c: " " for c in string.punctuation + "“”‘’—–"})


def clean_text(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", s)).strip()


def normalize_for_compare(s: str) -> list[str]:
    """Lowercase, strip punctuation/quotes/dashes, split into words. Used
    for both source and transcript before computing similarity so we don't
    penalize cosmetic differences like smart vs straight quotes."""
    s = s.lower().translate(_PUNCT_TBL)
    return [w for w in s.split() if w]


def load_chapter_segments(path: Path) -> tuple[int, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    m = re.search(r"chapter_(\d+)", path.name)
    chapter_num = int(m.group(1)) if m else 0
    segs = []
    for i, seg in enumerate(data.get("segments", [])):
        text = clean_text(seg.get("text", ""))
        if not text:
            continue
        segs.append({
            "chapter": chapter_num,
            "index": i,
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": text,
            "speaker": seg.get("speaker") or "?",
            "voice": seg.get("voice") or "?",
            "conf": seg.get("conf") or "?",
        })
    return chapter_num, segs


def transcribe_chapter(model: WhisperModel, mp3_path: Path) -> list[dict]:
    """Return a flat list of {word, start, end} for every word in the
    chapter, with timestamps in seconds."""
    segments, _info = model.transcribe(
        str(mp3_path),
        language="en",
        word_timestamps=True,
        vad_filter=False,
        beam_size=1,
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        for w in (seg.words or []):
            if w.word is None:
                continue
            words.append({
                "word": w.word.strip(),
                "start": float(w.start),
                "end": float(w.end),
            })
    return words


def bucket_words(words: list[dict], segments: list[dict]) -> dict[int, list[str]]:
    """Assign each Whisper word to the timing.json segment whose
    [start, end] window contains the word's midpoint. Returns a map of
    segment_index -> [words]."""
    out: dict[int, list[str]] = {s["index"]: [] for s in segments}
    # Sort segments by start; do a linear scan since both lists are sorted
    # by time.
    segs = sorted(segments, key=lambda s: s["start"])
    j = 0
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        while j < len(segs) - 1 and mid > segs[j]["end"]:
            j += 1
        # Snap to nearest segment by start if midpoint precedes the
        # first segment (transcript leading silence handling).
        out[segs[j]["index"]].append(w["word"])
    return out


def score_segment(seg: dict, transcript_words: list[str]) -> dict:
    src_words = normalize_for_compare(seg["text"])
    hyp_words = normalize_for_compare(" ".join(transcript_words))
    sm = SequenceMatcher(a=src_words, b=hyp_words, autojunk=False)
    similarity = sm.ratio()
    return {
        **seg,
        "src_words": len(src_words),
        "hyp_words": len(hyp_words),
        "similarity": similarity,
        "transcript": " ".join(transcript_words).strip(),
    }


def parse_chapter_list(spec: str | None, available: list[int]) -> list[int]:
    if not spec:
        return available
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(x for x in out if x in set(available))


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def print_row(s: dict) -> None:
    src = s["text"][:80].replace("\n", " ")
    hyp = s["transcript"][:80].replace("\n", " ") or "(empty)"
    print(f"  ch{s['chapter']:>2}  {fmt_time(s['start']):>7}  "
          f"voice={s['voice']:<4} {s['conf']:<4} "
          f"sim={s['similarity']:.2f}  "
          f"src({s['src_words']:>3}w): {src}")
    print(f"                                         "
          f"          hyp({s['hyp_words']:>3}w): {hyp}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_dir", type=Path)
    ap.add_argument("--model", default="large-v3",
                    help="faster-whisper model id (default large-v3). "
                         "Smaller: medium, small, base. Larger is slower but more accurate.")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--compute-type", default="float16",
                    help="float16 / int8_float16 / int8 / float32 (default float16)")
    ap.add_argument("--chapters", default=None,
                    help="comma-separated chapter list, e.g. '1,3,5-7'. Default: all.")
    ap.add_argument("--top", type=int, default=20,
                    help="how many worst segments to print")
    ap.add_argument("--min-words", type=int, default=3,
                    help="ignore source segments with fewer words than this")
    ap.add_argument("--out", type=Path, default=None,
                    help="output JSON path (default qa_whisper_<book_id>.json)")
    args = ap.parse_args()

    timing_files = sorted(args.book_dir.glob("chapter_*.timing.json"))
    if not timing_files:
        print(f"No chapter_*.timing.json in {args.book_dir}")
        raise SystemExit(1)

    by_chapter: dict[int, list[dict]] = {}
    for f in timing_files:
        ch_num, segs = load_chapter_segments(f)
        by_chapter[ch_num] = segs

    chapter_nums = parse_chapter_list(args.chapters, sorted(by_chapter))
    print(f"Book: {args.book_dir.name}")
    print(f"Chapters to process: {chapter_nums}")
    print(f"Loading {args.model} on {args.device}...")

    device = args.device
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"

    t0 = time.time()
    model = WhisperModel(args.model, device=device, compute_type=args.compute_type)
    print(f"Model loaded in {time.time()-t0:.1f}s on {device}")

    all_scored: list[dict] = []
    for ch in chapter_nums:
        mp3 = args.book_dir / f"chapter_{ch:03d}.mp3"
        if not mp3.exists():
            print(f"  ch{ch}: missing mp3, skipping")
            continue
        segs = by_chapter[ch]
        print(f"  ch{ch}: {len(segs)} segments, transcribing {mp3.name}...")
        t0 = time.time()
        words = transcribe_chapter(model, mp3)
        wall = time.time() - t0
        # Audio length from segment ends (avoids extra mp3 parsing).
        audio_len = max((s["end"] for s in segs), default=0)
        rt = wall / max(audio_len, 0.001)
        print(f"    {len(words)} words in {wall:.1f}s ({rt:.2f}x realtime)")
        buckets = bucket_words(words, segs)
        for seg in segs:
            scored = score_segment(seg, buckets[seg["index"]])
            all_scored.append(scored)

    if not all_scored:
        print("No segments scored.")
        return

    # Write JSON FIRST — transcription is expensive, so we never want a
    # printing bug to throw away the data.
    out_path = args.out or args.book_dir.parent.parent / f"qa_whisper_{args.book_dir.name}.json"
    out_path.write_text(json.dumps({
        "book": args.book_dir.name,
        "model": args.model,
        "device": device,
        "segments": all_scored,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nFull report saved to {out_path}")

    rankable = [s for s in all_scored if s["src_words"] >= args.min_words]
    rankable.sort(key=lambda s: s["similarity"])

    print(f"\n=== Top {args.top} most divergent (lowest similarity) ===")
    for s in rankable[:args.top]:
        try:
            print_row(s)
        except UnicodeEncodeError:
            # Last resort: drop unprintable chars rather than abort the run.
            safe = {**s, "text": s["text"].encode("ascii", "replace").decode("ascii"),
                    "transcript": s["transcript"].encode("ascii", "replace").decode("ascii")}
            print_row(safe)

    # Per-chapter aggregate stats.
    print("\n=== Per-chapter similarity summary (segments >=3w) ===")
    print("  chapter   n     mean    p10     p01     min")
    by_ch: dict[int, list[float]] = {}
    for s in rankable:
        by_ch.setdefault(s["chapter"], []).append(s["similarity"])
    for ch in sorted(by_ch):
        sims = sorted(by_ch[ch])
        n = len(sims)
        mean = sum(sims) / n
        p10 = sims[int(0.10 * (n - 1))]
        p01 = sims[int(0.01 * (n - 1))]
        print(f"  {ch:>4}    {n:>4}   {mean:>5.2f}   {p10:>5.2f}   {p01:>5.2f}   {sims[0]:>5.2f}")


if __name__ == "__main__":
    main()
