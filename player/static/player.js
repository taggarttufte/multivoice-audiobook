// Audiobook player — drives the polished UI against the Flask backend.
// Library view -> player view. Per-book progress posted every 5s.

const $ = id => document.getElementById(id);
const fmt = sec => {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`
    : `${m}:${String(s).padStart(2,"0")}`;
};
const fmtDur = sec => {
  if (!isFinite(sec) || sec <= 0) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
};

let LIBRARY = [];          // cached library list
let currentBook = null;    // {id, title, author, chapters, progress}
let currentChapter = null;
const audio = $("audio");

let positionSegments = [];
let totalTextLength = 0;
// When chapter_NNN.timing.json exists, this holds [{node, start, end}, ...]
// keyed on real audio time (seconds), and we use exact lookup instead of
// the char-fraction heuristic.
let timedSegments = [];

// ---------- Tags ---------------------------------------------------------
// Curated registry mirrors the design's static palette. Unknown tags
// (created by the user in earlier sessions) get an auto hue so they still
// render gracefully.
const TAG_REGISTRY = {
  "fiction":     { label:"Fiction",     glyph:"F",  hue:270 },
  "non-fiction": { label:"Non-fiction", glyph:"N",  hue:30  },
  "sci-fi":      { label:"Sci-fi",      glyph:"S",  hue:200 },
  "fantasy":     { label:"Fantasy",     glyph:"Y",  hue:285 },
  "literary":    { label:"Literary",    glyph:"L",  hue:40  },
  "horror":      { label:"Horror",      glyph:"H",  hue:0   },
  "thriller":    { label:"Thriller",    glyph:"T",  hue:350 },
  "comfort":     { label:"Comfort",     glyph:"C",  hue:320 },
  "history":     { label:"History",     glyph:"Hi", hue:25  },
  "psychology":  { label:"Psychology",  glyph:"P",  hue:180 },
  "favorites":   { label:"Favorites",   glyph:"★",  hue:50  },
  "re-read":     { label:"Re-read",     glyph:"R",  hue:170 },
};
const STATUS_TAGS = {
  "not-started":{ label:"Not started", glyph:"–", hue:210 },
  "in-progress":{ label:"In progress", glyph:"›", hue:140 },
  "finished":   { label:"Finished",    glyph:"✓", hue:150 },
};
function tagInfo(tid) {
  if (STATUS_TAGS[tid]) return STATUS_TAGS[tid];
  if (TAG_REGISTRY[tid]) return TAG_REGISTRY[tid];
  // Hash an unknown tag id to a stable hue.
  let h = 0;
  for (let i = 0; i < tid.length; i++) h = (h * 31 + tid.charCodeAt(i)) % 360;
  return { label: tid, glyph: tid[0]?.toUpperCase() || "?", hue: h };
}
function statusTagFor(b) {
  if (b.manually_finished || b.progress_fraction >= 0.99) return "finished";
  if (b.progress_fraction > 0)     return "in-progress";
  return "not-started";
}
function tagsFor(b) {
  return [statusTagFor(b), ...(b.tags || [])];
}
function tagPill(tid, opts = {}) {
  const t = tagInfo(tid);
  const isStatus = !!STATUS_TAGS[tid];
  const cls = ["tag-pill", isStatus ? "is-status" : ""].filter(Boolean).join(" ");
  return `<span class="${cls}" style="--tag-hue:${t.hue}" data-tag="${tid}">`
       + `<span class="g">${t.glyph}</span>${t.label}`
       + (opts.remove ? `<button class="x" data-remove="${tid}" aria-label="Remove tag">×</button>` : "")
       + `</span>`;
}

// Cover SVG fallback — gradient + initials, derived from book id (stable).
function coverSVG(book) {
  let h = 0;
  for (let i = 0; i < book.id.length; i++) h = (h * 31 + book.id.charCodeAt(i)) % 360;
  const a = `hsl(${h}, 35%, 18%)`;
  const b = `hsl(${(h + 40) % 360}, 45%, 32%)`;
  const initials = (book.title || book.id).split(/\s+/).slice(0, 2)
    .map(s => s[0]).join("").toUpperCase();
  const lastName = (book.author || "").split(/\s+/).slice(-1)[0]?.toUpperCase() || "";
  const titleStr = (book.title || "").length > 18
    ? (book.title || "").slice(0, 18) + "…"
    : (book.title || "");
  return `
    <svg viewBox="0 0 200 300" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">
      <defs>
        <linearGradient id="g-${book.id}" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="${a}"/><stop offset="1" stop-color="${b}"/>
        </linearGradient>
      </defs>
      <rect width="200" height="300" fill="url(#g-${book.id})"/>
      <g transform="translate(20,30)" fill="rgba(255,255,255,0.7)" font-family="'Instrument Serif', Georgia, serif">
        <text font-size="20" letter-spacing="0.04em">${lastName}</text>
      </g>
      <g transform="translate(20,260)" fill="rgba(255,255,255,0.97)" font-family="'Instrument Serif', Georgia, serif">
        <text font-size="24" font-style="italic">${titleStr}</text>
      </g>
      <text x="175" y="45" font-size="28" font-family="'Instrument Serif', Georgia, serif" fill="rgba(255,255,255,0.15)" text-anchor="end">${initials}</text>
    </svg>`;
}

// ---------- Library ------------------------------------------------------

let activeFilter = "all";

function getFilters() {
  const filters = [{ id: "all", label: "All" }];
  for (const [id, t] of Object.entries(STATUS_TAGS)) {
    filters.push({ id, label: t.label });
  }
  // Tags currently used by at least one book (avoids a giant chip row).
  const used = new Set();
  for (const b of LIBRARY) for (const t of (b.tags || [])) used.add(t);
  for (const id of used) filters.push({ id, label: tagInfo(id).label });
  return filters;
}

function matchesFilter(b, f) {
  if (f === "all")          return true;
  if (f === "in-progress")  return b.progress_fraction > 0 && b.progress_fraction < 0.99;
  if (f === "not-started")  return !b.progress_fraction;
  if (f === "finished")     return b.progress_fraction >= 0.99;
  return (b.tags || []).includes(f);
}

function renderFilters() {
  const wrap = $("library-filters");
  wrap.innerHTML = getFilters().map(f => {
    const count = f.id === "all"
      ? LIBRARY.length
      : LIBRARY.filter(b => matchesFilter(b, f.id)).length;
    return `<button class="chip ${f.id === activeFilter ? "active" : ""}" data-filter="${f.id}">`
         + `${f.label}<span class="chip-count">${count}</span></button>`;
  }).join("");
  wrap.querySelectorAll(".chip").forEach(btn => {
    btn.addEventListener("click", () => {
      activeFilter = btn.dataset.filter;
      renderFilters();
      renderBooks();
    });
  });
}

function renderBooks() {
  const grid = $("book-grid");
  const filtered = LIBRARY.filter(b => matchesFilter(b, activeFilter));

  const sub = $("library-subtitle");
  if (activeFilter === "all") {
    const inProg = LIBRARY.filter(b => b.progress_fraction > 0 && b.progress_fraction < 0.99).length;
    sub.textContent = LIBRARY.length === 0
      ? "Render a book with render_batch.py to get started."
      : `${LIBRARY.length} title${LIBRARY.length === 1 ? "" : "s"} · ${inProg} in progress`;
  } else {
    const f = getFilters().find(x => x.id === activeFilter);
    sub.textContent = `${filtered.length} ${filtered.length === 1 ? "title" : "titles"} in “${f?.label || activeFilter}”`;
  }

  if (!filtered.length) {
    grid.innerHTML = `<div class="grid-empty">${
      LIBRARY.length === 0
        ? "No books yet. Render some chapters with render_batch.py."
        : "No books match this filter."
    }</div>`;
    return;
  }

  grid.innerHTML = filtered.map(b => {
    const isFinished = b.manually_finished || b.progress_fraction >= 0.99;
    const isInProgress = !isFinished && b.progress_fraction > 0;
    return `
      <div class="book-card ${isInProgress ? "in-progress" : ""} ${isFinished ? "is-finished" : ""}" role="button" tabindex="0" data-id="${b.id}">
        <div class="cover">
          <div class="cover-fallback">${coverSVG(b)}</div>
          ${b.has_cover ? `<img class="cover-img" alt=""
                 src="/api/cover/${b.id}"
                 onload="this.classList.add('loaded')"
                 onerror="this.remove()">` : ""}
          <div class="cover-overlay">
            <div class="cover-title">${b.title}</div>
            ${b.author ? `<div class="cover-author">${b.author}</div>` : ""}
          </div>
          <button class="finish-toggle ${isFinished ? "on" : ""}"
                  data-finish="${b.id}"
                  aria-label="${isFinished ? "Mark as unfinished" : "Mark as finished"}"
                  title="${isFinished ? "Unmark finished" : "Mark as finished"}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M5 13l4 4L19 7"/>
            </svg>
          </button>
          <div class="play-overlay" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13a1 1 0 0 0 1.52.86l10.5-6.5a1 1 0 0 0 0-1.72L9.52 4.64A1 1 0 0 0 8 5.5z"/></svg>
          </div>
          <div class="progress-rail"><span style="width:${Math.round(b.progress_fraction * 100)}%"></span></div>
        </div>
        <div class="tag-row" data-book="${b.id}">
          ${tagsFor(b).map(tid => tagPill(tid, { remove: !STATUS_TAGS[tid] })).join("")}
          <button class="tag-pill tag-add" data-book="${b.id}" aria-label="Add tag"><span class="g">+</span>Tag</button>
        </div>
        <div class="meta">
          ${fmtDur(b.duration_seconds) ? `<span>${fmtDur(b.duration_seconds)}</span>` : ""}
          ${isInProgress
              ? `<span class="dot"></span><span>${Math.round(b.progress_fraction * 100)}% in</span>`
              : b.progress_fraction >= 0.99
                ? `<span class="dot"></span><span>Finished</span>`
                : `<span class="dot"></span><span>${b.chapter_count} ch</span>`}
        </div>
      </div>`;
  }).join("");

  grid.querySelectorAll(".book-card").forEach(el => {
    el.addEventListener("click", e => {
      if (e.target.closest(".tag-pill") ||
          e.target.closest(".tag-menu")  ||
          e.target.closest(".finish-toggle")) return;
      openBook(el.dataset.id);
    });
    el.addEventListener("keydown", e => {
      if ((e.code === "Enter" || e.code === "Space") &&
          !e.target.closest(".tag-pill") &&
          !e.target.closest(".finish-toggle")) {
        e.preventDefault();
        openBook(el.dataset.id);
      }
    });
  });
  grid.querySelectorAll(".finish-toggle").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const book = LIBRARY.find(x => x.id === btn.dataset.finish);
      if (!book) return;
      const next = !(book.manually_finished || book.progress_fraction >= 0.99);
      setFinished(book, next);
    });
  });
  grid.querySelectorAll("[data-remove]").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const card = btn.closest(".book-card");
      const book = LIBRARY.find(x => x.id === card.dataset.id);
      book.tags = (book.tags || []).filter(t => t !== btn.dataset.remove);
      saveTags(book);
      renderBooks(); renderFilters();
    });
  });
  grid.querySelectorAll(".tag-add").forEach(btn => {
    btn.addEventListener("click", e => { e.stopPropagation(); openTagMenu(btn); });
  });
}

async function loadLibrary() {
  LIBRARY = await fetch("/api/library").then(r => r.json());
  // Server already sorts by last_interacted desc, but resort defensively in
  // case future caching or proxies reorder it.
  LIBRARY.sort((a, b) => (b.last_interacted || 0) - (a.last_interacted || 0));
  renderFilters();
  renderBooks();
}

// Mark/unmark a book as finished and refresh the library view.
async function setFinished(book, finished) {
  await fetch(`/api/finish/${book.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ finished }),
  });
  book.manually_finished = finished;
  if (finished) book.progress_fraction = 1.0;
  renderBooks();
  renderFilters();
}

