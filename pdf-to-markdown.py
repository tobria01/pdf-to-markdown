import json
import os
import re
import sys
import concurrent.futures
from pathlib import Path
from markitdown import MarkItDown
from openai import OpenAI

# Default cleanup prompt — sent as a system message to the LLM.
# Overridable via config['processing']['cleanup_prompt'].
DEFAULT_CLEANUP_PROMPT = """You are a document cleanup assistant. Clean raw Markdown extracted from a PDF for optimal use by downstream LLMs. Return ONLY the cleaned Markdown.

Rules:
1. Convert fake tables (extraction artifacts with paragraph text in cells) to prose. Keep only genuine tabular data as Markdown tables.
2. Remove PDF headers, footers, page numbers, running titles (e.g., reversed text like "sesicrexE"), and publisher layout artifacts.
3. Remove extraction garbage: base64 blobs, XML/MathML tags, encoding artifacts.
4. Normalize headings: "#" for document title only, "##" for top-level sections (chapters or tasks), "###" for subsections, "####" for sub-subsections. Detect the document's numbering scheme and be consistent.
5. Preserve section numbering — whether task labels (I.A.1, Task I.B) or chapter/section numbers (1.1, 2.5.3). These are structural references.
6. Reconstruct broken equations into clean plain-text notation. Use Unicode symbols directly (π, φ, ω, ∑, ∫, λ, μ). Use underscore for subscripts (R_1, f_n) and caret for superscripts (x^2). Keep inline where possible. Fix equations shattered into fake tables.
7. Replace broken figure content with "**Figure N:** [description]". For diagrams, describe components AND their relationships/connections, not just a list of labels. Do not invent details. Always prefix with **Figure N:**.
8. Fix run-together words, collapse blank lines, remove excessive spaces.
9. Preserve genuine tabular data as clean Markdown tables.
10. Maintain document structure: section ordering, numbering, and reference lists.
11. Remove orphaned figure labels (standalone lines like "IED 3", "L20", axis labels like "A[V]", "t[s]") that can't be integrated into descriptions.

Goal: lossless semantic compression — minimum tokens while preserving all unambiguous meaning and relationships. Do not summarize. Do not add commentary — output only the cleaned Markdown."""

def load_config(config_path):
    """Load configuration from JSON file."""
    if not config_path.exists():
        print(f"Error: Configuration file {config_path} not found")
        sys.exit(1)
    
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        return config
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in configuration file: {e}")
        sys.exit(1)

def apply_env_overrides(config):
    """Override config values with environment variables when present.

    Supported env vars (take precedence over the JSON config):
        LLM_API_KEY    -> config['api']['api_key']
        LLM_BASE_URL   -> config['api']['base_url']
        LLM_MODEL      -> config['api']['model']
        LLM_INPUT_DIR  -> config['processing']['input_directory']
        LLM_OUTPUT_DIR -> config['processing']['output_directory']
    """
    config.setdefault('api', {})
    config.setdefault('processing', {})

    env_map = {
        'LLM_API_KEY': ('api', 'api_key'),
        'LLM_BASE_URL': ('api', 'base_url'),
        'LLM_MODEL': ('api', 'model'),
        'LLM_INPUT_DIR': ('processing', 'input_directory'),
        'LLM_OUTPUT_DIR': ('processing', 'output_directory'),
    }

    for env_name, (section, key) in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[section][key] = value
            print(f"  {env_name} -> {section}.{key} (from environment)")

    return config

def validate_config(config):
    """Validate required configuration parameters.

    API credentials may be supplied either via the JSON config file or via the
    corresponding LLM_* environment variables (see apply_env_overrides).
    """
    if 'processing' not in config:
        print("Error: Missing required configuration section: processing")
        sys.exit(1)

    processing_keys = ['input_directory', 'llm_prompt']
    for key in processing_keys:
        if key not in config['processing']:
            print(f"Error: Missing required processing configuration: {key}")
            sys.exit(1)

    # output_directory is optional; defaults to the repo's ./output folder.
    config['processing'].setdefault('output_directory', None)

    # API section is optional in the file itself as long as env vars provide it.
    config.setdefault('api', {})
    api_keys = ['base_url', 'api_key', 'model']
    for key in api_keys:
        if key not in config['api'] or not config['api'][key]:
            print(
                f"Error: Missing required API configuration: {key}. "
                f"Provide it in .config.json or via the LLM_* environment variables."
            )
            sys.exit(1)

