"""
Minimal audiobook player. Flask backend serves a single-page web UI.

Library convention: each book is a folder under LIBRARY_DIR with chapter_NNN.mp3
files (and optional matching chapter_NNN.txt). Per-book progress is stored in
.progress.json inside each book folder.

Run:
    pip install flask
    python app.py
    # open http://localhost:5000

Move LIBRARY_DIR if you want to point it at a different folder.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory


LIBRARY_DIR = Path(__file__).parent.parent / "library"
STATIC_DIR = Path(__file__).parent / "static"

CHAPTER_FILE_RE = re.compile(r"chapter(\d+)", re.IGNORECASE)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


def _find_ffprobe() -> str | None:
    """Locate ffprobe: PATH first, then known winget install location."""
    p = shutil.which("ffprobe")
    if p:
        return p
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget.exists():
        for ff in winget.glob("Gyan.FFmpeg_*/ffmpeg-*/bin/ffprobe.exe"):
            return str(ff)
    return None


_FFPROBE = _find_ffprobe()


def _probe_duration(mp3: Path) -> float:
    """Run ffprobe to get the duration of an MP3 in seconds. Returns 0.0 on failure."""
    if not _FFPROBE:
        return 0.0
    try:
        out = subprocess.run(
            [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(mp3)],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0.0


def chapter_durations(book_dir: Path) -> dict[int, float]:
    """Return {chapter_num: seconds} for every chapter MP3 in the book.
    Cached in `.durations.json`, keyed by mtime+size so re-renders bust the cache."""
    cache_path = book_dir / ".durations.json"
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    out: dict[int, float] = {}
    dirty = False
    for mp3 in sorted(book_dir.glob("chapter_*.mp3")):
        m = re.match(r"chapter_(\d+)\.mp3", mp3.name)
        if not m:
            continue
        n = int(m.group(1))
        try:
            st = mp3.stat()
        except OSError:
            continue
        key = str(n)
        entry = cache.get(key) or {}
        if (entry.get("size") == st.st_size
                and entry.get("mtime") == int(st.st_mtime)
                and "duration" in entry):
            out[n] = float(entry["duration"])
        else:
            dur = _probe_duration(mp3)
            cache[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "duration": dur}
            out[n] = dur
            dirty = True
    if dirty:
        try:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return out


# --- helpers --------------------------------------------------------------

def safe_book_dir(book_id: str) -> Path:
    """Resolve book_id to a directory inside LIBRARY_DIR, refuse traversal."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", book_id):
        abort(400, "bad book_id")
    p = (LIBRARY_DIR / book_id).resolve()
    if not p.is_dir() or LIBRARY_DIR.resolve() not in p.parents and p != LIBRARY_DIR.resolve():
        abort(404)
    return p


def list_chapters(book_dir: Path) -> list[dict]:
    """Return [{'num': 1, 'mp3': 'chapter_001.mp3', 'size': 12345,
                'duration_seconds': 612.4}, ...]. Durations cached on disk."""
    durations = chapter_durations(book_dir)
    chapters = []
    for mp3 in sorted(book_dir.glob("chapter_*.mp3")):
        m = re.match(r"chapter_(\d+)\.mp3", mp3.name)
        if not m:
            continue
        n = int(m.group(1))
        chapters.append({
            "num": n,
            "mp3": mp3.name,
            "size": mp3.stat().st_size,
            "has_text": (book_dir / f"chapter_{n:03d}.txt").exists(),
            "duration_seconds": durations.get(n, 0.0),
        })
    return chapters


def progress_path(book_dir: Path) -> Path:
    return book_dir / ".progress.json"


def bookmarks_path(book_dir: Path) -> Path:
    return book_dir / ".bookmarks.json"


def read_progress(book_dir: Path) -> dict:
    p = progress_path(book_dir)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d.setdefault("manually_finished", False)
            return d
        except Exception:
            pass
    return {"chapter": None, "time": 0.0, "speed": 1.0, "manually_finished": False}


def write_progress(book_dir: Path, data: dict) -> None:
    progress_path(book_dir).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def last_interacted(book_dir: Path) -> float:
    """Most recent of: book.json (added/rendered), .progress.json (read).
    Used to sort the library so the most recently-touched book is first."""
    candidates = [book_dir / "book.json", progress_path(book_dir)]
    mtimes = []
    for p in candidates:
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:
            pass
    if not mtimes:
        try:
            mtimes.append(book_dir.stat().st_mtime)
        except OSError:
            return 0.0
    return max(mtimes)