// Tag picker menu (positioned near the +Tag button).
function openTagMenu(trigger) {
  closeTagMenu();
  const book = LIBRARY.find(b => b.id === trigger.dataset.book);
  const used = new Set(book.tags || []);
  const avail = Object.entries(TAG_REGISTRY).filter(([id]) => !used.has(id));
  const menu = document.createElement("div");
  menu.className = "tag-menu";
  menu.innerHTML = `
    <div class="tag-menu-head">Add tag</div>
    <div class="tag-menu-list">${
      avail.length
        ? avail.map(([id, t]) => `<button class="tag-menu-item" data-pick="${id}" style="--tag-hue:${t.hue}"><span class="g">${t.glyph}</span>${t.label}</button>`).join("")
        : `<div class="tag-menu-empty">All built-in tags applied.</div>`
    }</div>
    <div class="tag-menu-new">
      <input type="text" placeholder="Create new tag…" maxlength="20" class="tag-menu-input">
      <button class="tag-menu-create">+</button>
    </div>`;
  document.body.appendChild(menu);
  const r = trigger.getBoundingClientRect();
  menu.style.left = Math.min(r.left, window.innerWidth - 300) + "px";
  menu.style.top  = (r.bottom + window.scrollY + 6) + "px";

  menu.querySelectorAll("[data-pick]").forEach(b => {
    b.addEventListener("click", () => {
      book.tags = [...(book.tags || []), b.dataset.pick];
      saveTags(book);
      closeTagMenu(); renderBooks(); renderFilters();
    });
  });
  const input = menu.querySelector(".tag-menu-input");
  const create = () => {
    const raw = input.value.trim();
    if (!raw) return;
    const id = raw.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9\-]/g, "");
    if (!id) return;
    if (!TAG_REGISTRY[id]) {
      let h = 0;
      for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
      TAG_REGISTRY[id] = { label: raw, glyph: raw[0].toUpperCase(), hue: h };
    }
    book.tags = [...(book.tags || []), id];
    saveTags(book);
    closeTagMenu(); renderBooks(); renderFilters();
  };
  menu.querySelector(".tag-menu-create").addEventListener("click", create);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") create();
    if (e.key === "Escape") closeTagMenu();
  });
  input.focus();
  setTimeout(() => document.addEventListener("click", outsideCloseTagMenu));
}
function outsideCloseTagMenu(e) {
  if (!e.target.closest(".tag-menu") && !e.target.closest(".tag-add")) closeTagMenu();
}
function closeTagMenu() {
  document.querySelectorAll(".tag-menu").forEach(m => m.remove());
  document.removeEventListener("click", outsideCloseTagMenu);
}

