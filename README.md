# pdf-to-markdown

Convert PDFs (and other documents) to LLM-friendly Markdown using a two-stage pipeline:

1. **MarkItDown** (with the `markitdown-ocr` plugin) extracts text and images from the PDF.
2. **Cleanup** (deterministic pre-clean → parallel LLM cleanup → deterministic post-process) fixes artifacts and produces clean, structured Markdown with plain-text equations and semantic figure descriptions.

The result is Markdown optimized for downstream LLMs — minimal tokens, maximum clarity, lossless semantic compression.

## How it works

```
PDF → MarkItDown (text + image extraction) → pre_clean (regex) → parallel LLM cleanup → post_process (regex) → final .md
```

1. Loads configuration from `.config.json` and/or environment variables.
2. Initializes MarkItDown with an OpenAI-compatible client pointed at your LLM API.
3. Tests the API connection (lists available models, warns if the configured model is missing).
4. For each file in `input_directory`:
   - Converts it with MarkItDown (text extraction + optional image OCR).
   - Pre-cleans: strips `<latexit>` tags, publisher page artifacts, standalone page numbers (regex, no LLM needed).
   - Chunks the text if it exceeds the context window.
   - Sends all chunks **concurrently** to the LLM with a cleanup system prompt that fixes fake tables, broken word spacing, shattered equations, figure artifacts, headers/footers, and more.
   - Post-processes: deduplicates titles/figures, normalizes heading levels, removes garbled lines.
   - Writes the cleaned result to `output/<filename>.md`.
   - Also writes the raw pre-cleanup output to `output/<filename>.raw.md` for comparison.
   - Files that already have a newer `.md` output are skipped.
   - On the first conversion error the script stops (fail-fast).

## Prerequisites

- Python 3.10+

### Setup

The `markitdown/` source tree is **gitignored** — it is not included in this repo. You need to clone it and install it (plus the OCR plugin) into a local virtualenv.

**1. Clone MarkItDown next to this script:**

```bash
git clone https://github.com/microsoft/markitdown.git markitdown
```

**2. Create and activate a virtualenv:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**3. Install MarkItDown (editable, from source):**

```bash
pip install -e markitdown/packages/markitdown
```

**4. Install the OCR plugin (editable, from source):**

The OCR plugin (`markitdown-ocr`) replaces the built-in text-only PDF converter with one that extracts embedded images and sends them to your LLM for transcription. It also falls back to full-page rendering + OCR for scanned PDFs.

```bash
pip install -e markitdown/packages/markitdown-ocr
```

This pulls in the extra dependencies needed for image extraction (`PyMuPDF`, `Pillow`, etc.).

**5. Install the OpenAI client:**

```bash
pip install openai
```

### Verify the installation

```bash
python3 -c "from markitdown import MarkItDown; from markitdown_ocr import PdfConverterWithOCR; print('OK')"
```

If that prints `OK`, both MarkItDown and the OCR plugin are correctly installed.

## Configuration

The tool works with **any OpenAI-compatible API** — point `base_url` at the provider of your choice (see the table below). API credentials can come from **environment variables** (recommended, safe for GitHub) or from a local JSON file (`.config.json`). Environment variables always take precedence, so you can keep a `.config.json` with non-secret settings and provide the key via env.

### Supported providers

Any endpoint implementing the OpenAI Chat Completions API works, including vision (images are sent as `image_url` data URIs):

| Provider | `base_url` |
|----------|------------|
| OpenAI | `https://api.openai.com/v1` |
| Anthropic | `https://api.anthropic.com/v1/` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Together | `https://api.together.xyz/v1` |
| Mistral | `https://api.mistral.ai/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| Ollama (local) | `http://localhost:11434/v1` |
| vLLM (local) | `http://localhost:8000/v1` |
| LM Studio (local) | `http://localhost:1234/v1` |

Note: the cleanup stage fires up to `cleanup_max_workers` (default 8) concurrent requests. Providers with strict rate limits may return 429s; requests are retried with exponential backoff (`api.max_retries`, default 5), but lower `cleanup_max_workers` if you still see failures.

### Option A: Environment variables (recommended)

```bash
export LLM_API_KEY="sk-..."
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_MODEL="your-model-name"
# Optional: override the input/output directories at runtime
export LLM_INPUT_DIR="input"
export LLM_OUTPUT_DIR="output"
```

You can put these in a `.env` file (gitignored) and `source .env` before running (see `.env.example`).

### Option B: `.config.json`

Copy the template and fill in your values:

```bash
cp .config.example.json .config.json
```

`.config.json` is **gitignored** so your key never gets committed. Required fields:

```json
{
  "api": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "YOUR_API_KEY_HERE",
    "model": "your-model-name",
    "max_retries": 5
  },
  "processing": {
    "input_directory": "input",
    "output_directory": "output",
    "llm_prompt": "You are analyzing an image extracted from a document. Transcribe all text exactly as shown. If the image is a diagram, chart, or figure, describe its components, connections, labels, and spatial layout in a structured way. If it contains math or formulas, write them in plain text notation (e.g., E[T_R] = 1/(mu_R - b_R)). Return only the transcription and description, nothing else.",
    "cleanup_enabled": true
  }
}
```