def create_client(config):
    """Create an OpenAI-compatible client for the configured endpoint.

    Works with any provider implementing the OpenAI Chat Completions API.
    max_retries enables the SDK's built-in retry with exponential backoff
    on 429/5xx/connection errors.
    """
    return OpenAI(
        base_url=config['api']['base_url'],
        api_key=config['api']['api_key'],
        max_retries=config['api'].get('max_retries', 5),
    )

def initialize_markitdown(config):
    """Initialize MarkItDown with the configured LLM."""
    try:
        client = create_client(config)

        md = MarkItDown(
            llm_client=client,
            llm_model=config['api']['model'],
            llm_prompt=config['processing']['llm_prompt']
        )
        return md
    except Exception as e:
        print(f"Error: Failed to initialize MarkItDown with LLM: {e}")
        sys.exit(1)

def test_api_connection(md, config):
    """Test if the LLM API is accessible."""
    try:
        print("Testing LLM API connection...")
        print(f"API Endpoint: {config['api']['base_url']}")
        print(f"Using Model: {config['api']['model']}")
        
        # Attempt to fetch available models to verify Connection
        client = md._llm_client if hasattr(md, '_llm_client') else None
        if client and hasattr(client, 'models'):
            try:
                models = client.models.list()
                model_names = [model.id for model in models.data]
                if config['api']['model'] not in model_names:
                    print(f"Warning: Model '{config['api']['model']}' not found in available models")
                    print(f"Available models: {model_names[:5]}...")  # Show first 5
                else:
                    print(f"SUCCESS: Model '{config['api']['model']}' is available")
            except Exception as e:
                print(f"Warning: Could not fetch available models: {e}")
        
        print("LLM API connection successful")
        return True

    except Exception as e:
        print(f"Error: LLM API connection failed: {e}")
        print("Please check your API key and network connection")
        sys.exit(1)

def strip_latexit(content):
    """Remove <latexit> tags and their base64-encoded contents from text.

    These tags are artifacts of a LaTeX-in-PDF tool; the base64 content is
    not decodable to readable text and just wastes tokens.
    """
    return re.sub(r'<latexit[^>]*>.*?</latexit>', '', content, flags=re.DOTALL)

def pre_clean(content):
    """Deterministic pre-cleaning applied before chunking and LLM/regex cleanup.

    Strips obvious PDF-layout artifacts that require no semantic understanding:
    1. <latexit> tags and their base64 contents (via strip_latexit)
    2. Publisher page-artifact lines, e.g. "02Chap01 8804 (1-24) 30/5/03 11:03 am Page 12"
    3. Standalone page numbers (1-3 digits on their own line)
    4. Collapses multiple blank lines

    Runs before chunking so the LLM never sees these artifacts, saving tokens
    and improving cleanup quality. Called by both cleanup_markdown() and regex_cleanup().
    """
    # 1. Strip latexit tags
    content = strip_latexit(content)

    # 2. Remove publisher page-artifact lines.
    #    Generalized pattern: "NN<Name>  MMMM (range) D/M/YY H:MM am/pm Page NN"
    #    Catches Prelims, Chap, Appendix, and any future variant.
    content = re.sub(
        r'^\d{2}\w+\s+\d{4}\s+\([^)]*\)\s+\d+/\d+/\d+\s+\d+:\d+\s*[ap]m\s+Page\s+\w+.*$',
        '', content, flags=re.MULTILINE
    )

    # 3. Remove standalone page numbers (1-3 digits on their own line)
    content = re.sub(r'^\d{1,3}$', '', content, flags=re.MULTILINE)

    # 4. Collapse multiple blank lines to max 2
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content