function saveTags(book) {
  fetch(`/api/tags/${book.id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tags: book.tags || [] }),
  });
}

// ---------- Speed --------------------------------------------------------

function setSpeed(v) {
  v = Math.max(0.5, Math.min(4, parseFloat(v) || 1.0));
  audio.playbackRate = v;
  $("speed-slider").value = v;
  $("speed-value").textContent = v.toFixed(2) + "×";
  updateSliderFill($("speed-slider"));
  document.querySelectorAll(".pb-speed .presets button").forEach(b => {
    b.classList.toggle("active", Math.abs(parseFloat(b.dataset.speed) - v) < 0.01);
  });
  localStorage.setItem("lastSpeed", String(v));
  // ETA depends on playback rate — refresh the time display whenever
  // speed changes (not just on timeupdate).
  if (typeof updateUi === "function") updateUi();
}

// ---------- Volume -------------------------------------------------------

function setVolume(v) {
  v = Math.max(0, Math.min(1, parseFloat(v) || 0));
  audio.volume = v;
  const pct = Math.round(v * 100);
  $("volume-slider").value = pct;
  $("volume-value").textContent = pct + "%";
  updateSliderFill($("volume-slider"));
  localStorage.setItem("lastVolume", String(v));
}

// Restore volume from last session immediately so it applies as soon as
// audio is loaded (HTMLAudioElement.volume persists across src changes).
setVolume(parseFloat(localStorage.getItem("lastVolume") ?? "1"));

function updateSliderFill(el) {
  const pct = ((parseFloat(el.value) - parseFloat(el.min)) /
               (parseFloat(el.max) - parseFloat(el.min))) * 100;
  el.style.setProperty("--val", pct + "%");
}

// ---------- Player view --------------------------------------------------

async function openBook(bookId) {
  currentBook = await fetch(`/api/book/${bookId}`).then(r => r.json());
  goPlayer();

  $("ra-title").textContent = currentBook.title;
  $("ra-author").textContent = currentBook.author || "";
  $("crumbs").textContent = currentBook.title;
  // Re-apply current mode now that we have a book — populates whichever
  // stage is active.
  applyMode(getMode());

  loadBookmarks();
  renderChapters();

  const p = currentBook.progress;
  const startCh = p.chapter || currentBook.chapters[0].num;
  const startTime = p.chapter ? (p.time || 0) : 0;
  const startSpeed = p.speed || parseFloat(localStorage.getItem("lastSpeed")) || 1.0;
  setSpeed(startSpeed);
  await playChapter(startCh, startTime, /*autoplay*/ false);
}

function renderChapters() {
  const ul = $("chapter-list");
  const lastNum = currentBook.progress?.chapter;
  ul.innerHTML = currentBook.chapters.map(c => {
    const done = lastNum && c.num < lastNum;
    return `<li data-num="${c.num}" class="${done ? "done" : ""}">
      <span class="num">${String(c.num).padStart(2, "0")}</span>
      <span class="ch-title">Chapter ${c.num}</span>
      <span class="dur">${fmt(c.duration_seconds || 0)}</span>
    </li>`;
  }).join("");
  $("chapters-count").textContent = currentBook.chapters.length;
  ul.querySelectorAll("li").forEach(li => {
    li.addEventListener("click", () => playChapter(parseInt(li.dataset.num)));
  });
}

async function playChapter(chapterNum, seekTo = 0, autoplay = true) {
  currentChapter = chapterNum;
  audio.src = `/api/audio/${currentBook.id}/${chapterNum}`;
  audio.load();
  audio.addEventListener("loadedmetadata", () => {
    setSpeed(parseFloat($("speed-slider").value));
    if (seekTo > 0) audio.currentTime = seekTo;
    if (autoplay) audio.play();
    updateUi();
  }, { once: true });

  const numStr = String(chapterNum).padStart(2, "0");
  $("pb-ch-num").textContent  = `CH ${numStr}`;
  $("pb-ch-name").textContent = `Chapter ${chapterNum}`;
  $("ra-ch-label").textContent = `Chapter ${chapterNum}`;
  $("np-chapter").textContent = `Chapter ${chapterNum}`;
  // If picture mode is active, re-render so past/current/future buckets
  // shift with the chapter change. Uses cached groups, no refetch.
  if (getMode() === "picture" && picCache.images) renderPictureStage(picCache.images);

  document.querySelectorAll("#chapter-list li").forEach(li => {
    li.classList.toggle("active", parseInt(li.dataset.num) === chapterNum);
  });

  // Read-along precedence:
  //   1. rich EPUB HTML (preserves paragraphs, italics, images)
  //   2. timing.json (exact audio↔paragraph alignment, but loses formatting)
  //   3. plain .txt (no alignment, no formatting)
  const el = $("chapter-text");
  el.textContent = "(loading...)";
  el.classList.remove("fallback-text");
  positionSegments = [];
  timedSegments = [];
  totalTextLength = 0;
  try {
    const htmlResp = await fetch(`/api/html/${currentBook.id}/${chapterNum}`);
    if (htmlResp.ok) {
      el.innerHTML = await htmlResp.text();
      buildPositionMap();
      return;
    }
    const tResp = await fetch(`/api/timing/${currentBook.id}/${chapterNum}`);
    if (tResp.ok) {
      renderTimedRead(await tResp.json());
      injectChapterImages(chapterNum);
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

// ---------- Chapter illustrations (overlay onto timed read-along) -------

async function injectChapterImages(chapterNum) {
  // Timed read-along ignores the EPUB HTML, so any illustrations the book
  // ships with would otherwise never appear. Pull them out of /api/html and
  // prepend a figures row at the top of the chapter view.
  try {
    const r = await fetch(`/api/html/${currentBook.id}/${chapterNum}`);
    if (!r.ok) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = await r.text();
    const imgs = tmp.querySelectorAll("img");
    if (!imgs.length) return;
    const row = document.createElement("div");
    row.className = "chapter-figures";
    imgs.forEach(img => {
      const fig = document.createElement("figure");
      const newImg = document.createElement("img");
      newImg.src = img.getAttribute("src") || "";
      newImg.alt = img.getAttribute("alt") || "";
      newImg.loading = "lazy";
      fig.appendChild(newImg);
      row.appendChild(fig);
    });
    const el = $("chapter-text");
    el.insertBefore(row, el.firstChild);
  } catch (e) { /* ignore */ }
}

// ---------- Timed read-along (uses chapter_NNN.timing.json) -------------

function renderTimedRead(timing) {
  const el = $("chapter-text");
  el.innerHTML = "";
  timedSegments = [];
  // The TTS-fed text contains <emphasis>...</emphasis> markup; convert it
  // to <em> for display. Other markdown-ish artifacts (smart quotes, em
  // dashes) pass through unchanged.
  for (const seg of timing.segments) {
    const p = document.createElement("p");
    p.dataset.start = seg.start;
    p.dataset.end   = seg.end;
    p.dataset.speaker = seg.speaker;
    if (seg.speaker && seg.speaker !== "narrator" && seg.speaker !== "?") {
      p.classList.add("dialogue");
    }
    // Render emphasis tags as italics; strip everything else.
    const html = String(seg.text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/&lt;emphasis&gt;/g, "<em>")
      .replace(/&lt;\/emphasis&gt;/g, "</em>");
    p.innerHTML = html;
    el.appendChild(p);
    timedSegments.push({ node: p, start: seg.start, end: seg.end });
  }
}

// ---------- Position indicator -------------------------------------------

function buildPositionMap() {
  positionSegments = [];
  const root = $("chapter-text");

  if (root.classList.contains("fallback-text")) {
    const lines = root.textContent.split(/\n/);
    root.innerHTML = "";
    let cursor = 0;
    for (const line of lines) {
      if (line.trim()) {
        const span = document.createElement("p");
        span.textContent = line;
        root.appendChild(span);
        positionSegments.push({ node: span, start: cursor, end: cursor + line.length });
        cursor += line.length;
      }
    }
    totalTextLength = cursor;
    return;
  }

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
  let hit = null;
  if (timedSegments.length) {
    // Exact path: each <p> has known start/end seconds.
    const t = audio.currentTime;
    for (const seg of timedSegments) {
      if (seg.start <= t && t < seg.end) { hit = seg; break; }
    }
    if (!hit && t >= timedSegments[timedSegments.length - 1].end) {
      hit = timedSegments[timedSegments.length - 1];
    }
  } else if (totalTextLength && audio.duration) {
    // Heuristic fallback for chapters without timing.json.
    const frac = audio.currentTime / audio.duration;
    const target = Math.floor(frac * totalTextLength);
    for (const seg of positionSegments) {
      if (seg.start <= target && target <= seg.end) { hit = seg; break; }
    }
    if (!hit) hit = positionSegments[positionSegments.length - 1];
  }
  if (!hit || hit.node === lastHighlighted) return;
  if (lastHighlighted) lastHighlighted.classList.remove("now");
  hit.node.classList.add("now");
  lastHighlighted = hit.node;
  // The read-along panel scrolls internally now. Compute the highlighted
  // paragraph's offset relative to the panel's viewport and only scroll
  // when it falls outside the comfortable middle band.
  const container = hit.node.closest(".readalong") || document.scrollingElement;
  const cBox = container.getBoundingClientRect();
  const r = hit.node.getBoundingClientRect();
  const topGuard = 60;
  const bottomGuard = 80;
  if (r.top < cBox.top + topGuard || r.bottom > cBox.bottom - bottomGuard) {
    hit.node.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// ---------- View switching -----------------------------------------------

function goLibrary() {
  audio.pause();
  $("view-library").hidden = false;
  $("view-player").hidden = true;
  $("player-bar").hidden = true;
  $("pb-pulltab").hidden = true;
  document.body.classList.remove("in-player-view");
  $("crumbs").textContent = "Library";
  $("nav-library").classList.add("active");
  $("nav-player").classList.remove("active");
  loadLibrary();           // refresh: progress may have advanced
  window.scrollTo({ top: 0 });
}

function goPlayer() {
  $("view-library").hidden = true;
  $("view-player").hidden = false;
  $("player-bar").hidden = false;
  document.body.classList.add("in-player-view");
  // Restore drawer state (collapsed/expanded persists across sessions).
  applyDrawerState(getDrawerCollapsed());
  $("nav-player").classList.add("active");
  $("nav-library").classList.remove("active");
  window.scrollTo({ top: 0 });
}

// ---------- Drawer (player-bar collapse / expand) ------------------------
// Two states: expanded (default) and collapsed (slides off-screen, leaves
// a small pull-tab visible). Drawer state persists across sessions.

const DRAWER_KEY = "drawerCollapsed";
function getDrawerCollapsed() {
  return localStorage.getItem(DRAWER_KEY) === "1";
}
function setDrawerCollapsed(v) {
  localStorage.setItem(DRAWER_KEY, v ? "1" : "0");
}
function applyDrawerState(collapsed) {
  const bar = $("player-bar");
  const tab = $("pb-pulltab");
  const handle = $("pb-handle");
  bar.classList.toggle("collapsed", collapsed);
  document.body.classList.toggle("drawer-collapsed", collapsed);
  tab.hidden = !collapsed;
  if (handle) {
    handle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    handle.setAttribute("aria-label",
      collapsed ? "Show player controls" : "Hide player controls");
    handle.title = collapsed ? "Show controls" : "Hide controls";
  }
}
function toggleDrawer() {
  const next = !getDrawerCollapsed();
  setDrawerCollapsed(next);
  applyDrawerState(next);
}

// Mini progress display on the pull-tab — updates from the same audio events.
function updatePullTabMini() {
  const mini = $("pb-pulltab-mini");
  if (!mini) return;
  if (!isFinite(audio.duration) || audio.duration <= 0) {
    mini.textContent = currentChapter ? `CH ${String(currentChapter).padStart(2,"0")}` : "—";
    return;
  }
  mini.textContent = `${fmt(audio.currentTime)} / ${fmt(audio.duration)}`;
}

// Click handlers
$("pb-handle").addEventListener("click", e => {
  // If a drag finished, the click handler from the swipe should not also
  // toggle. The drag code sets a flag we read here.
  if (e.currentTarget._suppressClick) {
    e.currentTarget._suppressClick = false;
    return;
  }
  toggleDrawer();
});
$("pb-pulltab").addEventListener("click", () => {
  setDrawerCollapsed(false);
  applyDrawerState(false);
});

// Touch / pointer drag — swipe down to collapse, swipe up to expand.
(function attachDrawerDrag() {
  const handle = $("pb-handle");
  const bar = $("player-bar");
  let startY = null;
  let lastY = null;

  function onDown(y) {
    startY = lastY = y;
    bar.classList.add("dragging");
  }
  function onMove(y) {
    if (startY == null) return;
    lastY = y;
    const dy = y - startY;
    // Allow downward drag in expanded state, upward drag if collapsed.
    if (!bar.classList.contains("collapsed") && dy > 0) {
      bar.style.transform = `translateY(${dy}px)`;
    } else if (bar.classList.contains("collapsed") && dy < 0) {
      bar.style.transform = `translateY(calc(100% + ${dy}px))`;
    }
  }
  function onUp() {
    if (startY == null) return;
    const dy = (lastY ?? startY) - startY;
    bar.style.transform = "";
    bar.classList.remove("dragging");
    const moved = Math.abs(dy);
    // Threshold: 40px decides the toggle. Below that it's a tap.
    if (moved > 40) {
      handle._suppressClick = true;   // don't double-fire on tap-end
      const wasCollapsed = bar.classList.contains("collapsed");
      const newCollapsed = wasCollapsed ? dy > -40 ? true : false : dy > 40 ? true : false;
      setDrawerCollapsed(newCollapsed);
      applyDrawerState(newCollapsed);
    }
    startY = lastY = null;
  }

  handle.addEventListener("pointerdown", e => {
    handle.setPointerCapture(e.pointerId);
    onDown(e.clientY);
  });
  handle.addEventListener("pointermove", e => {
    if (startY != null) onMove(e.clientY);
  });
  handle.addEventListener("pointerup", onUp);
  handle.addEventListener("pointercancel", onUp);

  // Pull-tab also supports an upward swipe to expand.
  const tab = $("pb-pulltab");
  let tabStartY = null, tabLastY = null;
  tab.addEventListener("pointerdown", e => {
    tab.setPointerCapture(e.pointerId);
    tabStartY = tabLastY = e.clientY;
  });
  tab.addEventListener("pointermove", e => {
    if (tabStartY != null) tabLastY = e.clientY;
  });
  tab.addEventListener("pointerup", () => {
    if (tabStartY != null && (tabStartY - (tabLastY ?? tabStartY)) > 30) {
      // Already handled by click; nothing extra needed beyond suppressing dups.
    }
    tabStartY = tabLastY = null;
  });
})();

// ---------- View mode (read / now-playing / picture) --------------------

const MODE_KEY = "playerMode";
const VALID_MODES = ["read", "now-playing", "picture"];
function getMode() {
  const saved = localStorage.getItem(MODE_KEY);
  return VALID_MODES.includes(saved) ? saved : "read";
}
function setMode(m) {
  if (!VALID_MODES.includes(m)) m = "read";
  localStorage.setItem(MODE_KEY, m);
  applyMode(m);
}
function applyMode(m) {
  document.body.classList.toggle("mode-now-playing", m === "now-playing");
  document.body.classList.toggle("mode-picture",     m === "picture");
  document.querySelectorAll(".mode-seg-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.mode === m);
    if (b.dataset.mode === m) b.setAttribute("aria-selected", "true");
    else b.removeAttribute("aria-selected");
  });
  if (m === "now-playing") refreshNowPlayingStage();
  if (m === "picture")     refreshPictureStage();
}
document.querySelectorAll(".mode-seg-btn").forEach(b => {
  b.addEventListener("click", () => setMode(b.dataset.mode));
});

// ---------- Picture stage -----------------------------------------------
// Manhwa/webnovel-style scroll: continuous full-width image flow, with
// inline chapter dividers. Future chapters' images are spoiler-blocked.
// Cached per book so re-entering is instant.
let picCache = { bookId: null, images: null };
let picRevealed = new Set();   // section keys the user has chosen to reveal

async function refreshPictureStage() {
  if (!currentBook) return;
  const scroll = $("pic-scroll");
  if (picCache.bookId !== currentBook.id) {
    picCache = { bookId: currentBook.id, images: null };
    picRevealed = new Set();
  }
  if (!picCache.images) {
    scroll.innerHTML = `<div class="pic-loading">Loading illustrations…</div>`;
    try {
      const r = await fetch(`/api/images/${currentBook.id}`);
      picCache.images = r.ok ? await r.json() : [];
    } catch (e) {
      picCache.images = [];
    }
  }
  renderPictureStage(picCache.images);
}

// Group key for a single image entry — used to bucket the linear list back
// into front-matter / per-chapter / back-matter sections for spoiler logic.
function picKey(it) {
  if (it.section === "front") return "front";
  if (it.section === "back")  return "back";
  return `ch${it.chapter}`;
}
function picStatus(key, userCh) {
  if (key === "front") return "front";
  if (key === "back")  return "back";
  const ch = parseInt(key.slice(2));
  if (ch <  userCh) return "past";
  if (ch === userCh) return "current";
  return "future";
}
function picIsBlocked(key, status) {
  if (status === "front") return false;             // always show front matter
  if (status === "future") return !picRevealed.has(key);
  if (status === "back")   return !picRevealed.has(key);
  return false;
}

function renderPictureStage(images) {
  const scroll = $("pic-scroll");
  if (!images.length) {
    scroll.innerHTML = `<div class="pic-empty">No illustrations found in this book.</div>`;
    return;
  }
  // Cumulative chapter timestamps from currentBook.chapters.
  const startsAt = {};
  let cum = 0;
  for (const c of (currentBook?.chapters || [])) {
    startsAt[c.num] = cum;
    cum += c.duration_seconds || 0;
  }
  const userCh = currentChapter || 0;

  // Walk linearly; emit a section divider whenever the bucket changes,
  // then either an image or (once) a spoiler blocker for the bucket.
  const parts = [];
  let lastKey = null;
  let blockedKey = null;   // suppress further images in this bucket
  for (const it of images) {
    const key = picKey(it);
    if (key !== lastKey) {
      const status = picStatus(key, userCh);
      const blocked = picIsBlocked(key, status);
      // Front matter has no divider — the cover speaks for itself.
      if (key !== "front") parts.push(renderPicDivider(key, it, status, startsAt));
      if (blocked) {
        parts.push(renderPicBlocker(key, it, status));
        blockedKey = key;
      } else {
        blockedKey = null;
      }
      lastKey = key;
    }
    if (blockedKey === key) continue;
    parts.push(renderPicImage(it));
  }
  scroll.innerHTML = parts.join("");

  // Reveal handler.
  scroll.querySelectorAll(".pic-blocker").forEach(b => {
    b.addEventListener("click", () => {
      picRevealed.add(b.dataset.key);
      renderPictureStage(images);
    });
  });
}

function renderPicDivider(key, sample, status, startsAt) {
  let title, sub;
  if (key === "front") {
    title = "Front matter";
    sub = "Cover · color inserts · before the story";
  } else if (key === "back") {
    title = "Back matter";
    sub = "After the final chapter";
  } else {
    const ch = parseInt(key.slice(2));
    title = `Chapter ${ch}`;
    const t = startsAt[ch];
    sub = (t != null && t > 0) ? `~${fmtDur(t)} in` : "Start of book";
  }
  const statusLabel = {
    front: "", past: "Read", current: "Now reading",
    future: "Spoilers ahead", back: "End matter",
  }[status] || "";
  return `
    <div class="pic-divider ${status}" data-key="${key}">
      <span class="pic-divider-title">${title}</span>
      ${statusLabel ? `<span class="pic-divider-status">${statusLabel}</span>` : ""}
      <span class="pic-divider-sub">${sub}</span>
    </div>`;
}

function renderPicImage(it) {
  // Aspect ratio reserves layout space so the page doesn't jump as images
  // load lazily.
  const ratio = (it.width && it.height)
    ? `style="aspect-ratio:${it.width}/${it.height}"` : "";
  return `<img class="pic-img" src="${it.src}" alt="${escapeAttr(it.alt)}" loading="lazy" ${ratio}>`;
}

function renderPicBlocker(key, sample, status) {
  const label = status === "back" ? "End-of-book art" : `Chapter ${sample.chapter}`;
  return `
    <button class="pic-blocker" data-key="${key}">
      <span class="pic-blocker-warn">Spoilers ahead</span>
      <span class="pic-blocker-msg">${label} hasn't been reached yet</span>
      <span class="pic-blocker-hint">Tap to reveal these illustrations</span>
    </button>`;
}

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function refreshNowPlayingStage() {
  if (!currentBook) return;
  $("np-author").textContent = currentBook.author || "";
  $("np-title").textContent  = currentBook.title || "";
  const chNum = currentChapter || (currentBook.chapters?.[0]?.num ?? "—");
  $("np-chapter").textContent = `Chapter ${chNum}`;
  // Cover: SVG fallback underneath, real cover image overlaid. /api/book/<id>
  // doesn't return has_cover, so always try to load and let onerror clean up
  // when the EPUB has none. Mirror the library's library-card markup exactly.
  const cover = $("np-cover");
  cover.innerHTML = coverSVG(currentBook)
    + `<img class="cover-img" alt=""
         src="/api/cover/${currentBook.id}"
         onload="this.classList.add('loaded')"
         onerror="this.remove()">`;
}

$("nav-library").addEventListener("click", e => { e.preventDefault(); goLibrary(); });
$("nav-player").addEventListener("click",  e => {
  e.preventDefault();
  if (currentBook) goPlayer();
});

// ---------- Transport controls -------------------------------------------

const playBtn = $("play-btn");
const playIcon = $("play-icon");
function setPlayIcon(playing) {
  playIcon.innerHTML = playing
    ? `<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>`
    : `<path d="M8 5.5v13a1 1 0 0 0 1.52.86l10.5-6.5a1 1 0 0 0 0-1.72L9.52 4.64A1 1 0 0 0 8 5.5z"/>`;
  playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
}
playBtn.addEventListener("click", () => {
  if (audio.paused) audio.play(); else audio.pause();
});
$("skip-back").addEventListener("click", () => {
  audio.currentTime = Math.max(0, audio.currentTime - 30);
});
$("skip-fwd").addEventListener("click", () => {
  audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 30);
});

// Seek slider — value is 0..1000 for smooth dragging. Mode-aware: in book
// mode the slider represents the whole book; if the user scrubs past the
// current chapter boundary, defer the chapter switch to release time so
// we don't reload audio on every input event mid-drag.
const seek = $("seek-slider");
seek.addEventListener("input", () => {
  updateSliderFill(seek);
  const frac = parseFloat(seek.value) / 1000;
  if (progressMode === "book" && currentBook) {
    const total = bookTotalSeconds();
    if (total <= 0) return;
    const targetSec = frac * total;
    const target = chapterAtBookTime(targetSec);
    if (!target) return;
    if (target.num === currentChapter) {
      audio.currentTime = target.time;
      pendingChapterChange = null;
    } else {
      pendingChapterChange = target;
      // Live preview: show the would-be book position without audio jump.
      $("time-current").textContent = fmt(targetSec);
    }
  } else if (audio.duration) {
    audio.currentTime = frac * audio.duration;
  }
});
seek.addEventListener("change", () => {
  if (pendingChapterChange) {
    const t = pendingChapterChange;
    pendingChapterChange = null;
    playChapter(t.num, t.time, !audio.paused);
  }
});

// Speed slider + presets
const speed = $("speed-slider");
speed.addEventListener("input", () => setSpeed(speed.value));
document.querySelectorAll(".pb-speed .presets button").forEach(b => {
  b.addEventListener("click", () => setSpeed(b.dataset.speed));
});

// Volume slider
const volume = $("volume-slider");
volume.addEventListener("input", () => setVolume(volume.value / 100));

// Bookmark
const bmBtn = $("bookmark-btn");
bmBtn.addEventListener("click", async () => {
  if (!currentBook || !currentChapter) return;
  const txt = $("chapter-text").textContent || "";
  const ratio = audio.duration ? (audio.currentTime / audio.duration) : 0;
  const startChar = Math.floor(ratio * txt.length);
  const snippet = txt.slice(startChar, startChar + 80).replace(/\s+/g, " ").trim();
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
  bmBtn.classList.remove("flash");
  void bmBtn.offsetWidth;
  bmBtn.classList.add("flash");
  loadBookmarks();
});

// ---------- Bookmarks ---------------------------------------------------

async function loadBookmarks() {
  if (!currentBook) return;
  const bms = await fetch(`/api/bookmarks/${currentBook.id}`).then(r => r.json());
  renderBookmarks(bms);
}

function renderBookmarks(bms) {
  const ul = $("bookmark-list");
  if (!bms.length) {
    ul.innerHTML = `<li class="empty">No bookmarks yet. Tap the flag in the player bar to save a moment.</li>`;
    $("bookmark-count").textContent = 0;
    return;
  }
  ul.innerHTML = bms.map((bm, i) => `
    <li data-i="${i}" data-ch="${bm.chapter}" data-time="${bm.time}">
      <span class="bm-icon">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 3h12v18l-6-4-6 4z"/></svg>
      </span>
      <span class="bm-body">
        <span class="bm-label">${bm.label || `Chapter ${bm.chapter}`}</span>
        <span class="bm-meta">Ch ${bm.chapter} · ${fmt(bm.time)}</span>
      </span>
      <button class="bm-delete" aria-label="Remove bookmark">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </li>
  `).join("");
  $("bookmark-count").textContent = bms.length;

  ul.querySelectorAll("li").forEach(li => {
    li.addEventListener("click", e => {
      if (e.target.closest(".bm-delete")) return;
      playChapter(parseInt(li.dataset.ch), parseFloat(li.dataset.time), true);
    });
  });
  ul.querySelectorAll(".bm-delete").forEach(btn => {
    btn.addEventListener("click", async e => {
      e.stopPropagation();
      const idx = parseInt(btn.closest("li").dataset.i);
      await fetch(`/api/bookmarks/${currentBook.id}/${idx}`, { method: "DELETE" });
      loadBookmarks();
    });
  });
}

// ---------- Progress mode (chapter / book) ------------------------------
// "chapter" = seek bar shows current chapter (default, prior behavior).
// "book"    = seek bar shows position across the whole book; dragging
//             past a chapter boundary triggers a chapter switch on release.

let progressMode = localStorage.getItem("progressMode") || "chapter";
let pendingChapterChange = null;

function bookTotalSeconds() {
  return (currentBook?.chapters || [])
    .reduce((s, c) => s + (c.duration_seconds || 0), 0);
}
function bookTimeAtChapter(chNum, t) {
  let cum = 0;
  for (const c of (currentBook?.chapters || [])) {
    if (c.num === chNum) return cum + (t || 0);
    cum += c.duration_seconds || 0;
  }
  return 0;
}
function chapterAtBookTime(bookSec) {
  let cum = 0;
  for (const c of (currentBook?.chapters || [])) {
    const dur = c.duration_seconds || 0;
    if (bookSec < cum + dur) return { num: c.num, time: bookSec - cum };
    cum += dur;
  }
  const last = currentBook?.chapters?.[currentBook.chapters.length - 1];
  return last ? { num: last.num, time: last.duration_seconds || 0 } : null;
}
function fmtRate(r) {
  // Trim trailing zeros: 1.00 → 1, 2.70 → 2.7, 3.20 → 3.2
  return (Math.round(r * 100) / 100).toString().replace(/\.?0+$/, "") + "×";
}
function applyProgressMode() {
  const btn = $("prog-mode");
  if (btn) {
    btn.classList.toggle("book", progressMode === "book");
    btn.textContent = progressMode === "book" ? "Book" : "Chapter";
  }
  updateUi();
}
$("prog-mode").addEventListener("click", () => {
  progressMode = progressMode === "book" ? "chapter" : "book";
  localStorage.setItem("progressMode", progressMode);
  applyProgressMode();
});

// ---------- Audio events + progress save --------------------------------

function updateUi() {
  setPlayIcon(!audio.paused);
  let cur, total;
  if (progressMode === "book" && currentBook) {
    cur   = bookTimeAtChapter(currentChapter || 0, audio.currentTime);
    total = bookTotalSeconds();
  } else {
    cur   = audio.currentTime;
    total = audio.duration;
  }
  $("time-current").textContent = fmt(cur);
  $("time-total").textContent   = fmt(total);
  if (total > 0 && isFinite(total)) {
    seek.value = (cur / total) * 1000;
    updateSliderFill(seek);
  }
  // ETA: divides remaining time by playback rate. Total/current stay at 1×.
  const eta = $("eta");
  if (eta && total > 0 && isFinite(total)) {
    const remaining = Math.max(0, total - cur);
    const rate = audio.playbackRate || 1;
    const at1x = remaining / rate;
    eta.innerHTML = remaining < 1
      ? "Done"
      : `${fmtDur(at1x) || `${Math.ceil(at1x)}s`} left <span class="rate">(${fmtRate(rate)})</span>`;
  } else if (eta) {
    eta.textContent = "—";
  }
  updatePullTabMini();
}

audio.addEventListener("timeupdate",     updateUi);
audio.addEventListener("play",           () => { updateUi(); updatePositionIndicator(); });
audio.addEventListener("pause",          () => { updateUi(); updatePositionIndicator(); });
audio.addEventListener("seeked",         updatePositionIndicator);
audio.addEventListener("loadedmetadata", () => { updateUi(); updatePositionIndicator(); });
audio.addEventListener("ended",          () => {
  const nums = currentBook.chapters.map(c => c.num);
  const idx = nums.indexOf(currentChapter);
  const next = nums[idx + 1];
  if (next != null) playChapter(next);
});

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

// Keyboard: space=pause, arrows=skip, b=bookmark
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.code === "Space")           { e.preventDefault(); playBtn.click(); }
  else if (e.code === "ArrowLeft")  $("skip-back").click();
  else if (e.code === "ArrowRight") $("skip-fwd").click();
  else if (e.code === "KeyB")       bmBtn.click();
});

// Boot
applyMode(getMode());
applyProgressMode();
loadLibrary();
