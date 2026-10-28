# AGENTS.md

## What this is

Single-script Python tool (`pdf-to-markdown.py`) converting PDFs to Markdown for **LLM consumption** (retrieval/QA corpora) — explicitly NOT for human reading. Output goal is lossless semantic compression at minimum tokens.

Two-stage pipeline: MarkItDown extraction (with OCR plugin) → cleanup. Cleanup default is LLM mode with parallelized chunk processing.

## Running the script

```bash
source .venv/bin/activate
python3 pdf-to-markdown.py
```

Must use `python3`, not `python` — the system has no `python` alias.

The script skips files that already have a newer `.md` in `output/`. To re-convert, delete BOTH the `.md` AND the `.raw.md` first (skip check only looks at the `.md`, but leaving a stale `.raw.md` invites confusion).

`process_files()` is fail-fast: the first conversion error exits with `sys.exit(1)`. A failed multi-chunk run leaves no partial output to clean up.

## Timing expectations (measured, Mistral-Medium-3.5-128B on IDUN, 8 workers)

- 500-page textbook (1.26M chars → 27 chunks @ 48K): ~10 min MarkItDown extraction+OCR, then ~11 min parallel LLM cleanup. Per-chunk LLM time is ~2.5–3 min; sequential would be ~80 min, parallelization gives ~7×.
- Small document (2 chunks): ~45 s cleanup.
- **Wall time for large books is dominated by MarkItDown extraction + OCR, not the LLM cleanup.** Don't optimize the wrong stage.

## Setup gotchas

- `markitdown/` is a **gitignored external clone** of Microsoft's markitdown repo. It is NOT included. Must `git clone` it and `pip install -e` both `markitdown/packages/markitdown` and `markitdown/packages/markitdown-ocr` into `.venv`.
- The virtualenv lives at `.venv/` (repo root), not `markitdown/.venv/`.
- The OCR plugin (`markitdown-ocr`) must be installed — without it, the built-in PDF converter does text-only extraction and never calls the LLM for images.
- Verify install with: `python3 -c "from markitdown import MarkItDown; from markitdown_ocr import PdfConverterWithOCR; print('OK')"`
- If the script can't import `openai`, the package is missing from `.venv` — `pip install openai`.

## Config and secrets

- `.config.json` holds the API key and is **gitignored**. Never commit it.
- Env vars (`LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_INPUT_DIR`, `LLM_OUTPUT_DIR`) override config file values.
- `.config.example.json` is the committed template; `.env.example` documents the env vars.
- Subtlety: shipped configs set `"cleanup_mode": "llm"`, but the **in-code fallback** (in `process_files()`) is `'auto'` if the key is missing. Don't assume "llm" without checking config.
- Any OpenAI-compatible endpoint works (base_url is configurable — OpenAI, Anthropic, Gemini, OpenRouter, Groq, Ollama, vLLM, ...). No provider-specific code; the client is always `openai.OpenAI`, built by `create_client()`.
- Concurrency: parallel cleanup fires up to `cleanup_max_workers` (default 8) concurrent requests. Retries with backoff are built in (`api.max_retries`, default 5), but rate-limited endpoints may still need a lower worker count.

## Architecture: cleanup pipeline

Modes via `cleanup_mode` in config:

- **LLM mode** (default in shipped config): `cleanup_markdown()` = `pre_clean()` → chunk (48K chars) → **all chunks concurrently** via `ThreadPoolExecutor` (`cleanup_max_workers`, default 8) with `DEFAULT_CLEANUP_PROMPT` as system message → stitch → `post_process_cleanup()`.
- **Fast mode** (regex only): `regex_cleanup()` = `pre_clean()` → single-char noise removal → shattered-equation table joining → figure axis-label fragment removal. Seconds for any size, but quality is far below LLM mode (only use for clean PDFs).
- **Auto mode**: `assess_artifacts()` scores raw extraction (latexit = 5 pts, garbled lines = 2 pts, fake table rows = 0.1 pts, per 1K chars) vs `cleanup_auto_threshold` (0.5). Above → LLM, below → fast.

There is **no cross-chunk context** — chunks are independent. Consistency comes from the shared system prompt plus `post_process_cleanup()` (dedupes h1 titles, normalizes task/section heading levels, fixes missing spaces in numeric headings, dedupes `**Figure N:**` descriptions, drops garbled lines).

Key functions: `pre_clean()` (deterministic: latexit + publisher page artifacts + standalone page numbers), `chunk_text()`, `cleanup_markdown()`, `post_process_cleanup()`, `regex_cleanup()`, `assess_artifacts()`, `strip_latexit()`.

## Hard output constraints

- **Equations in plain text with Unicode symbols** (π, φ, ω, ∑, underscore subscripts `R_1`): `g(t) = A sin(2πft + φ)`. **NEVER LaTeX** (`$$`, `\sin`, `\pi`) — user requirement; LaTeX wastes tokens for downstream LLMs.
- Figure descriptions must be semantic (components + relationships), prefixed `**Figure N:**`.
- Don't summarize content — compression only. Structure (sections, numbering, references) must survive.

## Editing conventions

- No comments in code unless explicitly requested.
- `DEFAULT_CLEANUP_PROMPT` at the top of the file is the cleanup system prompt — keep it concise. Overridable via `config['processing']['cleanup_prompt']` without code changes.
- `.raw.md` is only written when `cleanup_enabled` is true.

## Known imperfections after LLM cleanup (not bugs to chase)

- Occasional residual LaTeX fragments (2 lines in a 631K-char book) despite the prompt rule.
- A few fake-table rows may survive (7 of ~5000 in the reference book).

## Output files

- `output/<name>.md` — final cleaned Markdown
- `output/<name>.raw.md` — raw MarkItDown output (pre-cleanup), for before/after comparison
- Both gitignored (only `.gitkeep` tracked)