You can mix both approaches: e.g. keep `base_url`, `model`, `input_directory`, `output_directory`, and `llm_prompt` in the JSON file, and provide only `LLM_API_KEY` via env.

#### Config fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `api.base_url` | Yes | — | OpenAI-compatible API endpoint (see provider table above) |
| `api.api_key` | Yes | — | API key (or via `LLM_API_KEY` env var) |
| `api.model` | Yes | — | Model name for both image OCR and text cleanup |
| `api.max_retries` | No | `5` | Retries on 429/5xx/connection errors with exponential backoff (OpenAI SDK behavior, honors Retry-After) |
| `processing.input_directory` | Yes | — | Directory containing files to convert (relative to repo root) |
| `processing.output_directory` | No | `output` | Directory for Markdown output (relative to repo root) |
| `processing.llm_prompt` | Yes | — | Instruction sent to the LLM alongside each extracted image |
| `processing.cleanup_enabled` | No | `true` | Whether to run cleanup after MarkItDown extraction |
| `processing.cleanup_mode` | No | `"llm"` | Cleanup strategy: `"auto"` (detect), `"llm"` (always LLM, parallelized), `"fast"` (always regex), `"off"` (none) |
| `processing.cleanup_auto_threshold` | No | `0.5` | Artifact score per 1K chars above which auto-mode chooses LLM cleanup |
| `processing.cleanup_prompt` | No | built-in | Override the cleanup system prompt (see below) |
| `processing.cleanup_chunk_chars` | No | `48000` | Max characters per chunk for the cleanup LLM call. Larger chunks mean fewer chunks, fewer prompt repetitions, and lower token usage. Set lower if your model has a small context window. |
| `processing.cleanup_max_workers` | No | `8` | Max concurrent LLM requests during cleanup. Higher = faster but uses more API connections. Lower it for endpoints with strict rate limits. |

#### The `llm_prompt` field

This prompt is sent to the LLM alongside each extracted image (by the OCR plugin). It controls how the model describes figures, diagrams, and images. Adjust it to suit your document types. The default above is tuned for technical documents with diagrams and math.

#### The `cleanup_prompt` field

This prompt is sent as a **system message** during the cleanup stage. It instructs the LLM how to clean the raw MarkItDown output. The script has a comprehensive built-in default (see `DEFAULT_CLEANUP_PROMPT` in `pdf-to-markdown.py`) that handles:

1. Converting fake tables (extraction artifacts) back to prose
2. Removing PDF headers, footers, page numbers, and publisher layout artifacts
3. Removing extraction garbage (base64 blobs, XML tags)
4. Normalizing Markdown headings (consistent hierarchy for both textbooks and assignments)
5. Preserving section numbering (chapter numbers like `2.5.3` or task labels like `I.A.1`)
6. Reconstructing broken equations in clean plain-text notation (Unicode symbols, underscore subscripts)
7. Replacing broken figure content with concise semantic descriptions (components + relationships)
8. Removing redundant whitespace and fixing run-together words
9. Keeping genuine tables as Markdown tables
10. Preserving document structure and references

The goal: **lossless semantic compression — minimum tokens while preserving all unambiguous meaning and relationships**.

To use a custom cleanup prompt, add `cleanup_prompt` to the `processing` section of your config.

## Input / output folders

Both live inside the repo so everything is self-contained:

- **`input/`** — drop the PDFs/documents you want to convert here.
- **`output/`** — generated `.md` files are written here:
  - `<filename>.md` — final cleaned Markdown
  - `<filename>.raw.md` — raw MarkItDown output (pre-cleanup), for comparison

