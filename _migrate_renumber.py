"""One-shot: renumber existing rendered books to skip front matter.

Walks each library/<book>/, builds an old_chapter_num -> new_chapter_num map
by comparing unfiltered TOC discovery against filtered, then renames
chapter_NNN.{mp3,txt,timing.json}, deletes files for skipped chapters, and
migrates .progress.json + .bookmarks.json. Wipes .durations.json (auto-regenerates).

Run from project root:  python _migrate_renumber.py
Idempotent — running again is a no-op once renamed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "player"))
from app import discover_chapters_in_epub  # noqa: E402

PROJECT = Path(__file__).resolve().parent
LIBRARY = PROJECT / "library"


def renumber_book(book_dir: Path) -> dict:
    book_json = book_dir / "book.json"
    if not book_json.exists():
        return {"book": book_dir.name, "skipped_reason": "no book.json"}
    meta = json.loads(book_json.read_text(encoding="utf-8"))
    epub_str = meta.get("source_epub")
    if not epub_str or not Path(epub_str).exists():
        return {"book": book_dir.name, "skipped_reason": "no/missing epub"}
    epub = Path(epub_str)

    new_chapters = discover_chapters_in_epub(epub, skip_front_matter=True)
    old_chapters = discover_chapters_in_epub(epub, skip_front_matter=False)

    file_to_new: dict[str, int] = {}
    for n, files in new_chapters.items():
        for f in files:
            file_to_new[f] = n

    rename_map: dict[int, int | None] = {}
    for old_num, files in old_chapters.items():
        rename_map[old_num] = file_to_new.get(files[0]) if files else None

    deleted = []
    renamed = []
    for old_num in sorted(rename_map):
        new_num = rename_map[old_num]
        for ext in ("mp3", "txt", "timing.json"):
            src = book_dir / f"chapter_{old_num:03d}.{ext}"
            if not src.exists():
                continue
            if new_num is None:
                src.unlink()
                deleted.append(src.name)
            elif new_num != old_num:
                dst = book_dir / f"chapter_{new_num:03d}.{ext}"
                if dst.exists():
                    dst.unlink()  # safe: new_num < old_num for non-skipped
                src.rename(dst)
                renamed.append(f"{src.name} -> {dst.name}")

    progress_path = book_dir / ".progress.json"
    if progress_path.exists():
        try:
            prog = json.loads(progress_path.read_text(encoding="utf-8"))
            old_chap = prog.get("chapter")
            if old_chap is not None:
                new_chap = rename_map.get(old_chap)
                if new_chap is None:
                    prog = {"chapter": None, "time": 0.0,
                            "speed": prog.get("speed", 1.0)}
                else:
                    prog["chapter"] = new_chap
                progress_path.write_text(json.dumps(prog, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[!] {book_dir.name}: progress migration failed: {e}")

    bookmarks_path = book_dir / ".bookmarks.json"
    if bookmarks_path.exists():
        try:
            bms = json.loads(bookmarks_path.read_text(encoding="utf-8"))
            new_bms = []
            for bm in bms:
                old_chap = bm.get("chapter")
                new_chap = rename_map.get(old_chap)
                if new_chap is not None:
                    bm["chapter"] = new_chap
                    new_bms.append(bm)
            bookmarks_path.write_text(json.dumps(new_bms, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[!] {book_dir.name}: bookmark migration failed: {e}")

    dur_path = book_dir / ".durations.json"
    if dur_path.exists():
        dur_path.unlink()

    return {
        "book": book_dir.name,
        "old_count": len(old_chapters),
        "new_count": len(new_chapters),
        "deleted": len(deleted),
        "renamed": len(renamed),
    }


def main() -> None:
    if not LIBRARY.exists():
        print(f"No library at {LIBRARY}")
        return
    for d in sorted(LIBRARY.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        result = renumber_book(d)
        print(result)


if __name__ == "__main__":
    main()
