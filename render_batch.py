"""
Batch render of EPUB chapters to MP3 + matching .txt, using Grok TTS.

- Groups EPUB files into logical chapters by leading number.
- Splits each chapter into ≤15K-char chunks.
- Caches each chunk by hash(text + voice_id) so re-runs skip paid work.
- Fires chunks in parallel (Grok allows 50 RPS / 100 concurrent).
- Concats chunks per chapter (naive byte concat — works for MP3, no ffmpeg needed).
- Output: library/<book_id>/chapter_NNN.mp3 and chapter_NNN.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from text_preprocess import preprocess_for_tts  # noqa: E402
from attribution import attribute, stats as attribution_stats  # noqa: E402
from html_to_marked_text import chapter_to_marked_text  # noqa: E402


VOICE_ROOT = Path(__file__).parent
LIBRARY_ROOT = VOICE_ROOT / "library"
CACHE_ROOT = VOICE_ROOT / "tts_cache"
CHUNK_LIMIT = 14_500  # leave headroom under Grok's 15K
GROK_URL = "https://api.x.ai/v1/tts"


# --- env / api key ---------------------------------------------------------

def get_xai_key() -> str:
    key = os.environ.get("XAI_API_KEY")
    if key:
        return key
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[System.Environment]::GetEnvironmentVariable('XAI_API_KEY','User')"],
                capture_output=True, text=True, timeout=5,
            )
            key = r.stdout.strip()
            if key:
                return key
        except Exception:
            pass
    raise RuntimeError("XAI_API_KEY not set in env or Windows user registry")


# --- chapter discovery ----------------------------------------------------

CHAPTER_FILE_RE = re.compile(r"chapter(\d+)", re.IGNORECASE)

# Labels treated as front/back matter and skipped when numbering chapters.
# These are wasted TTS spend (image-only or pure metadata).
_SKIP_LABEL_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"^cover\b", r"^insert\b", r"^title\s*page$", r"^frontispiece$",
        r"^copyright(s)?\b", r"^copyrights?\s+and\s+credits$",
        r"^table\s*of\s*contents( page)?$", r"^contents$",
        r"^jnovels?$", r"^information$", r"^character\s+gallery$",
        r"^(yen\s+)?newsletter\b",
    )
]


def _is_front_matter(label: str) -> bool:
    s = (label or "").strip()
    return any(p.match(s) for p in _SKIP_LABEL_PATTERNS)


def _resolve(base_path: str, href: str) -> str:
    """Resolve href relative to base_path (an EPUB-internal file path)."""
    href = href.split("#")[0]
    if not href:
        return ""
    base_dir = base_path.rsplit("/", 1)[0] if "/" in base_path else ""
    parts = (base_dir.split("/") if base_dir else []) + href.split("/")
    out: list[str] = []
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            if out:
                out.pop()
        else:
            out.append(p)
    return "/".join(out)


def _read_toc_entries(zf: zipfile.ZipFile, opf_soup: BeautifulSoup, opf_path: str
                      ) -> list[tuple[str, str]]:
    """Return [(label, file_path), ...] in TOC order. EPUB3 nav preferred, NCX fallback."""
    entries: list[tuple[str, str]] = []

    # EPUB3 nav.xhtml
    for item in opf_soup.find_all("item"):
        if "nav" in (item.get("properties") or "").split():
            nav_path = _resolve(opf_path, item.get("href", ""))
            try:
                nav_soup = BeautifulSoup(zf.read(nav_path), "html.parser")
            except KeyError:
                continue
            toc_nav = (nav_soup.find("nav", attrs={"epub:type": "toc"})
                       or nav_soup.find("nav"))
            if toc_nav:
                for a in toc_nav.find_all("a"):
                    href = a.get("href", "")
                    if not href:
                        continue
                    target = _resolve(nav_path, href)
                    label = " ".join(a.get_text().split()).strip()
                    if target:
                        entries.append((label or target, target))
            if entries:
                return entries

    # EPUB2 NCX
    for item in opf_soup.find_all("item"):
        if item.get("media-type") == "application/x-dtbncx+xml":
            ncx_path = _resolve(opf_path, item.get("href", ""))
            try:
                ncx_soup = BeautifulSoup(zf.read(ncx_path), "xml")
            except KeyError:
                continue
            for np in ncx_soup.find_all("navPoint"):
                content = np.find("content")
                text_el = np.find("navLabel")
                text_el = text_el.find("text") if text_el else None
                if not content:
                    continue
                src = content.get("src", "")
                if not src:
                    continue
                target = _resolve(ncx_path, src)
                label = " ".join(text_el.get_text().split()).strip() if text_el else target
                if target:
                    entries.append((label or target, target))
            if entries:
                return entries

    return entries


def _spine_order(opf_soup: BeautifulSoup, opf_path: str) -> list[str]:
    """Return list of file paths in spine order."""
    manifest: dict[str, str] = {}
    for item in opf_soup.find_all("item"):
        item_id = item.get("id", "")
        if item_id:
            manifest[item_id] = _resolve(opf_path, item.get("href", ""))
    spine: list[str] = []
    for ref in opf_soup.find_all("itemref"):
        idref = ref.get("idref", "")
        if idref in manifest and manifest[idref]:
            spine.append(manifest[idref])
    return spine


def _discover_via_toc(epub_path: Path, *, skip_front_matter: bool = True
                      ) -> tuple[dict[int, list[str]], dict[int, str]]:
    """Use EPUB TOC + spine to map chapters → files. Returns (chapters, labels).

    When skip_front_matter is True (default), TOC entries matching
    _SKIP_LABEL_PATTERNS (Cover, Title Page, Copyright, TOC, Newsletter, …)
    are dropped from the chapter numbering so chapter 1 = first real content.
    Set False to recover the raw 1:1 TOC numbering (used by migration)."""
    chapters: dict[int, list[str]] = {}
    labels: dict[int, str] = {}
    with zipfile.ZipFile(epub_path) as zf:
        try:
            container = BeautifulSoup(zf.read("META-INF/container.xml"), "xml")
            opf_path = container.find("rootfile")["full-path"]
        except (KeyError, TypeError):
            return chapters, labels
        opf_soup = BeautifulSoup(zf.read(opf_path), "xml")

        toc_entries = _read_toc_entries(zf, opf_soup, opf_path)
        spine = _spine_order(opf_soup, opf_path)
        if not toc_entries or not spine:
            return chapters, labels

        # Dedup TOC by file (keep first label per unique file).
        first_label: dict[str, str] = {}
        toc_order: list[str] = []
        for label, target in toc_entries:
            if target not in first_label:
                first_label[target] = label
                toc_order.append(target)

        # Walk spine; bump chapter number when we cross a TOC-start file.
        chapter_num = 0
        toc_set = set(toc_order)
        in_skip = False
        for spine_file in spine:
            if spine_file in toc_set:
                if skip_front_matter and _is_front_matter(first_label[spine_file]):
                    in_skip = True
                else:
                    in_skip = False
                    chapter_num += 1
                    chapters[chapter_num] = [spine_file]
                    labels[chapter_num] = first_label[spine_file]
            elif chapter_num > 0 and not in_skip:
                chapters[chapter_num].append(spine_file)
            # else: pre-TOC spine files (rare) are skipped.

    return chapters, labels


def _discover_via_filename(epub_path: Path) -> dict[int, list[str]]:
    """Fallback: match filenames containing 'chapterNNN'."""
    chapters: dict[int, list[str]] = {}
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            m = CHAPTER_FILE_RE.search(name)
            if not m:
                continue
            n = int(m.group(1))
            chapters.setdefault(n, []).append(name)
    return chapters


def discover_chapters(epub_path: Path) -> dict[int, list[str]]:
    """Return {chapter_number: [xhtml_filenames in spine order]}.

    Reads the EPUB TOC (nav.xhtml or NCX) and walks the spine to group
    files into chapters. Falls back to filename matching ('chapterNNN')
    for EPUBs without a usable TOC.
    """
    chapters, _ = _discover_via_toc(epub_path)
    if chapters:
        return chapters
    return _discover_via_filename(epub_path)


def discover_chapter_labels(epub_path: Path) -> dict[int, str]:
    """Return {chapter_number: label} when TOC-based discovery is available."""
    _, labels = _discover_via_toc(epub_path)
    return labels


def extract_chapter_text(epub_path: Path, file_names: list[str]) -> str:
    """Concatenate text from multiple xhtml files. Skip parts under 500 chars
    (those are usually just title page boilerplate)."""
    parts = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in file_names:
            soup = BeautifulSoup(zf.read(name), "html.parser")
            for el in soup(["script", "style"]):
                el.decompose()
            text = re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()
            if len(text) >= 500:
                parts.append(text)
    return " ".join(parts)


# --- chunking -------------------------------------------------------------

def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split on sentence boundaries to stay <= limit chars per chunk."""
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, buf = [], ""
    for s in sentences:
        if len(buf) + len(s) + 1 > limit and buf:
            chunks.append(buf.strip())
            buf = s
        else:
            buf = (buf + " " + s) if buf else s
    if buf:
        chunks.append(buf.strip())
    return chunks