Paths in the config are resolved **relative to the repo root** (the script's directory) unless you give an absolute path. The folder contents are gitignored (only a `.gitkeep` placeholder is tracked), so your documents and generated Markdown won't be committed.

## Running

```bash
# Activate the environment
source .venv/bin/activate

# (If using env vars) export them, or source a .env file
export LLM_API_KEY="sk-..."

# Drop your documents into input/
# then run:
python3 pdf-to-markdown.py
```

Output Markdown files appear in `output/` (e.g. `input/report.pdf` -> `output/report.md`).

To re-convert a file, delete the existing `.md` (and `.raw.md`) from `output/` first (the script skips files that already have a newer output).

## Repository layout

```
pdf-to-markdown/
├── pdf-to-markdown.py            # main script (extraction + cleanup pipeline)
├── .config.example.json          # template (committed)
├── .config.json                  # your real config (gitignored, you create it)
├── .gitignore
├── .venv/                        # Python virtualenv (you create it)
├── input/                        # drop source documents here (contents gitignored)
│   └── .gitkeep
├── output/                       # generated .md and .raw.md files (contents gitignored)
│   └── .gitkeep
└── markitdown/                   # MarkItDown source (gitignored, you clone it)
    └── packages/
        ├── markitdown/           # core package
        └── markitdown-ocr/        # OCR plugin (PDF image extraction + LLM)
```

## How the OCR plugin works

The built-in MarkItDown PDF converter does **text extraction only** — it ignores embedded images and never calls the LLM. The `markitdown-ocr` plugin replaces it with a converter that:

1. Extracts text from the PDF text layer (using `pdfplumber`/`pdfminer`).
2. Extracts embedded images from each page (using `PyMuPDF`/`pdfplumber`).
3. Sends each extracted image to your LLM vision model (using the `llm_prompt` as the instruction).
4. Interleaves the OCR/description results with the text by vertical position.
5. For scanned PDFs with no text layer, renders full pages as images and OCRs them.

**Limitation:** Vector graphics (diagrams drawn with PDF drawing commands, not raster images) are not extracted as images by the plugin. For PDFs where the figures are vector-based, the text layer is used as-is and figures may not be described by the OCR plugin. However, the LLM cleanup stage will still attempt to turn the broken figure text into a concise semantic description from whatever fragments are present in the text layer.

## How the cleanup stage works

After MarkItDown extracts text (and optionally OCRs images), the raw output typically contains artifacts: fake tables, run-together words, shattered equations, page headers/footers, and extraction garbage like `<latexit>` base64 blobs. The cleanup stage fixes these using one of three modes, chosen automatically or via config:

### Cleanup modes

| Mode | Config value | How it works | Speed | Best for |
|------|-------------|--------------|-------|----------|
| **LLM** (default) | `"llm"` | Pre-clean → parallel LLM cleanup → post-process | ~10-12 min for 500-page books | Most documents |
| **Auto** | `"auto"` | Scores the artifact level, then chooses fast or LLM | Varies | Mixed document sets |
| **Fast** | `"fast"` | Regex-only cleanup, no LLM calls | ~seconds | Very clean PDFs, offline use |
| **Off** | `"off"` | No cleanup | instant | Raw extraction only |

### Auto-detection

In auto mode, the script scores the raw extraction using `assess_artifacts()`:
- **Latexit tags**: 5 pts each (strong indicator of a dirty PDF)
- **Run-together word lines**: 2 pts each (extraction spacing failure)
- **Fake table rows** (>50% empty cells): 0.1 pts each

The score is normalized to **points per 1,000 characters** and compared to `cleanup_auto_threshold` (default: `0.5`). If the score is at or above the threshold, LLM mode is used; otherwise, fast mode is used.

### Fast mode (regex-only)

Conservative deterministic cleanup — no LLM calls, takes seconds even for 1,000+ page books:
1. Pre-clean: strip `<latexit>` tags, publisher page artifacts (all variants), standalone page numbers
2. Remove single-char noise lines (letters only, not math symbols or table cells)
3. Detect shattered-equation tables (math operators + mostly empty cells) and join them to prose
4. Remove standalone figure axis-label fragments (e.g., `A[V]`, `t[s]`)
5. Collapse multiple blank lines

Does NOT do: heading normalization, figure semantic descriptions, equation reconstruction, fake-table conversion. Use LLM mode for best quality.

### LLM mode (full cleanup, parallelized)

1. **Pre-clean** (regex, no LLM): Removes `<latexit>` tags, publisher page-artifact lines (e.g., `02Chap01 8804 (1-24) 30/5/03 11:03 am Page 12`), standalone page numbers, collapses blank lines.

2. **Chunk** (if needed): Splits the pre-cleaned text into chunks under `cleanup_chunk_chars` (default 48,000 chars), respecting paragraph boundaries.

3. **Parallel LLM cleanup**: All chunks are sent to the LLM **concurrently** via a thread pool (`cleanup_max_workers`, default 8). Each chunk receives the cleanup system prompt. With 8 workers a 500-page book (~27 chunks at 48K chars) cleans in ~10-12 minutes instead of ~80 sequentially; a 2-chunk document takes under a minute.

4. **Stitch**: Joins cleaned chunks back together in order.

5. **Post-processing** (deterministic, regex): A final pass fixes remaining issues:
   - Removes duplicate h1 titles (keeps first occurrence).
   - Normalizes heading levels (task labels `I.A.1` → `###`, section numbers `N.N` → `###`, `N.N.N` → `####`).
   - Deduplicates figure descriptions (keeps first, replaces subsequent with references).
   - Removes garbled lines (lines dominated by single-character words, excluding tables, equations, and math symbols).
   - Collapses multiple blank lines.

The raw pre-cleanup output is always saved as `<filename>.raw.md` so you can compare before/after and verify the cleanup quality.

## Security notes

- `.config.json` and `.env` are listed in `.gitignore` — never commit real secrets.
- Before pushing, double-check with `git status` that no secret files are staged.
- If a key is ever accidentally committed, rotate it immediately and scrub the history (`git filter-repo` or BFG).
