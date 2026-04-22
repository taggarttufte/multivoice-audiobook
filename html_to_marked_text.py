"""
Convert EPUB chapter HTML to plain text WITH expressive markers preserved.

We want to feed Grok TTS text like:
    "I can't believe it," she said. <emphasis>You actually did it.</emphasis>

So this walks the HTML DOM and emits text where italic / emphasis spans are
wrapped with <emphasis>...</emphasis> Grok tags. Other formatting is dropped.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag


ITALIC_TAGS = {"em", "i", "cite"}
ITALIC_CLASSES = {"italic", "italics", "i"}


def _is_italic(el: Tag) -> bool:
    if el.name in ITALIC_TAGS:
        return True
    cls = el.get("class") or []
    return any(c.lower() in ITALIC_CLASSES for c in cls)


def _walk(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    # Recurse children
    inner = "".join(_walk(c) for c in node.children)
    if _is_italic(node) and inner.strip():
        # Don't double-wrap if already wrapped by a parent italic.
        if not inner.strip().startswith("<emphasis>"):
            return f"<emphasis>{inner}</emphasis>"
    return inner


def chapter_to_marked_text(epub_path: Path, file_names: list[str]) -> str:
    """Concatenate text from the chapter's xhtml files, preserving italics
    as <emphasis> tags. Same min-length filter as plain extraction."""
    parts: list[str] = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in file_names:
            soup = BeautifulSoup(zf.read(name), "html.parser")
            for el in soup(["script", "style"]):
                el.decompose()
            body = soup.find("body") or soup
            text = _walk(body)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 500:
                parts.append(text)
    return " ".join(parts)