def chunk_text(text, max_chars=48000):
    """Split text into chunks under max_chars, respecting paragraph boundaries.

    Splits on double newlines first, then falls back to single newlines and
    finally to hard character limits for very long paragraphs.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    paragraphs = text.split('\n\n')
    current_chunk = ""

    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 <= max_chars:
            current_chunk = current_chunk + ('\n\n' if current_chunk else '') + para
        else:
            if current_chunk:
                chunks.append(current_chunk)
            # If a single paragraph is too long, split it by single newlines
            if len(para) > max_chars:
                lines = para.split('\n')
                line_chunk = ""
                for line in lines:
                    if len(line_chunk) + len(line) + 1 <= max_chars:
                        line_chunk = line_chunk + ('\n' if line_chunk else '') + line
                    else:
                        if line_chunk:
                            chunks.append(line_chunk)
                        # Hard split very long single lines
                        if len(line) > max_chars:
                            for i in range(0, len(line), max_chars):
                                chunks.append(line[i:i + max_chars])
                        else:
                            line_chunk = line
                if line_chunk:
                    current_chunk = line_chunk
                else:
                    current_chunk = ""
            else:
                current_chunk = para

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def cleanup_markdown(config, raw_content):
    """Clean up raw MarkItDown output via a parallelized LLM cleanup pass.

    Pipeline: pre_clean → chunk → parallel LLM cleanup → stitch → post_process.
    All chunks are sent to the LLM concurrently. Cross-chunk consistency is
    handled by the cleanup prompt rules and deterministic post-processing.
    Returns the cleaned Markdown string.
    """
    cleanup_prompt = config['processing'].get('cleanup_prompt', DEFAULT_CLEANUP_PROMPT)
    max_chars = config['processing'].get('cleanup_chunk_chars', 48000)
    max_workers = config['processing'].get('cleanup_max_workers', 8)

    # Step 1: Pre-clean (regex: strip latexit, page artifacts, standalone page numbers)
    pre_cleaned = pre_clean(raw_content)
    print(f"  Pre-cleaned: {len(raw_content)} -> {len(pre_cleaned)} chars")

    # Step 2: Chunk if necessary
    chunks = chunk_text(pre_cleaned, max_chars=max_chars)
    print(f"  Chunked into {len(chunks)} chunk(s) (max {max_chars} chars each)")

    # Step 3: LLM cleanup for each chunk, processed concurrently
    client = create_client(config)
    model = config['api']['model']

    def clean_chunk(chunk_text):
        messages = [
            {"role": "system", "content": cleanup_prompt},
            {"role": "user", "content": chunk_text},
        ]
        response = client.chat.completions.create(model=model, messages=messages)
        return response.choices[0].message.content

    if len(chunks) == 1:
        print("  Cleaning 1 chunk...")
        cleaned_chunks = [clean_chunk(chunks[0])]
    else:
        print(f"  Cleaning {len(chunks)} chunks concurrently (max {max_workers} workers)...")
        cleaned_chunks = [None] * len(chunks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(clean_chunk, chunk): i
                for i, chunk in enumerate(chunks)
            }
            completed = 0
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                cleaned_chunks[idx] = future.result()
                completed += 1
                print(f"  Completed chunk {idx + 1}/{len(chunks)} ({completed}/{len(chunks)} done)")

    # Step 4: Stitch chunks and run deterministic post-processing
    stitched = '\n\n'.join(cleaned_chunks)
    final = post_process_cleanup(stitched)

    return final


def post_process_cleanup(text):
    """Deterministic post-processing fixes applied after all chunks are stitched.

    These fixes address chunking artifacts that the LLM may not have caught:
    1. Duplicate h1 titles (keep first, remove subsequent)
    2. Garbled lines (lines dominated by single-character words, not tables/equations/headings/math)
    3. Inconsistent heading levels (normalize task labels I.A.x and section numbers N.N.x to consistent levels)
    4. Duplicate figure descriptions (keep first, reference subsequent)
    """
    lines = text.split('\n')
    result = []
    seen_h1 = set()
    seen_figures = set()

    # Math symbols that indicate a line is an equation fragment, not garbled text
    math_symbols = set('∑∫πφωλμθαβγδσΨΩΦ∂∇√≈≤≥≠∞·×±⊕⊗')

    for line in lines:
        stripped = line.strip()

        # Fix 1: Remove duplicate h1 titles
        h1_match = re.match(r'^#\s+(.+)', stripped)
        if h1_match:
            title = h1_match.group(1).strip()
            if title in seen_h1:
                continue  # skip duplicate
            seen_h1.add(title)

        # Fix 3a: Normalize task heading levels (I.A.x, I.B.x, etc. → ###)
        task_match = re.match(r'^(#{1,6})\s+((?:I|II|III|IV)\.[AB]\.\d.*)', stripped)
        if task_match:
            line = f"### {task_match.group(2)}"
            stripped = line.strip()

        # Fix 3b: Normalize section-numbered headings (N.N, N.N.N → ### or ####)
        #   Only applies to lines that are already headings (start with #).
        #   Also inserts the space between number and title when the LLM lost it ("2.2Signals" → "2.2 Signals").
        sec_match = re.match(r'^(#{1,6})\s+(\d+\.\d+\.\d+)\s*(.*)$', stripped)
        if sec_match:
            title = sec_match.group(3).strip()
            line = f"#### {sec_match.group(2)}" + (f" {title}" if title else "")
            stripped = line.strip()
        else:
            sec_match2 = re.match(r'^(#{1,6})\s+(\d+\.\d+)\s*(.*)$', stripped)
            if sec_match2:
                title = sec_match2.group(3).strip()
                line = f"### {sec_match2.group(2)}" + (f" {title}" if title else "")
                stripped = line.strip()

        # Fix 4: Deduplicate figure descriptions
        fig_match = re.match(r'^\*\*Figure\s+(\d+):\*\*', stripped)
        if fig_match:
            fig_num = fig_match.group(1)
            if fig_num in seen_figures:
                line = f"[Figure {fig_num}: see earlier description]"
                stripped = line.strip()
            else:
                seen_figures.add(fig_num)

        # Fix 4b: Clean up lines where LLM already added reference but kept the prefix
        ref_match = re.match(r'^\*\*Figure\s+(\d+):\*\*\s*\[Figure\s+\d+:\s*see earlier description\]', stripped)
        if ref_match:
            fig_num = ref_match.group(1)
            line = f"[Figure {fig_num}: see earlier description]"
            stripped = line.strip()

        # Fix 2: Remove garbled lines (not tables, not equations, not headings, not math)
        # A garbled line has >50% words of length 1-2, and at least 4 words.
        # Lines containing math symbols or $ delimiters are preserved.
        if stripped and not stripped.startswith('|') and not stripped.startswith('#') \
           and not stripped.startswith('**Figure') and not stripped.startswith('[') \
           and not stripped.startswith('E[') and not stripped.startswith('-') \
           and not stripped.startswith('$'):
            has_math = any(c in math_symbols for c in stripped)
            if not has_math:
                words = stripped.split()
                if len(words) >= 4:
                    short_words = sum(1 for w in words if len(w) <= 2)
                    if short_words > len(words) * 0.5:
                        continue  # skip garbled line

        result.append(line)

    # Collapse multiple consecutive blank lines to max 2
    cleaned = '\n'.join(result)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

    return cleaned


def assess_artifacts(raw_content):
    """Score the artifact level of raw MarkItDown output.

    Returns a score per 1000 characters. Higher = more artifacts = needs LLM cleanup.
    Used by auto-detection to choose between fast (regex) and LLM cleanup modes.

    Weights:
        - latexit tags: 5 pts each
        - run-together word lines: 2 pts each
        - fake table rows (>50% empty cells): 0.1 pts each
    """
    score = 0

    # Latexit tags
    latexit_count = len(re.findall(r'<latexit', raw_content))
    score += latexit_count * 5

    # Run-together words (lines where >30% of words are >20 chars)
    garbled_lines = 0
    for line in raw_content.split('\n'):
        stripped = line.strip()
        if stripped and not stripped.startswith('|'):
            words = stripped.split()
            if len(words) >= 5:
                long_words = sum(1 for w in words if len(w) > 20)
                if long_words > len(words) * 0.3:
                    garbled_lines += 1
    score += garbled_lines * 2

    # Fake table rows (>50% empty cells, excluding header separators)
    fake_table_rows = 0
    for line in raw_content.split('\n'):
        stripped = line.strip()
        if stripped.startswith('|') and stripped.endswith('|') and '---' not in stripped:
            cells = [c.strip() for c in stripped.split('|')[1:-1]]
            if cells:
                empty = sum(1 for c in cells if not c)
                if empty > len(cells) * 0.5:
                    fake_table_rows += 1
    score += fake_table_rows * 0.1

    # Normalize to score per 1000 chars
    char_count = max(len(raw_content), 1)
    return score / (char_count / 1000)


def _fix_shattered_equations(lines):
    """Detect table blocks that are shattered equations and join them to prose.

    A shattered equation looks like a table where most cells are empty and
    the non-empty cells contain math operators or function names (sin, cos,
    sum, pi, etc.). These are detection heuristics, not full reconstruction.
    """
    result = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith('|'):
            block = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                block.append(lines[i])
                i += 1
            if _is_shattered_equation(block):
                joined = ' '.join(
                    cell.strip()
                    for row in block
                    if '---' not in row
                    for cell in row.strip().strip('|').split('|')
                    if cell.strip()
                )
                if joined:
                    result.append(joined)
        else:
            result.append(lines[i])
            i += 1
    return result


def _is_shattered_equation(block):
    """Heuristic: does this table block look like a shattered equation?"""
    math_keywords = re.compile(r'(sin|cos|tan|log|exp|sum|\\sum|\\pi|\\phi|\\omega|=|\+|\-|/|\(|\)|∑|∫|π|φ|ω|λ|μ)')
    if len(block) < 2:
        return False
    all_cells = []
    non_empty = []
    for row in block:
        if '---' in row:
            continue
        cells = [c.strip() for c in row.strip().strip('|').split('|')]
        for cell in cells:
            all_cells.append(cell)
            if cell:
                non_empty.append(cell)
    if not all_cells:
        return False
    empty_ratio = 1 - (len(non_empty) / len(all_cells))
    if empty_ratio < 0.4:
        return False
    text = ' '.join(non_empty)
    return bool(math_keywords.search(text))


def _remove_figure_label_fragments(lines):
    """Remove standalone axis-label lines that are figure fragments.

    Matches patterns like 'A[V]', 't[s]', 'f[Hz]', 'Q = M sin ϕ' when they
    appear as short standalone lines (not inside tables, not headings).
    """
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('|') and not stripped.startswith('#') \
           and len(stripped) < 30 and not stripped.startswith('**') \
           and not stripped.startswith('-') and not stripped.startswith('['):
            if re.match(r'^\w+\[[^\]]+\]$', stripped):
                continue
            if re.match(r'^[A-Z]\s*=\s*[A-Z]\s*(sin|cos)\s*\w$', stripped):
                continue
        result.append(line)
    return result


def regex_cleanup(raw_content):
    """Conservative regex-based cleanup — no LLM calls.

    Handles common PDF extraction artifacts deterministically:
    1. Pre-clean (latexit, page artifacts, page numbers, blank lines)
    2. Remove single-char noise lines (not inside tables)
    3. Convert shattered-equation tables to joined prose lines
    4. Remove standalone figure axis-label fragments
    """
    content = pre_clean(raw_content)

    lines = content.split('\n')

    lines = _fix_shattered_equations(lines)

    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('|') and re.match(r'^[a-zA-Z]{1,2}$', stripped):
            continue
        cleaned_lines.append(line)
    lines = cleaned_lines

    lines = _remove_figure_label_fragments(lines)

    content = '\n'.join(lines)
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()


def process_files(md, input_dir, output_dir, config):
    """Process all files in the input directory, writing results to output_dir.

    If config['processing']['cleanup_enabled'] is True (default), runs an LLM
    cleanup pass after MarkItDown extraction. The raw pre-cleanup output is
    saved as <name>.raw.md for comparison.
    """
    pensum_dir = Path(input_dir)
    
    if not pensum_dir.exists():
        print(f"Error: Input directory {input_dir} does not exist")
        sys.exit(1)
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    files = [f for f in pensum_dir.iterdir() if f.is_file() and f.name != ".gitkeep" and f.name != "Mobile Communications (2nd Edition).pdf"]
    
    if not files:
        print(f"No files found in {input_dir}")
        return
    
    print(f"\nFound {len(files)} file(s) to process")
    print(f"Input:  {pensum_dir}")
    print(f"Output: {output_path}")
    print("=" * 50)
    
    success_count = 0
    error_count = 0
    
    for file_path in files:
        try:
            print(f"\nProcessing: {file_path.name}")
            print(f"File type: {file_path.suffix}")
            
            output_file = output_path / (file_path.stem + ".md")
            
            # Skip if output already exists and is newer
            if output_file.exists() and output_file.stat().st_mtime >= file_path.stat().st_mtime:
                print(f"Skipping: {output_file.name} already exists and is up to date")
                continue
            
            # Convert file with MarkItDown
            print("Converting with MarkItDown...")
            result = md.convert(str(file_path))
            raw_content = result.text_content

            # Determine if cleanup is enabled (default: True)
            cleanup_enabled = config.get('processing', {}).get('cleanup_enabled', True)

            if cleanup_enabled:
                # Save raw (pre-cleanup) output for comparison
                raw_file = output_path / (file_path.stem + ".raw.md")
                with open(raw_file, "w", encoding="utf-8") as f:
                    f.write(raw_content)
                print(f"  Saved raw extraction: {raw_file.name}")

                # Determine cleanup mode
                cleanup_mode = config.get('processing', {}).get('cleanup_mode', 'auto')
                auto_threshold = config.get('processing', {}).get('cleanup_auto_threshold', 0.5)

                if cleanup_mode == 'auto':
                    # Auto-detect artifact level
                    score = assess_artifacts(raw_content)
                    if score >= auto_threshold:
                        chosen_mode = 'llm'
                        print(f"  Artifact score: {score:.2f}/1K chars (threshold: {auto_threshold}) → LLM cleanup")
                    else:
                        chosen_mode = 'fast'
                        print(f"  Artifact score: {score:.2f}/1K chars (threshold: {auto_threshold}) → fast regex cleanup")
                else:
                    chosen_mode = cleanup_mode
                    print(f"  Cleanup mode: {chosen_mode} (from config)")

                if chosen_mode == 'off':
                    final_content = raw_content
                elif chosen_mode == 'fast':
                    print("  Running regex cleanup (fast, no LLM)...")
                    final_content = regex_cleanup(raw_content)
                else:
                    print("  Running LLM cleanup...")
                    final_content = cleanup_markdown(config, raw_content)
            else:
                final_content = raw_content

            # Write final output
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(final_content)
            
            print(f"✓ SUCCESS: Created {output_file.name}")
            success_count += 1
            
        except Exception as e:
            error_msg = f"Error processing {file_path.name}: {e}"
            print(f"✗ FAILED: {error_msg}")
            error_count += 1
            
            # Fail completely as requested
            print("\nProcessing stopped due to error.")
            print("Please check the error message and fix the issue before running again.")
            sys.exit(1)
    
    print("\n" + "=" * 50)
    print(f"Processing complete!")
    print(f"Successfully processed: {success_count} file(s)")
    print(f"Errors: {error_count}")

def main():
    print("=" * 50)
    print("PDF/Document to Markdown Converter with LLM Cleanup")
    print("=" * 50)

    # Configuration paths
    config_file = Path(__file__).parent / '.config.json'
    
    # Load configuration (file is optional if all required values come from env vars)
    if config_file.exists():
        print(f"\nLoading configuration from: {config_file}")
        config = load_config(config_file)
    else:
        print(f"\nNo config file found at {config_file}; relying on environment variables.")
        config = {'api': {}, 'processing': {}}
    
    # Apply environment variable overrides (LLM_API_KEY, LLM_BASE_URL, etc.)
    print("Applying environment variable overrides...")
    config = apply_env_overrides(config)
    validate_config(config)
    print("✓ Configuration loaded successfully")

    # Initialize MarkItDown with the configured LLM
    print("\nInitializing MarkItDown with LLM...")
    md = initialize_markitdown(config)
    print("✓ MarkItDown initialized successfully")

    # Test API connection
    print("\nTesting LLM API connection...")
    test_api_connection(md, config)
    
    # Process files
    repo_root = Path(__file__).resolve().parent
    input_directory = config['processing']['input_directory']
    if not os.path.isabs(input_directory):
        input_directory = str(repo_root / input_directory)
    
    output_directory = config['processing'].get('output_directory') or 'output'
    if not os.path.isabs(output_directory):
        output_directory = str(repo_root / output_directory)
    
    print(f"\nProcessing files from: {input_directory}")
    process_files(md, input_directory, output_directory, config)
    
    print("\nDone!")

if __name__ == "__main__":
    main()