def read_bookmarks(book_dir: Path) -> list[dict]:
    p = bookmarks_path(book_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def write_bookmarks(book_dir: Path, data: list[dict]) -> None:
    bookmarks_path(book_dir).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


# --- EPUB rich content ----------------------------------------------------

def book_meta(book_dir: Path) -> dict:
    p = book_dir / "book.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# --- chapter discovery (mirrors render_batch.discover_chapters) ----------
# Kept duplicated here so player/app.py stays standalone (no parent-dir import).
# If this drifts from render_batch.py, MP3s and player content will misalign.

def _resolve_internal(base_path: str, href: str) -> str:
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
            if out: out.pop()
        else:
            out.append(p)
    return "/".join(out)


def _toc_targets_in_order(zf: zipfile.ZipFile, opf_soup: BeautifulSoup,
                          opf_path: str) -> list[str]:
    """Return list of file paths (TOC order). EPUB3 nav preferred, NCX fallback."""
    out: list[str] = []
    for item in opf_soup.find_all("item"):
        if "nav" in (item.get("properties") or "").split():
            nav_path = _resolve_internal(opf_path, item.get("href", ""))
            try:
                nav_soup = BeautifulSoup(zf.read(nav_path), "html.parser")
            except KeyError:
                continue
            toc_nav = (nav_soup.find("nav", attrs={"epub:type": "toc"})
                       or nav_soup.find("nav"))
            if toc_nav:
                for a in toc_nav.find_all("a"):
                    target = _resolve_internal(nav_path, a.get("href", ""))
                    if target:
                        out.append(target)
            if out:
                return out
    for item in opf_soup.find_all("item"):
        if item.get("media-type") == "application/x-dtbncx+xml":
            ncx_path = _resolve_internal(opf_path, item.get("href", ""))
            try:
                ncx_soup = BeautifulSoup(zf.read(ncx_path), "xml")
            except KeyError:
                continue
            for np in ncx_soup.find_all("navPoint"):
                content = np.find("content")
                if not content:
                    continue
                target = _resolve_internal(ncx_path, content.get("src", ""))
                if target:
                    out.append(target)
            if out:
                return out
    return out


def _spine_files(opf_soup: BeautifulSoup, opf_path: str) -> list[str]:
    manifest: dict[str, str] = {}
    for item in opf_soup.find_all("item"):
        item_id = item.get("id", "")
        if item_id:
            manifest[item_id] = _resolve_internal(opf_path, item.get("href", ""))
    out: list[str] = []
    for ref in opf_soup.find_all("itemref"):
        idref = ref.get("idref", "")
        if idref in manifest and manifest[idref]:
            out.append(manifest[idref])
    return out


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
    """Return True for TOC entries that are pure metadata / image-only and
    shouldn't count toward chapter numbering. Must match render_batch.py."""
    s = (label or "").strip()
    return any(p.match(s) for p in _SKIP_LABEL_PATTERNS)


def discover_chapters_in_epub(epub_path: Path, *, skip_front_matter: bool = True
                              ) -> dict[int, list[str]]:
    """{chapter_num: [files]} matching render_batch.discover_chapters output.
    Pass skip_front_matter=False to get the raw TOC numbering (used by
    the migration that maps old MP3 numbers to new ones)."""
    chapters: dict[int, list[str]] = {}
    with zipfile.ZipFile(epub_path) as zf:
        opf_rel = _opf_path(zf)
        if not opf_rel:
            return _filename_fallback_chapters(zf)
        opf_soup = BeautifulSoup(zf.read(opf_rel), "xml")
        # Need (label, target) pairs for the front-matter filter.
        toc_entries_with_labels: list[tuple[str, str]] = []
        for item in opf_soup.find_all("item"):
            if "nav" in (item.get("properties") or "").split():
                nav_path = _resolve_internal(opf_rel, item.get("href", ""))
                try:
                    nav_soup = BeautifulSoup(zf.read(nav_path), "html.parser")
                except KeyError:
                    continue
                toc_nav = (nav_soup.find("nav", attrs={"epub:type": "toc"})
                           or nav_soup.find("nav"))
                if toc_nav:
                    for a in toc_nav.find_all("a"):
                        target = _resolve_internal(nav_path, a.get("href", ""))
                        label = " ".join(a.get_text().split()).strip()
                        if target:
                            toc_entries_with_labels.append((label or target, target))
                if toc_entries_with_labels:
                    break
        if not toc_entries_with_labels:
            for item in opf_soup.find_all("item"):
                if item.get("media-type") == "application/x-dtbncx+xml":
                    ncx_path = _resolve_internal(opf_rel, item.get("href", ""))
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
                        target = _resolve_internal(ncx_path, content.get("src", ""))
                        label = " ".join(text_el.get_text().split()).strip() if text_el else target
                        if target:
                            toc_entries_with_labels.append((label or target, target))
                    if toc_entries_with_labels:
                        break
        spine = _spine_files(opf_soup, opf_rel)
        if not toc_entries_with_labels or not spine:
            return _filename_fallback_chapters(zf)
        first_label: dict[str, str] = {}
        toc_order: list[str] = []
        for label, target in toc_entries_with_labels:
            if target not in first_label:
                first_label[target] = label
                toc_order.append(target)
        toc_set = set(toc_order)
        chapter_num = 0
        in_skip = False
        for spine_file in spine:
            if spine_file in toc_set:
                if skip_front_matter and _is_front_matter(first_label[spine_file]):
                    in_skip = True
                else:
                    in_skip = False
                    chapter_num += 1
                    chapters[chapter_num] = [spine_file]
            elif chapter_num > 0 and not in_skip:
                chapters[chapter_num].append(spine_file)
    return chapters


def _filename_fallback_chapters(zf: zipfile.ZipFile) -> dict[int, list[str]]:
    chapters: dict[int, list[str]] = {}
    for name in sorted(zf.namelist()):
        if not name.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        m = CHAPTER_FILE_RE.search(name)
        if not m:
            continue
        chapters.setdefault(int(m.group(1)), []).append(name)
    return chapters


def epub_chapter_files(epub_path: Path, chapter_num: int) -> list[str]:
    """Files belonging to a chapter, using the same TOC-aware discovery
    as render_batch.py. Critical for player↔render alignment."""
    return discover_chapters_in_epub(epub_path).get(chapter_num, [])


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Pure-Python (w, h) reader for JPEG / PNG / GIF / WEBP. Returns None
    for unrecognized formats. Used to filter out tiny dinkus / scene-break
    icons from picture mode without pulling in PIL."""
    n = len(data)
    if n < 24:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return (w, h)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w = int.from_bytes(data[6:8], "little")
        h = int.from_bytes(data[8:10], "little")
        return (w, h)
    if data[:2] == b"\xff\xd8":
        # JPEG: walk segments to find SOF0/SOF2 (start-of-frame).
        i = 2
        while i + 9 < n:
            if data[i] != 0xff:
                return None
            marker = data[i + 1]
            # SOF markers are 0xC0..0xCF except DHT(C4), JPG(C8), DAC(CC).
            if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h)
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            i += 2 + seg_len
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 " and n >= 30:
            w = int.from_bytes(data[26:28], "little") & 0x3fff
            h = int.from_bytes(data[28:30], "little") & 0x3fff
            return (w, h)
        if chunk == b"VP8L" and n >= 25:
            b1, b2, b3, b4 = data[21:25]
            w = ((b2 & 0x3f) << 8 | b1) + 1
            h = ((b4 & 0x0f) << 10 | b3 << 2 | (b2 & 0xc0) >> 6) + 1
            return (w, h)
        if chunk == b"VP8X" and n >= 30:
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return (w, h)
    return None


def _opf_path(zf: zipfile.ZipFile) -> str | None:
    """Read META-INF/container.xml and return the OPF rootfile path, or None."""
    try:
        c = BeautifulSoup(zf.read("META-INF/container.xml"), "xml")
        rf = c.find("rootfile")
        return rf.get("full-path") if rf else None
    except (KeyError, TypeError):
        return None


def _find_cover_in_epub(epub_path: Path) -> tuple[str, str] | None:
    """Locate cover image inside an EPUB. Returns (internal_path, mimetype)
    or None. Tries (in order): manifest item with properties="cover-image"
    (EPUB3), <meta name="cover"> referencing a manifest id (EPUB2), then
    common filename heuristics (cover.{jpg,png,jpeg,webp})."""
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_rel = _opf_path(zf)
            if opf_rel:
                opf = BeautifulSoup(zf.read(opf_rel), "xml")
                base = PurePosixPath(opf_rel).parent
                # EPUB3: properties="cover-image"
                for it in opf.find_all("item"):
                    props = (it.get("properties") or "").split()
                    if "cover-image" in props:
                        href = it.get("href")
                        if href:
                            return ((base / href).as_posix(),
                                    it.get("media-type") or "image/jpeg")
                # EPUB2: <meta name="cover" content="<id>"/>
                meta_cover = opf.find("meta", attrs={"name": "cover"})
                if meta_cover and meta_cover.get("content"):
                    cover_id = meta_cover["content"]
                    for it in opf.find_all("item"):
                        if it.get("id") == cover_id and it.get("href"):
                            return ((base / it["href"]).as_posix(),
                                    it.get("media-type") or "image/jpeg")
            # Filename heuristic fallback
            for name in zf.namelist():
                low = name.lower()
                if low.rsplit("/", 1)[-1] in (
                    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp"
                ):
                    ext = low.rsplit(".", 1)[-1]
                    mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                          "png": "image/png", "webp": "image/webp"}[ext]
                    return (name, mt)
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def render_chapter_html(epub_path: Path, chapter_num: int, book_id: str) -> str:
    """Concatenate the chapter's xhtml(s), strip dangerous tags, rewrite
    image src to point at our /api/img endpoint."""
    parts = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in epub_chapter_files(epub_path, chapter_num):
            soup = BeautifulSoup(zf.read(name), "html.parser")
            # Strip anything that can run code or pull external assets.
            for tag in soup(["script", "style", "link", "meta", "iframe"]):
                tag.decompose()
            # Strip on*= event handlers.
            for el in soup.find_all(True):
                for attr in list(el.attrs):
                    if attr.lower().startswith("on"):
                        del el.attrs[attr]
            # Rewrite <img src=...> to absolute zip path -> our img endpoint.
            base_dir = PurePosixPath(name).parent
            for img in soup.find_all("img"):
                src = img.get("src")
                if not src or src.startswith(("http://", "https://", "data:")):
                    continue
                resolved = (base_dir / src).as_posix()
                # Normalize "../" segments
                parts_seg = []
                for p in resolved.split("/"):
                    if p == "..":
                        if parts_seg: parts_seg.pop()
                    elif p and p != ".":
                        parts_seg.append(p)
                clean = "/".join(parts_seg)
                img["src"] = f"/api/img/{book_id}/{urllib.parse.quote(clean)}"
            # Just inner body content, not full <html>.
            body = soup.find("body")
            parts.append(str(body) if body else str(soup))
    return "\n".join(parts)


# --- routes ---------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/library")
def api_library():
    """List all books in the library."""
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    books = []
    for d in sorted(LIBRARY_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        chapters = list_chapters(d)
        if not chapters:
            continue
        prog = read_progress(d)
        meta = book_meta(d)
        epub_str = meta.get("source_epub")
        has_cover = False
        if epub_str:
            try:
                has_cover = _find_cover_in_epub(Path(epub_str)) is not None
            except Exception:
                has_cover = False
        # Default title from folder name, with trailing _mv / _MV stripped.
        default_title = re.sub(r"_mv$", "", d.name, flags=re.IGNORECASE)
        default_title = default_title.replace("_", " ").title()
        total_seconds = sum(c["duration_seconds"] for c in chapters)
        books.append({
            "id": d.name,
            "title": meta.get("title") or default_title,
            "author": meta.get("author") or "",
            "chapter_count": len(chapters),
            "duration_seconds": total_seconds,
            "last_chapter": prog.get("chapter"),
            "last_time": prog.get("time", 0.0),
            "has_cover": has_cover,
            "last_interacted": last_interacted(d),
            "manually_finished": bool(prog.get("manually_finished")),
            "tags": meta.get("tags") or [],
        })
    books.sort(key=lambda b: b["last_interacted"], reverse=True)
    return jsonify(books)


@app.route("/api/cover/<book_id>")
def api_cover(book_id):
    """Serve the cover image for a book by extracting it from the source EPUB."""
    book_dir = safe_book_dir(book_id)
    meta = book_meta(book_dir)
    epub_str = meta.get("source_epub")
    if not epub_str:
        abort(404)
    epub_path = Path(epub_str)
    if not epub_path.exists():
        abort(404)
    found = _find_cover_in_epub(epub_path)
    if not found:
        abort(404)
    internal_path, mimetype = found
    with zipfile.ZipFile(epub_path) as zf:
        try:
            data = zf.read(internal_path)
        except KeyError:
            abort(404)
    return Response(data, mimetype=mimetype)


@app.route("/api/book/<book_id>")
def api_book(book_id):
    book_dir = safe_book_dir(book_id)
    meta = book_meta(book_dir)
    default_title = re.sub(r"_mv$", "", book_id, flags=re.IGNORECASE)
    default_title = default_title.replace("_", " ").title()
    return jsonify({
        "id": book_id,
        "title": meta.get("title") or default_title,
        "chapters": list_chapters(book_dir),
        "progress": read_progress(book_dir),
    })


@app.route("/api/audio/<book_id>/<int:chapter_num>")
def api_audio(book_id, chapter_num):
    book_dir = safe_book_dir(book_id)
    mp3 = book_dir / f"chapter_{chapter_num:03d}.mp3"
    if not mp3.exists():
        abort(404)
    return send_file(mp3, mimetype="audio/mpeg", conditional=True)


@app.route("/api/text/<book_id>/<int:chapter_num>")
def api_text(book_id, chapter_num):
    book_dir = safe_book_dir(book_id)
    txt = book_dir / f"chapter_{chapter_num:03d}.txt"
    if not txt.exists():
        abort(404)
    return txt.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain; charset=utf-8"}


def collect_book_images(epub_path: Path, book_id: str, min_dim: int = 200) -> list[dict]:
    """Walk the EPUB spine in reading order; pull every <img> from every
    HTML page; resolve to /api/img URLs and read pixel dimensions so we
    can filter out scene-break / dinkus icons.

    Each entry: {chapter, section, src, alt, width, height, source_file,
                 spine_index}.
      - chapter: int chapter number, or None for front/back matter
      - section: "front" | "chapter" | "back" — bucket label for the UI"""
    out: list[dict] = []
    # Use the TOC-aware chapter map so picture-mode buckets line up with the
    # MP3 numbering produced by render_batch.
    chapter_map = discover_chapters_in_epub(epub_path)
    file_to_chapter: dict[str, int] = {}
    for n, files in chapter_map.items():
        for f in files:
            file_to_chapter[f] = n
    max_chapter = max(chapter_map) if chapter_map else 0
    with zipfile.ZipFile(epub_path) as zf:
        opf_rel = _opf_path(zf)
        if not opf_rel:
            return out
        opf = BeautifulSoup(zf.read(opf_rel), "xml")
        spine = opf.find("spine")
        manifest = opf.find("manifest")
        if not spine or not manifest:
            return out
        manifest_map = {it.get("id"): it.get("href") for it in manifest.find_all("item") if it.get("id")}
        base_dir = PurePosixPath(opf_rel).parent
        seen_chapters: set[int] = set()
        # Walk in reading (spine) order.
        first_chapter_seen = False
        for spine_idx, it in enumerate(spine.find_all("itemref")):
            href = manifest_map.get(it.get("idref"), "")
            if not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            full = (base_dir / href).as_posix()
            try:
                html = zf.read(full)
            except KeyError:
                continue
            chapter = file_to_chapter.get(full)
            if chapter is not None:
                first_chapter_seen = True
                section = "chapter"
                seen_chapters.add(chapter)
            else:
                section = "back" if first_chapter_seen and seen_chapters and max(seen_chapters) >= max_chapter else "front"
            soup = BeautifulSoup(html, "html.parser")
            page_dir = PurePosixPath(full).parent
            for img in soup.find_all("img"):
                src = img.get("src")
                if not src or src.startswith(("http://", "https://", "data:")):
                    continue
                # Resolve relative to the HTML file's directory, normalize
                # away ".." segments.
                resolved = (page_dir / src).as_posix()
                parts: list[str] = []
                for p in resolved.split("/"):
                    if p == "..":
                        if parts: parts.pop()
                    elif p and p != ".":
                        parts.append(p)
                clean = "/".join(parts)
                try:
                    img_bytes = zf.read(clean)
                except KeyError:
                    continue
                dims = image_dimensions(img_bytes) or (0, 0)
                w, h = dims
                # Filter: anything smaller than min_dim on the shorter axis
                # is almost certainly a dinkus / chapter-break ornament.
                if w and h and min(w, h) < min_dim:
                    continue
                out.append({
                    "chapter": chapter,
                    "section": section,
                    "src": f"/api/img/{book_id}/{urllib.parse.quote(clean)}",
                    "alt": img.get("alt", "") or "",
                    "width": w,
                    "height": h,
                    "source_file": href,
                    "spine_index": spine_idx,
                })
    return out


@app.route("/api/images/<book_id>")
def api_images(book_id):
    """Ordered list of every illustration in the EPUB (front matter, chapter
    art, back matter), with chapter assignment. Used by picture mode."""
    book_dir = safe_book_dir(book_id)
    meta = book_meta(book_dir)
    epub_path_str = meta.get("source_epub")
    if not epub_path_str:
        return jsonify([])
    epub_path = Path(epub_path_str)
    if not epub_path.exists():
        return jsonify([])
    return jsonify(collect_book_images(epub_path, book_id))


@app.route("/api/html/<book_id>/<int:chapter_num>")
def api_html(book_id, chapter_num):
    """Rich EPUB chapter HTML with images. Falls back to 404 if no source EPUB."""
    book_dir = safe_book_dir(book_id)
    meta = book_meta(book_dir)
    epub_path_str = meta.get("source_epub")
    if not epub_path_str:
        abort(404, "no source_epub in book.json")
    epub_path = Path(epub_path_str)
    if not epub_path.exists():
        abort(404, f"EPUB not found: {epub_path}")
    html = render_chapter_html(epub_path, chapter_num, book_id)
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/img/<book_id>/<path:img_path>")
def api_img(book_id, img_path):
    """Serve an image embedded in the source EPUB."""
    book_dir = safe_book_dir(book_id)
    meta = book_meta(book_dir)
    epub_path_str = meta.get("source_epub")
    if not epub_path_str:
        abort(404)
    epub_path = Path(epub_path_str)
    if not epub_path.exists():
        abort(404)
    # zipfile only reads paths inside the archive — no traversal possible.
    try:
        with zipfile.ZipFile(epub_path) as zf:
            data = zf.read(img_path)
    except KeyError:
        abort(404)
    # Guess MIME from extension.
    ext = Path(img_path).suffix.lower()
    mime = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")
    return Response(data, mimetype=mime)


@app.route("/api/progress/<book_id>", methods=["GET", "POST"])
def api_progress(book_id):
    book_dir = safe_book_dir(book_id)
    if request.method == "POST":
        data = request.get_json(force=True)
        existing = read_progress(book_dir)
        write_progress(book_dir, {
            "chapter": int(data.get("chapter", 0)),
            "time": float(data.get("time", 0.0)),
            "speed": float(data.get("speed", 1.0)),
            # Preserve manually_finished across auto-saves so the user's
            # explicit toggle isn't wiped by a 5-second progress tick.
            "manually_finished": bool(existing.get("manually_finished")),
        })
        return jsonify({"ok": True})
    return jsonify(read_progress(book_dir))


@app.route("/api/finish/<book_id>", methods=["POST"])
def api_finish(book_id):
    """Toggle manually_finished. Body: {"finished": true|false}."""
    book_dir = safe_book_dir(book_id)
    data = request.get_json(force=True) or {}
    existing = read_progress(book_dir)
    existing["manually_finished"] = bool(data.get("finished", True))
    # Preserve known fields, fill defaults so the file remains complete.
    write_progress(book_dir, {
        "chapter": existing.get("chapter"),
        "time": float(existing.get("time", 0.0)),
        "speed": float(existing.get("speed", 1.0)),
        "manually_finished": existing["manually_finished"],
    })
    return jsonify({"ok": True, "manually_finished": existing["manually_finished"]})


@app.route("/api/bookmarks/<book_id>", methods=["GET", "POST"])
def api_bookmarks(book_id):
    book_dir = safe_book_dir(book_id)
    bookmarks = read_bookmarks(book_dir)
    if request.method == "POST":
        data = request.get_json(force=True)
        bookmarks.append({
            "chapter": int(data.get("chapter", 0)),
            "time": float(data.get("time", 0.0)),
            "label": str(data.get("label", "")).strip()[:120],
            "created": str(data.get("created", "")),
        })
        write_bookmarks(book_dir, bookmarks)
        return jsonify({"ok": True, "count": len(bookmarks)})
    return jsonify(bookmarks)


@app.route("/api/bookmarks/<book_id>/<int:idx>", methods=["DELETE"])
def api_bookmark_delete(book_id, idx):
    book_dir = safe_book_dir(book_id)
    bookmarks = read_bookmarks(book_dir)
    if 0 <= idx < len(bookmarks):
        bookmarks.pop(idx)
        write_bookmarks(book_dir, bookmarks)
        return jsonify({"ok": True})
    abort(404)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address. 127.0.0.1 = local only (default); "
                         "0.0.0.0 = exposed to your LAN")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    print(f"Library: {LIBRARY_DIR}")
    if args.host == "0.0.0.0":
        print("WARNING: serving on all interfaces. Anyone on your network can")
        print("         see your library and bookmarks. Use only on trusted wifi.")
    app.run(host=args.host, port=args.port, debug=False)
