"""
Duration-anomaly QA pre-filter for the multi-voice TTS pipeline.

Reads a book's `chapter_NNN.timing.json` files, learns each voice's typical
speaking rate from the data itself (median seconds-per-character on
medium-length segments), and reports segments whose actual duration
deviates most from what the text length would predict.

Two main bug classes get caught cheaply:
  - Too long  -> hallucination, stutter, repeated phrase
  - Too short -> swallowed words, cut-off line

Run:
    python qa_audio.py library/danmachi_v13_mv
    python qa_audio.py library/danmachi_v13_mv --top 30
    python qa_audio.py library/danmachi_v13_mv --json qa_v13.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CHAPTER_NUM_RE = re.compile(r"chapter_(\d+)")


def clean_text(s: str) -> str:
    """Drop expressive markup (<emphasis>, <whisper>, etc.) and normalize
    whitespace so the char count reflects what's actually being spoken."""
    return _WS_RE.sub(" ", _TAG_RE.sub("", s)).strip()


def load_chapter(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    m = _CHAPTER_NUM_RE.search(path.name)
    chapter_num = int(m.group(1)) if m else 0
    out: list[dict] = []
    for i, seg in enumerate(data.get("segments", [])):
        text = clean_text(seg.get("text", ""))
        if not text:
            continue
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
        out.append({
            "chapter": chapter_num,
            "index": i,
            "start": start,
            "end": end,
            "duration": max(0.0, end - start),
            "text": text,
            "speaker": seg.get("speaker") or "?",
            "voice": seg.get("voice") or "?",
            "conf": seg.get("conf") or "?",
            "chars": len(text),
        })
    return out


def per_voice_rate(segments: list[dict]) -> dict[str, float]:
    """Median seconds-per-character per voice, learned from segments long
    enough to be stable (≥ 30 chars and ≥ 1 s of audio). Short segments
    are dominated by leading / trailing silence and would skew the rate."""
    by_voice: dict[str, list[float]] = {}
    for s in segments:
        if s["chars"] < 30 or s["duration"] < 1.0:
            continue
        by_voice.setdefault(s["voice"], []).append(s["duration"] / s["chars"])
    return {v: statistics.median(rs) for v, rs in by_voice.items() if rs}


def score_segments(segments: list[dict], voice_rates: dict[str, float]) -> None:
    """Annotate each segment with `expected`, `ratio`, `abs_dev`."""
    fallback = (statistics.median(voice_rates.values())
                if voice_rates else 0.06)   # ~17 cps if no data at all
    for s in segments:
        rate = voice_rates.get(s["voice"], fallback)
        # Floor the expected duration at 0.4 s so single-word segments don't
        # produce massive ratios from rounding noise alone.
        expected = max(0.4, s["chars"] * rate)
        ratio = s["duration"] / expected
        s["expected"] = expected
        s["ratio"] = ratio
        s["abs_dev"] = abs(ratio - 1.0)


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def print_row(s: dict) -> None:
    text = s["text"][:90].replace("\n", " ")
    print(f"  ch{s['chapter']:>2}  {fmt_time(s['start']):>7}  "
          f"voice={s['voice']:<4} {s['conf']:<4} "
          f"ratio={s['ratio']:>5.2f}  "
          f"{s['duration']:>5.1f}s vs ~{s['expected']:>4.1f}s  "
          f"[{s['chars']:>3}c] {text}")


def chapter_summary(segments: list[dict]) -> None:
    """Print per-chapter aggregate ratio stats. Books with one bad chapter
    show up here even if individual segments are unremarkable."""
    by_ch: dict[int, list[float]] = {}
    for s in segments:
        if s["chars"] >= 10:
            by_ch.setdefault(s["chapter"], []).append(s["ratio"])
    if not by_ch:
        return
    print("\n=== Per-chapter ratio summary (segments >=10c) ===")
    print("  chapter   n    median    p90    p99     max")
    for ch in sorted(by_ch):
        ratios = sorted(by_ch[ch])
        n = len(ratios)
        med = statistics.median(ratios)
        p90 = ratios[int(0.90 * (n - 1))]
        p99 = ratios[int(0.99 * (n - 1))]
        mx = ratios[-1]
        print(f"  {ch:>4}   {n:>4}   {med:>5.2f}   {p90:>5.2f}   {p99:>5.2f}   {mx:>5.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_dir", type=Path, help="path to library/<book_id>")
    ap.add_argument("--top", type=int, default=20,
                    help="how many worst segments to print per category (default 20)")
    ap.add_argument("--min-chars", type=int, default=4,
                    help="ignore segments shorter than this for ranking (default 4)")
    ap.add_argument("--json", type=Path,
                    help="also write the full annotated segment list as JSON")
    args = ap.parse_args()

    timing_files = sorted(args.book_dir.glob("chapter_*.timing.json"))
    if not timing_files:
        print(f"No chapter_*.timing.json in {args.book_dir}")
        raise SystemExit(1)

    all_segments: list[dict] = []
    for f in timing_files:
        all_segments.extend(load_chapter(f))

    voice_rates = per_voice_rate(all_segments)
    print(f"Book: {args.book_dir.name}")
    print(f"Chapters: {len(timing_files)}, segments: {len(all_segments)}")
    if voice_rates:
        print("Learned speaking rates:")
        for v in sorted(voice_rates):
            print(f"  {v}: {1/voice_rates[v]:.1f} chars/sec  ({voice_rates[v]*1000:.0f} ms/char)")
    else:
        print("Warning: no segments long enough to learn rates; using fallback 17 cps")

    score_segments(all_segments, voice_rates)
    rankable = [s for s in all_segments if s["chars"] >= args.min_chars]

    print(f"\n=== Top {args.top} too-LONG (hallucination / stutter / repeat) ===")
    for s in sorted(rankable, key=lambda s: s["ratio"], reverse=True)[:args.top]:
        print_row(s)

    print(f"\n=== Top {args.top} too-SHORT (swallowed / cut-off) ===")
    for s in sorted(rankable, key=lambda s: s["ratio"])[:args.top]:
        print_row(s)

    chapter_summary(all_segments)

    if args.json:
        all_segments.sort(key=lambda s: (s["chapter"], s["start"]))
        args.json.write_text(json.dumps({
            "book": args.book_dir.name,
            "voice_rates_chars_per_sec": {v: 1 / r for v, r in voice_rates.items()},
            "segments": all_segments,
        }, indent=2), encoding="utf-8")
        print(f"\nFull report written to {args.json}")


if __name__ == "__main__":
    main()
