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
import re
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory


LIBRARY_DIR = Path(__file__).parent.parent / "library"
STATIC_DIR = Path(__file__).parent / "static"

CHAPTER_FILE_RE = re.compile(r"chapter(\d+)", re.IGNORECASE)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


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
    """Return [{'num': 1, 'mp3': 'chapter_001.mp3', 'size': 12345}, ...]"""
    chapters = []
    for mp3 in sorted(book_dir.glob("chapter_*.mp3")):
        m = re.match(r"chapter_(\d+)\.mp3", mp3.name)
        if not m:
            continue
        chapters.append({
            "num": int(m.group(1)),
            "mp3": mp3.name,
            "size": mp3.stat().st_size,
            "has_text": (book_dir / f"chapter_{int(m.group(1)):03d}.txt").exists(),
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
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"chapter": None, "time": 0.0, "speed": 1.0}


def write_progress(book_dir: Path, data: dict) -> None:
    progress_path(book_dir).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


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


def epub_chapter_files(epub_path: Path, chapter_num: int) -> list[str]:
    """All xhtml files in the EPUB whose name contains 'chapter<NUM>'."""
    out = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            m = CHAPTER_FILE_RE.search(name)
            if m and int(m.group(1)) == chapter_num:
                out.append(name)
    return out


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
        books.append({
            "id": d.name,
            "title": d.name.replace("_", " ").title(),
            "chapter_count": len(chapters),
            "last_chapter": prog.get("chapter"),
            "last_time": prog.get("time", 0.0),
        })
    return jsonify(books)


@app.route("/api/book/<book_id>")
def api_book(book_id):
    book_dir = safe_book_dir(book_id)
    return jsonify({
        "id": book_id,
        "title": book_id.replace("_", " ").title(),
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
        write_progress(book_dir, {
            "chapter": int(data.get("chapter", 0)),
            "time": float(data.get("time", 0.0)),
            "speed": float(data.get("speed", 1.0)),
        })
        return jsonify({"ok": True})
    return jsonify(read_progress(book_dir))


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
