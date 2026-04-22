# multivoice-audiobook

Convert any EPUB into a multi-voice audiobook using xAI's Grok TTS, then listen
in the bundled web player with chapter navigation, variable speed (0.5×–4×),
bookmarks, and a synchronized read-along view of the source text.

You bring your own EPUB. The tool detects characters automatically, assigns
voices by gender, and writes a single JSON config you can edit to override
specific assignments.

## Pipeline

```
EPUB ─▶ chapter HTML
      ─▶ italics preserved as <emphasis>
      ─▶ heuristic dialogue attribution (with HIGH/MED/LOW/UNK confidence)
      ─▶ per-character voice mapping (gender-matched)
      ─▶ Grok TTS (parallel, segment-cached)
      ─▶ ffmpeg concat per chapter
      ─▶ Flask web player
```

## Features

- **Heuristic dialogue attribution** with confidence scoring; on cleanly-tagged
  prose, ~90 %+ of dialogue lands at HIGH or MED confidence with no LLM call.
- **Auto cast detection**: scans the EPUB for character names from dialogue
  tags, infers gender from pronoun proximity, suggests a voice mapping. The
  resulting `cast_<book>.json` is the single source of truth — edit it to
  override anything.
- **Gender-matched voice pool**: minor characters draw from a pool that
  respects detected gender, so a female minor character never gets a male
  voice (and vice versa).
- **Per-segment caching**: every TTS call is keyed on `hash(text + voice_id)`
  and stored in `tts_cache/`. Re-runs and partial failures don't re-pay.
- **Concurrent rendering** up to Grok's published rate limits.
- **Pronunciation overrides** via a per-book JSON dict (proper nouns the TTS
  butchers — give it phonetic respellings).
- **Roman-numeral and abbreviation expansion** in preprocessing
  ("Chapter XII" → "Chapter 12", "Mr." → "Mister").
- **Bundled Flask web player**:
  - Library view (any book in `library/<book_id>/` with `chapter_NNN.mp3`)
  - Variable speed 0.5×–4× (persisted per-book + last-used default)
  - ±30 s skip (or arrow keys), play/pause (or space)
  - Bookmarks with auto-labels (or `B` key); persisted to disk
  - Read-along panel with paragraph-level position highlight
  - Optional rich EPUB content (paragraphs, italics, embedded images) via the
    source EPUB
  - Persistent per-book progress; resumes where you left off
  - Custom dark theme with thin scrollbars

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
winget install Gyan.FFmpeg                # Windows; ffmpeg required for clean concat
```

### 2. Set your Grok API key

```bash
setx XAI_API_KEY "sk-..."                 # Windows: persists across reboots
# Then close and reopen your terminal so the new env var is visible.
```

The tool also reads from the Windows user-scope env var directly, so the
key is picked up even when launched from contexts that didn't inherit it.

### 3. Build a cast file from your EPUB

```bash
python cast_builder.py --epub mybook.epub --book-id mybook
```

This produces `cast_mybook.json` with detected characters, inferred gender,
and auto-assigned voices for the top characters by frequency.
**Open it and review** — the heuristic gets gender wrong sometimes for
prominent characters whose pronouns overlap with others in dialogue scenes.

### 4. Render chapters

```bash
python render_batch.py \
  --epub mybook.epub \
  --book-id mybook \
  --chapters 1-3 \
  --multi-voice \
  --cast cast_mybook.json
```

Each chapter writes:
- `library/mybook/chapter_NNN.mp3` — the audio
- `library/mybook/chapter_NNN.txt` — the text sent to TTS (read-along fallback)
- `library/mybook/chapter_NNN.attribution.json` — speaker/voice/confidence per
  segment (debugging + future LLM tuning input)
- `library/mybook/book.json` — points the player at the source EPUB so it can
  serve rich HTML for the read-along panel

For single-voice mode, omit `--multi-voice` and `--cast`, then pass `--voice`:

```bash
python render_batch.py --epub mybook.epub --book-id mybook \
  --chapters 1-3 --voice rex
```

### 5. Listen

```bash
python player/app.py
```

Open <http://localhost:5000>.

**Access from phone / other devices** — bind to all interfaces:

```bash
python player/app.py --host 0.0.0.0
```

Then visit `http://<your-machine-ip>:5000` from any device on the same wifi.
For access from outside your home network, [Tailscale](https://tailscale.com/)
is the cleanest option — install it on your Windows machine and your phone,
then use the tailnet IP or MagicDNS name.

The UI is mobile-responsive (stacked layout on narrow screens, touch-friendly
controls); adding the player to your iOS/Android home screen works like a
standalone app.

> **Security note:** `0.0.0.0` has no auth — anyone on your network can see
> your library and bookmarks. Fine at home; not on shared wifi.

## Voice options

Five Grok voices are exposed:

| ID | Description |
|----|-------------|
| `rex` | confident, clear (default for top male character) |
| `leo` | authoritative, strong |
| `eve` | energetic, upbeat (default for top female character) |
| `ara` | warm, friendly |
| `sal` | smooth, balanced (default narrator) |

For more variety per character, see Grok's
[expressive tags](https://docs.x.ai/developers/model-capabilities/audio/voice)
(`<emphasis>`, `<whisper>`, `[sigh]`, etc.) — italics in the source EPUB are
already wrapped with `<emphasis>` automatically.

## Cost & performance

Approximate per novel-length book (~600 K characters):

- **~$2.50** in Grok API calls at $4.20 / 1M chars
- **~10–15 min** wall clock with 6-way parallelism
- **~3 hours** of audio output

Your render will be cheaper after the first attempt because the per-segment
cache means re-runs (e.g. after editing the cast file for a small subset of
characters) only pay for the changed segments.

## Architecture

| File | Role |
|------|------|
| `cast_builder.py`        | Scan EPUB → detect characters → infer gender → suggest voice mapping |
| `attribution.py`         | Split text into `[(speaker, text, confidence), ...]` |
| `text_preprocess.py`     | Pronunciation overrides, Roman numerals, abbreviation expansion |
| `html_to_marked_text.py` | Walk EPUB HTML, preserve italics as `<emphasis>` |
| `render_batch.py`        | Orchestrator: chunking, parallel TTS, ffmpeg concat, both single- and multi-voice modes |
| `player/app.py`          | Flask backend: library, audio, text, HTML, progress, bookmarks |
| `player/static/`         | Single-page HTML / JS / CSS player UI |

## Roadmap

- LLM tuning pass (qwen2.5:7b via Ollama) for low-confidence segments
- PDF input (currently EPUB only)
- Auto-suggest pronunciations for unusual proper nouns
- Crossfaded segment transitions

## Demo

Three short clips generated by this pipeline from *Pride and Prejudice*
(public domain, Project Gutenberg) live in [`demos/`](./demos/):

- [`opening.mp3`](./demos/opening.mp3) — Chapter I, the famous opening + first Bennet dialogue (35 s)
- [`lucas.mp3`](./demos/lucas.mp3) — Chapter VI, four characters at Lucas Lodge (45 s)
- [`proposal.mp3`](./demos/proposal.mp3) — Chapter XXXIV, Darcy's first proposal (60 s)

To regenerate end-to-end (~$0.07 in API calls), see
[`demos/README.md`](./demos/README.md).

## Use your own books

This tool processes EPUB files **you provide**. No books are bundled. The
demo uses *Pride and Prejudice* (public domain, via Project Gutenberg).
Please respect copyright law when using this tool on books you don't own or
don't have rights to adapt.

## License

MIT