# --- TTS with hash cache --------------------------------------------------

def chunk_hash(text: str, voice_id: str) -> str:
    return hashlib.sha256(f"{voice_id}\n{text}".encode("utf-8")).hexdigest()[:16]


def tts_chunk(text: str, voice_id: str, api_key: str) -> bytes:
    """Render one chunk. Hits cache if already rendered."""
    CACHE_ROOT.mkdir(exist_ok=True)
    cache_path = CACHE_ROOT / f"{chunk_hash(text, voice_id)}.mp3"
    if cache_path.exists():
        return cache_path.read_bytes()

    resp = requests.post(
        GROK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"text": text, "voice_id": voice_id, "language": "en"},
        timeout=300,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Grok TTS {resp.status_code}: {resp.text[:300]}")
    cache_path.write_bytes(resp.content)
    return resp.content


# --- per-chapter render ---------------------------------------------------

# --- ffmpeg discovery + concat -------------------------------------------

def find_ffmpeg() -> str | None:
    """Locate ffmpeg: PATH first, then known winget install location."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    # winget default install path
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget.exists():
        for ff in winget.glob("Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe"):
            return str(ff)
    return None


_FFMPEG = find_ffmpeg()


def concat_audio_bytes(parts: list[bytes], out_path: Path) -> None:
    """Concatenate MP3 byte chunks. Uses ffmpeg if available (clean seams),
    else naive byte concat (works but may click between segments)."""
    if not parts:
        out_path.write_bytes(b"")
        return
    if len(parts) == 1:
        out_path.write_bytes(parts[0])
        return
    if _FFMPEG:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            list_file = td_path / "list.txt"
            with open(list_file, "w", encoding="utf-8") as f:
                for i, b in enumerate(parts):
                    p = td_path / f"seg_{i:05d}.mp3"
                    p.write_bytes(b)
                    f.write(f"file '{p.as_posix()}'\n")
            subprocess.run(
                [_FFMPEG, "-y", "-loglevel", "error",
                 "-f", "concat", "-safe", "0",
                 "-i", str(list_file), "-c", "copy", str(out_path)],
                check=True,
            )
    else:
        out_path.write_bytes(b"".join(parts))


# --- voice resolution -----------------------------------------------------

_FEMALE_VOICES = {"eve", "ara"}
_MALE_VOICES   = {"rex", "leo"}


def resolve_voice(speaker: str, cast: dict) -> str:
    """Map a speaker name to a Grok voice_id using the cast file.
    Unknowns and '?' fall back to narrator voice (safest).
    Minor characters get a hashed pool assignment matched to their gender,
    so a female minor character never gets a male voice and vice versa."""
    if speaker in ("narrator", "?", "unknown"):
        return cast.get("narrator", "sal")
    info = cast.get("characters", {}).get(speaker)
    if info and info.get("voice"):
        return info["voice"]
    # Build a gender-matched pool from the minor_pool list.
    pool_full = cast.get("minor_pool", ["sal"])
    gender = (info or {}).get("gender", "unknown")
    if gender == "F":
        pool = [v for v in pool_full if v in _FEMALE_VOICES] or pool_full
    elif gender == "M":
        pool = [v for v in pool_full if v in _MALE_VOICES] or pool_full
    else:
        pool = pool_full
    if not pool:
        return cast.get("narrator", "sal")
    h = int(hashlib.md5(speaker.encode()).hexdigest(), 16)
    return pool[h % len(pool)]


# --- multi-voice render ---------------------------------------------------

def render_chapter_multi_voice(
    chapter_num: int,
    text: str,
    cast: dict,
    out_dir: Path,
    api_key: str,
    pronunciations_path: Path | None,
    parallel: int = 4,
) -> tuple[Path, int]:
    """Attribute, voice-map, render each segment, concat. Saves
    chapter_NNN.attribution.json next to the audio for inspection."""
    # Pass the full cast so attribute() can do gender-based pronoun resolution.
    segments = attribute(text, cast)

    s = attribution_stats(segments)
    print(f"[ch{chapter_num:03d}] {s['total']} segments  conf={s['by_conf']}")

    # Build flat list of (text_to_speak, voice_id, speaker, conf) — splitting
    # any oversized segments on sentence boundaries.
    flat: list[tuple[str, str, str, str]] = []
    for seg in segments:
        voice = resolve_voice(seg["speaker"], cast)
        text_pp = preprocess_for_tts(seg["text"], pronunciations_path=pronunciations_path)
        for part in chunk_text(text_pp):
            if part.strip():
                flat.append((part, voice, seg["speaker"], seg["conf"]))

    print(f"[ch{chapter_num:03d}] -> {len(flat)} render calls "
          f"({sum(len(p[0]) for p in flat):,} chars)")

    # Save attribution manifest for inspection / future LLM tuning
    manifest_path = out_dir / f"chapter_{chapter_num:03d}.attribution.json"
    manifest_path.write_text(
        json.dumps([
            {"speaker": s, "voice": v, "conf": c, "text": t[:200]}
            for (t, v, s, c) in flat
        ], indent=2),
        encoding="utf-8",
    )

    # Parallel TTS render (cached per-segment by hash)
    results: list[bytes | None] = [None] * len(flat)
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(tts_chunk, t, v, api_key): i
                for i, (t, v, _, _) in enumerate(flat)}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            results[i] = fut.result()
            done += 1
            if done % 10 == 0 or done == len(flat):
                print(f"[ch{chapter_num:03d}]   {done}/{len(flat)}")

    out_path = out_dir / f"chapter_{chapter_num:03d}.mp3"
    concat_audio_bytes([b for b in results if b], out_path)
    return out_path, sum(len(p[0]) for p in flat)


# --- single-voice render -------------------------------------------------

def render_chapter(
    chapter_num: int,
    chunks: list[str],
    voice_id: str,
    out_dir: Path,
    api_key: str,
    parallel: int = 4,
) -> tuple[Path, int]:
    """Render all chunks for a chapter, concat, write chapter_NNN.mp3.
    Returns (output_path, total_chars)."""
    out_path = out_dir / f"chapter_{chapter_num:03d}.mp3"
    print(f"[ch{chapter_num:03d}] {len(chunks)} chunk(s), {sum(len(c) for c in chunks)} chars")

    # Parallel render. Order preserved via index.
    results: list[bytes | None] = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(tts_chunk, c, voice_id, api_key): i for i, c in enumerate(chunks)}
        for fut in as_completed(futs):
            i = futs[fut]
            results[i] = fut.result()
            print(f"[ch{chapter_num:03d}]   chunk {i+1}/{len(chunks)} done ({len(results[i])} bytes)")

    # Concat (uses ffmpeg if installed for cleaner seams).
    concat_audio_bytes([b for b in results if b], out_path)  # type: ignore[arg-type]
    return out_path, sum(len(c) for c in chunks)


# --- main -----------------------------------------------------------------

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render EPUB chapters to MP3 via Grok TTS.")
    ap.add_argument("--epub", required=True, type=Path, help="path to .epub")
    ap.add_argument("--book-id", required=True, help="folder name in library/ (e.g. pride_prejudice)")
    ap.add_argument("--chapters", required=True, help="chapter range, e.g. '1-2' or '3'")
    ap.add_argument("--voice", default="rex", help="voice_id (eve/ara/rex/sal/leo)")
    ap.add_argument("--pronunciations", type=Path, default=None,
                    help="optional JSON dict for word replacements (omit for none)")
    ap.add_argument("--parallel", type=int, default=4, help="concurrent TTS calls")
    ap.add_argument("--multi-voice", action="store_true",
                    help="enable multi-voice rendering (requires --cast)")
    ap.add_argument("--cast", type=Path, default=None,
                    help="cast JSON file (built by cast_builder.py)")
    args = ap.parse_args()

    api_key = get_xai_key()
    out_dir = LIBRARY_ROOT / args.book_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Persist book metadata so the player can find the source EPUB for rich HTML.
    book_meta_path = out_dir / "book.json"
    book_meta = {}
    if book_meta_path.exists():
        try:
            book_meta = json.loads(book_meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    book_meta.update({
        "id": args.book_id,
        "source_epub": str(args.epub.resolve()),
        "voice": args.voice,
    })
    book_meta_path.write_text(json.dumps(book_meta, indent=2), encoding="utf-8")

    if "-" in args.chapters:
        a, b = args.chapters.split("-")
        wanted = list(range(int(a), int(b) + 1))
    else:
        wanted = [int(args.chapters)]

    all_chapters = discover_chapters(args.epub)
    chapter_labels = discover_chapter_labels(args.epub)
    if chapter_labels:
        print(f"[*] EPUB has {len(all_chapters)} chapters:")
        for n in sorted(all_chapters):
            print(f"      {n:3d}. {chapter_labels.get(n, '(no label)')}")
    else:
        print(f"[*] EPUB has chapters: {sorted(all_chapters)}")
    print(f"[*] Rendering: {wanted}, voice={args.voice}, book_id={args.book_id}")

    cast = None
    if args.multi_voice:
        if not args.cast or not args.cast.exists():
            sys.exit("--multi-voice requires --cast pointing to an existing cast JSON")
        cast = json.loads(args.cast.read_text(encoding="utf-8"))
        print(f"[*] Multi-voice mode. Cast has {len(cast.get('characters', {}))} characters.")
        print(f"[*] ffmpeg: {_FFMPEG or 'NOT FOUND (using naive concat)'}")

    total_chars = 0
    for n in wanted:
        if n not in all_chapters:
            print(f"[!] chapter {n} not in EPUB — skipping")
            continue

        if args.multi_voice:
            # Use HTML extraction so italics survive as <emphasis> tags.
            text = chapter_to_marked_text(args.epub, all_chapters[n])
            txt_path = out_dir / f"chapter_{n:03d}.txt"
            txt_path.write_text(text, encoding="utf-8")
            _, chars = render_chapter_multi_voice(
                n, text, cast, out_dir, api_key,
                pronunciations_path=args.pronunciations,
                parallel=args.parallel,
            )
        else:
            text = extract_chapter_text(args.epub, all_chapters[n])
            text = preprocess_for_tts(text, pronunciations_path=args.pronunciations)
            chunks = chunk_text(text)
            txt_path = out_dir / f"chapter_{n:03d}.txt"
            txt_path.write_text(text, encoding="utf-8")
            _, chars = render_chapter(n, chunks, args.voice, out_dir, api_key,
                                      parallel=args.parallel)
        total_chars += chars

    cost = total_chars * 4.20 / 1_000_000
    print(f"\n[DONE] Total chars: {total_chars}, est. cost (excluding cache hits): ${cost:.2f}")
    print(f"[*] Output: {out_dir}")


if __name__ == "__main__":
    main()
