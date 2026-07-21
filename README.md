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
- Papers below the keep threshold (default `0.3`) are excluded from the statistics —
  but they stay listed, because **the thresholds live in the spreadsheet**, not in the
  export.

The workbook is organized for **topic statistics** (no charts — that's out of scope):

- **`Papers`** — one tidy, pivot-ready row per scored paper: `Topic · Paper ID · Title ·
  Authors · Year · Venue · Score · Relevance · Direct · Kept · Sources · Reason · Link`.
  `Topic` is the keyword (stack several single-topic exports into one dataset for
  cross-topic stats), `Year` is numeric, `Score` a float. `Relevance` (band), `Direct`
  and `Kept` (1/0 flags) are **formulas** driven by the Config sheet. `Sources` lists
  which indexes confirmed the paper. Links are clickable (open-access PDF where
  available, otherwise a DOI resolving to IEEE Xplore).
- **`Summary`** — a per-year cross-tab (`Fetched · Matched · Direct · Strong ·
  Moderate · Weak · Avg score · Matched share`) with a TOTAL row, computed **live**
  with `COUNTIFS`/`AVERAGEIFS` over the Papers sheet. In `--all` mode "Matched share"
  is the fraction of the whole conference program that dealt with the topic.
- **`Config`** — the four band cut-offs (Direct / Strong / Moderate / Keep) as editable
  yellow cells. Type a new number and the Papers bands and the entire Summary re-derive
  themselves; no re-run, no re-scoring.
- **`Meta`** — the run parameters and which sources were cross-checked, so each export
  is self-describing.

## How it works

Three deterministic Python scripts do the API calls and file I/O; the **agent** does
the relevance scoring (that's the part only judgment can do):

```
keyword + venue + years
        │
        ▼
scripts/fetch_candidates.py   ── Semantic Scholar Graph API ──▶  candidates.json
        │                                                         (title, abstract,
        │                                                          tldr, authors,
        │                                                          links, keyword_hit)
        ▼
scripts/crosscheck_sources.py ── dblp / OpenAlex / Crossref ───▶  crosscheck.json
        │                        (+ IEEE Xplore with a key)        + missing papers
        │                                                            merged back in
        ▼
  AGENT scores each paper (score + reason)  ─────────────────▶  candidates.scored.json
        │
        ▼
scripts/build_ods.py          ── formulas, not baked-in cutoffs ─▶  results.ods
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

# 2. Cross-check that Semantic Scholar's list is complete, and fold in what it
#    missed (--merge). Essential when the count itself is the result.
python3 scripts/crosscheck_sources.py \
  --venue ISSCC --years 2021-2024 \
  --in candidates.json --out crosscheck.json --merge

# 3. Score them: edit candidates.json, filling `score` (0..1) and `reason`
#    for each paper (this is the judgment step the agent automates).

# 4. Build the spreadsheet (--threshold only seeds the editable Config cell)
python3 scripts/build_ods.py --in candidates.json --out results.ods --threshold 0.3
```

### Cross-checking coverage

Semantic Scholar indexes the citation graph, not conference programs, so its venue
metadata for these conferences has gaps. `crosscheck_sources.py` independently
enumerates the same venue/years from sources that *do* index programs, and reports
what S2 missed:

| source | default | key needed | notes |
|--------|---------|-----------|-------|
| `dblp` | ✅ | — | the most reliable conference-program index |
| `crossref` | ✅ | — | DOI registry; sees whatever IEEE deposited |
| `ieee` | — | `IEEE_XPLORE_API_KEY` | authoritative, but institutional |
| `openalex` | — | — (set `OPENALEX_MAILTO` for the polite pool) | broad open catalogue, but see caveat below |

Default `--sources dblp,crossref`. Papers are matched by DOI, then by normalized
title, then by token overlap; proceedings front matter (session overviews, indexes,
committee pages) is filtered out so it can't inflate the denominator.
`crosscheck.json` gives a per-source `coverage` figure and the full list of missing
papers; `--merge` appends them to `candidates.json` for scoring (they arrive
**title-only**, with no abstract). Every source is optional and failures are
non-fatal — but if none is reachable, treat the coverage as *unverified* rather than
confirmed.

> **OpenAlex caveat:** it is off by default because its venue metadata for IEEE
> conferences is poor — many ISSCC works are present in OpenAlex with no
> `primary_location` at all, so enumerating by venue silently under-reports and its
> `coverage` number would be misleading. It stays available (`--sources ...,openalex`)
> for venues OpenAlex does index properly.

## Supported conferences

Defined in `scripts/venues.json` — name aliases, venue-verification substrings, the
per-source query hints used by the cross-check, and **`venue_exclude_substrings`**,
which keep sibling conferences out. That last one is not optional bookkeeping: *IEEE
**Asian** Solid-State Circuits Conference* (A-SSCC) contains ISSCC's
`"solid-state circuits conference"` substring, and without the exclusion an ISSCC
survey quietly absorbs A-SSCC's entire program.

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
  rerun `fetch_candidates.py` with `--no-venue-filter` or a broader `--query`, and use
  `crosscheck_sources.py` to quantify the gap instead of guessing at it.
- **Merged-in papers are title-only.** dblp and Crossref expose no abstracts, so
  papers recovered by the cross-check are scored from their titles alone — weaker
  evidence than the S2 abstract+TLDR path, and the `reason` should say so.
- **IEEE Xplore API** access is optional and only useful with an institutional
  key (`IEEE_XPLORE_API_KEY`); default DOI links already resolve to Xplore.
