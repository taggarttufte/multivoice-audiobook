// Minimal audiobook player. Library view -> player view. State kept in DOM/JS.
// Per-book progress posted to backend every 5s; restored on book open.

const $ = id => document.getElementById(id);
const fmt = sec => {
  if (!isFinite(sec)) return "0:00";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
};

let currentBook = null;     // {id, title, chapters: [...], progress: {...}}
let currentChapter = null;  // chapter number (int)
const audio = $("audio");

// Position indicator: list of {node, start, end} where each entry is a
// paragraph-like element and its char range in the chapter's text.
let positionSegments = [];
let totalTextLength = 0;

// --- Speed: single source of truth helper --------------------------------
// HTML5 audio resets playbackRate on every audio.load(), so we always
// reapply from this helper after the new chapter's metadata loads. Also
// remembers across-book default in localStorage.
function setSpeed(v) {
  v = Math.max(0.5, Math.min(4, parseFloat(v) || 1.0));
  audio.playbackRate = v;
  $("speed").value = v;
  const label = v.toFixed(2) + "×";
  $("speed-value").textContent = label;
  $("speed-label").textContent = label;
  localStorage.setItem("lastSpeed", String(v));
}

// --- Library ---------------------------------------------------------------

async function loadLibrary() {
  const books = await fetch("/api/library").then(r => r.json());
  const list = $("library-list");
  list.innerHTML = "";
  if (!books.length) {
    list.innerHTML = `<p class="hint">No books yet. Render some chapters with render_batch.py.</p>`;
    return;
  }
  for (const b of books) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="title">${b.title}</div>
      <div class="sub">${b.chapter_count} chapter${b.chapter_count === 1 ? "" : "s"}${
        b.last_chapter ? ` · last on ch${b.last_chapter}` : ""
      }</div>`;
    card.onclick = () => openBook(b.id);
    list.appendChild(card);
  }
}

// --- Book / player ---------------------------------------------------------

async function openBook(bookId) {
  currentBook = await fetch(`/api/book/${bookId}`).then(r => r.json());
  $("library-view").hidden = true;
  $("player-view").hidden = false;
  $("player-bar").hidden = false;
  $("book-title").textContent = currentBook.title;
  loadBookmarks();

  const list = $("chapter-list");
  list.innerHTML = "";
  for (const ch of currentBook.chapters) {
    const li = document.createElement("li");
    li.textContent = `Chapter ${ch.num}`;
    li.dataset.num = ch.num;
    li.onclick = () => playChapter(ch.num);
    list.appendChild(li);
  }

  // Restore last position if we have one, else start at first chapter.
  // Speed precedence: saved per-book > last-used (localStorage) > 1.0
  const p = currentBook.progress;
  const startCh = p.chapter || currentBook.chapters[0].num;
  const startTime = p.chapter ? (p.time || 0) : 0;
  const startSpeed = p.speed || parseFloat(localStorage.getItem("lastSpeed")) || 1.0;
  setSpeed(startSpeed);
  await playChapter(startCh, startTime, /*autoplay*/ false);
}

async function playChapter(chapterNum, seekTo = 0, autoplay = true) {
  currentChapter = chapterNum;
  audio.src = `/api/audio/${currentBook.id}/${chapterNum}`;
  audio.load();
  audio.addEventListener("loadedmetadata", () => {
    // audio.load() resets playbackRate — reapply from current slider value.
    setSpeed(parseFloat($("speed").value));
    if (seekTo > 0) audio.currentTime = seekTo;
    if (autoplay) audio.play();
    updateUi();
  }, { once: true });

  $("chapter-label").textContent = `Chapter ${chapterNum}`;
  for (const li of document.querySelectorAll("#chapter-list li")) {
    li.classList.toggle("active", parseInt(li.dataset.num) === chapterNum);
  }

  // Load read-along content. Prefer rich EPUB HTML (with images, paragraphs,
  // italics); fall back to plain .txt if no source EPUB is configured.
  const el = $("chapter-text");
  el.textContent = "(loading...)";
  el.classList.remove("fallback-text");
  positionSegments = [];
  totalTextLength = 0;
  try {
    const htmlResp = await fetch(`/api/html/${currentBook.id}/${chapterNum}`);
    if (htmlResp.ok) {
      el.innerHTML = await htmlResp.text();
      buildPositionMap();
      return;
    }
    const txtResp = await fetch(`/api/text/${currentBook.id}/${chapterNum}`);
    if (txtResp.ok) {
      el.classList.add("fallback-text");
      el.textContent = await txtResp.text();
      buildPositionMap();
    } else {
      el.textContent = "(no text)";
    }
  } catch (e) {
    el.textContent = "(no text)";
  }
}

// --- Position indicator ---------------------------------------------------

function buildPositionMap() {
  // Walk the rendered chapter, treat each block-level element as one segment
  // (or each line for the plain-text fallback). Cumulative char offsets let
  // us map an audio fraction to a paragraph cheaply.
  positionSegments = [];
  const root = $("chapter-text");

  // Plain-text fallback: chunk by line.
  if (root.classList.contains("fallback-text")) {
    // Wrap each non-empty line in a span so we can highlight it.
    const lines = root.textContent.split(/\n/);
    root.innerHTML = "";
    let cursor = 0;
    for (const line of lines) {
      if (line.trim()) {
        const span = document.createElement("div");
        span.textContent = line;
        span.className = "ra-line";
        root.appendChild(span);
        positionSegments.push({ node: span, start: cursor, end: cursor + line.length });
        cursor += line.length;
      } else {
        root.appendChild(document.createElement("br"));
      }
    }
    totalTextLength = cursor;
    return;
  }

  // Rich HTML: paragraphs, headings, blockquotes count as segments.
  const blocks = root.querySelectorAll("p, h1, h2, h3, h4, h5, h6, blockquote, li");
  let cursor = 0;
  for (const b of blocks) {
    const len = (b.textContent || "").length;
    if (len > 0) {
      positionSegments.push({ node: b, start: cursor, end: cursor + len });
      cursor += len;
    }
  }
  totalTextLength = cursor;
}

let lastHighlighted = null;
function updatePositionIndicator() {
  if (!totalTextLength || !audio.duration) return;
  const frac = audio.currentTime / audio.duration;
  const target = Math.floor(frac * totalTextLength);
  // Binary-friendly linear scan (segments are tiny; not worth a tree).
  let hit = null;
  for (const seg of positionSegments) {
    if (seg.start <= target && target <= seg.end) { hit = seg; break; }
  }
  if (!hit) hit = positionSegments[positionSegments.length - 1];
  if (!hit || hit.node === lastHighlighted) return;
  if (lastHighlighted) lastHighlighted.classList.remove("current-position");
  hit.node.classList.add("current-position");
  lastHighlighted = hit.node;
  // Only scroll if it's out of view, and use the panel as the scroll container.
  const panel = $("chapter-text").parentElement;
  const nodeRect = hit.node.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  if (nodeRect.top < panelRect.top || nodeRect.bottom > panelRect.bottom) {
    hit.node.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// --- Controls --------------------------------------------------------------

$("play-pause").onclick = () => audio.paused ? audio.play() : audio.pause();
$("back-30").onclick    = () => audio.currentTime = Math.max(0, audio.currentTime - 30);
$("fwd-30").onclick     = () => audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 30);
$("prev-chap").onclick  = () => navigateChapter(-1);
$("next-chap").onclick  = () => navigateChapter(+1);
$("back-to-library").onclick = () => {
  audio.pause();
  $("player-view").hidden = true;
  $("player-bar").hidden = true;
  $("library-view").hidden = false;
  loadLibrary();
};

$("speed").oninput = e => setSpeed(e.target.value);

$("seek").oninput = e => {
  if (audio.duration) audio.currentTime = (parseFloat(e.target.value) / 100) * audio.duration;
};

function navigateChapter(delta) {
  const nums = currentBook.chapters.map(c => c.num);
  const idx = nums.indexOf(currentChapter);
  const next = nums[idx + delta];
  if (next != null) playChapter(next);
}

// --- UI updates + progress save -------------------------------------------

function updateUi() {
  $("play-pause").textContent = audio.paused ? "▶" : "⏸";
  $("time-label").textContent = `${fmt(audio.currentTime)} / ${fmt(audio.duration)}`;
  $("seek").value = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
}

audio.addEventListener("timeupdate", updateUi);
audio.addEventListener("play",  () => { updateUi(); updatePositionIndicator(); });
audio.addEventListener("pause", () => { updateUi(); updatePositionIndicator(); });
audio.addEventListener("seeked", updatePositionIndicator);
audio.addEventListener("loadedmetadata", updatePositionIndicator);
audio.addEventListener("ended", () => navigateChapter(+1));

// Save progress every 5 seconds while playing
setInterval(() => {
  if (!currentBook || !currentChapter || audio.paused) return;
  fetch(`/api/progress/${currentBook.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chapter: currentChapter,
      time: audio.currentTime,
      speed: audio.playbackRate,
    }),
  });
}, 5000);

