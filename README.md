# isscc-guidelines-agent — Conference Paper Scout

An agent that mines a solid-state-circuits conference (**ISSCC** by default, or
**VLSI** / **ESSERC**) across a range of years for papers related to a **theme
keyword**, scores each paper by relevance using LLM judgment, and exports an **ODS
spreadsheet**.

Each surviving paper gets a **score in `[0, 1]`**:
- **`1.0`** — the paper is directly about the theme (e.g. theme *LLM* → an *LLM
  inference accelerator*; scores 1.0 even if the literal string "LLM" is absent, as
  long as it is unambiguously about it).
- **`0 < x < 1`** — the keyword doesn't appear (or only in passing), but the paper is
  judged genuinely related, graded by how strongly.
- Papers below the threshold (default `0.3`) are **removed** from the spreadsheet.

Spreadsheet columns: **Paper ID · Title · Authors · Link · Score · Reason · Year ·
Venue**. Links are clickable (open-access PDF where available, otherwise a DOI that
resolves to IEEE Xplore).

## How it works

Two deterministic Python scripts do the API calls and file I/O; the **agent** does the
relevance scoring (that's the part only judgment can do):

```
keyword + venue + years
        │
        ▼
scripts/fetch_candidates.py   ── Semantic Scholar Graph API ──▶  candidates.json
        │                                                         (title, abstract,
        │                                                          tldr, authors,
        │                                                          links, keyword_hit)
        ▼
  AGENT scores each paper (score + reason)  ─────────────────▶  candidates.scored.json
        │
        ▼
scripts/build_ods.py          ── drops sub-threshold / unscored ─▶  results.ods
```

The agent instructions live in **[`AGENT.md`](AGENT.md)**; the Claude Code subagent
definition is **[`.claude/agents/paper-scout.md`](.claude/agents/paper-scout.md)**.

## Setup (one time)

```bash
pip install -r requirements.txt

# Semantic Scholar API key — free, request at:
#   https://www.semanticscholar.org/product/api#api-key-form
# It arrives by email (often a day or two). Then:
cp .env.example .env      # paste your key into .env
source .env
```

Without a key the pipeline still runs, but on the shared, heavily rate-limited pool.

## Usage

### Via the agent (recommended, minimal interaction)

In Claude Code, ask for it — e.g.:

> Use paper-scout to find ISSCC 2021–2024 papers about compute-in-memory.

The subagent will expand the keyword, fetch candidates, score them, and drop
`results.ods` in the working directory. It only asks you for genuinely missing inputs
(keyword, years) and for the API key if it isn't set up yet.

### Manually (if you want to drive the scripts yourself)

```bash
# 1. Fetch candidates — either a keyword-recall subset (default)...
python3 scripts/fetch_candidates.py \
  --venue ISSCC --years 2021-2024 \
  --keyword "LLM" \
  --query '("LLM" | "large language model" | "transformer" | "attention")' \
  --out candidates.json

# ...or EVERY paper in the venue/years (for complete surveys & statistics):
python3 scripts/fetch_candidates.py \
  --venue ISSCC --years 2021-2024 --keyword "LLM" --all --out candidates.json

# 2. Score them: edit candidates.json, filling `score` (0..1) and `reason`
#    for each paper (this is the judgment step the agent automates).

# 3. Build the spreadsheet
python3 scripts/build_ods.py --in candidates.json --out results.ods --threshold 0.3
```

## Supported conferences

Defined in `scripts/venues.json` (name aliases + venue-verification substrings):

| key       | conference |
|-----------|------------|
| `ISSCC`   | IEEE International Solid-State Circuits Conference |
| `VLSI`    | IEEE Symposium on VLSI Technology and Circuits |
| `ESSERC`  | IEEE European Solid-State Electronics Research Conference (formerly ESSCIRC) |

Add more by editing `scripts/venues.json`.

## Scope & limitations

- **Text depth:** For paywalled IEEE conferences the deepest text available
  programmatically is **title + abstract + TLDR**. These abstracts are dense and
  specific, so they support solid judgment, but this is not full-text mining. If you
  have PDFs (via your institution), you can ask the agent to read specific ones to
  refine borderline scores. The pipeline never scrapes or bypasses paywalls.
- **Coverage** depends on Semantic Scholar's indexing of the venue; the venue filter
  is verified locally against known name variants. If a run returns too few papers,
  rerun `fetch_candidates.py` with `--no-venue-filter` or a broader `--query`.
- **IEEE Xplore API** enrichment is optional and only useful with an institutional
  key (`IEEE_XPLORE_API_KEY`); default DOI links already resolve to Xplore.