// Keyboard: space=pause, arrow keys=skip, b=bookmark
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT") return;
  if (e.code === "Space")      { e.preventDefault(); $("play-pause").click(); }
  else if (e.code === "ArrowLeft")  $("back-30").click();
  else if (e.code === "ArrowRight") $("fwd-30").click();
  else if (e.code === "KeyB")       $("bookmark").click();
});

// --- Bookmarks ------------------------------------------------------------

async function loadBookmarks() {
  if (!currentBook) return;
  const bms = await fetch(`/api/bookmarks/${currentBook.id}`).then(r => r.json());
  renderBookmarks(bms);
}

function renderBookmarks(bms) {
  const list = $("bookmark-list");
  list.innerHTML = "";
  if (!bms.length) {
    list.innerHTML = `<li class="empty">none yet — press 🔖 or 'b' to add one</li>`;
    return;
  }
  bms.forEach((bm, idx) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="bm-label" title="Jump to Ch${bm.chapter} ${fmt(bm.time)}">
        ${bm.label || `Ch${bm.chapter}`}
      </span>
      <span class="bm-meta">Ch${bm.chapter} · ${fmt(bm.time)}</span>
      <button class="bm-del" title="Delete">×</button>`;
    li.querySelector(".bm-label").onclick = () => {
      playChapter(bm.chapter, bm.time, /*autoplay*/ true);
    };
    li.querySelector(".bm-del").onclick = async (e) => {
      e.stopPropagation();
      await fetch(`/api/bookmarks/${currentBook.id}/${idx}`, { method: "DELETE" });
      loadBookmarks();
    };
    list.appendChild(li);
  });
}

$("bookmark").onclick = async () => {
  if (!currentBook || !currentChapter) return;
  // auto-label: try to grab a snippet of nearby text from the read-along panel
  const txt = $("chapter-text").textContent || "";
  const ratio = audio.duration ? (audio.currentTime / audio.duration) : 0;
  const startChar = Math.floor(ratio * txt.length);
  const snippet = txt.slice(startChar, startChar + 60).replace(/\s+/g, " ").trim();
  await fetch(`/api/bookmarks/${currentBook.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chapter: currentChapter,
      time: audio.currentTime,
      label: snippet,
      created: new Date().toISOString(),
    }),
  });
  // little flash to confirm it saved
  const btn = $("bookmark");
  btn.classList.add("flash");
  setTimeout(() => btn.classList.remove("flash"), 400);
  loadBookmarks();
};

// Boot
loadLibrary();
