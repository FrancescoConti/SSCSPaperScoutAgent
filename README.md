# isscc-guidelines-agent — Conference Paper Scout

An agent that mines one or more solid-state-circuits conferences (**ISSCC** by
default, or any of **VLSI** / **CICC** / **ASSCC** / **ESSERC**, or `all`) across a
range of years for papers related to one or more **theme topics**, scores each paper
by relevance per topic using LLM judgment, and exports an **ODS spreadsheet** with one
score column per topic.

Each surviving paper gets a **score in `[0, 1]` per topic**:
- **`1.0`** — the paper is directly about that theme (e.g. theme *LLM* → an *LLM
  inference accelerator*; scores 1.0 even if the literal string "LLM" is absent, as
  long as it is unambiguously about it).
- **`0 < x < 1`** — the keyword doesn't appear (or only in passing), but the paper is
  judged genuinely related to that theme, graded by how strongly.
- Papers below the keep threshold (default `0.3`, shared across every topic) are
  excluded from the statistics — but they stay listed, because **the thresholds live
  in the spreadsheet**, not in the export. A paper never scored against a given topic
  (e.g. it came from a different `--in` file) gets an **empty** cell for that topic,
  not `0` — `0` means "judged unrelated."

The workbook is organized for **cross-topic, cross-venue statistics** (no charts —
that's out of scope):

- **`Papers`** — one row per paper scored on at least one topic, sorted by best score:
  `Paper ID · Title · Authors · Year · Venue key · Venue · Sources · Link · Best score
  · Best topic · Kept (any)`, then a `Score: T · Relevance: T · Reason: T` block per
  topic, in CLI order. `Relevance`, `Best score`, `Best topic` and `Kept (any)` are
  **formulas** driven by the Config sheet. `Sources` lists which indexes confirmed the
  paper. Links are clickable (open-access PDF where available, otherwise a DOI
  resolving to IEEE Xplore).
- **`Summary`** — tidy long form, one row per `Topic × Venue × Year`: `Fetched ·
  Matched · Direct · Strong · Moderate · Weak · Avg score · Matched share`, computed
  **live** with `COUNTIFS`/`AVERAGEIFS` over Papers, plus `(all)` roll-up rows per
  venue, per year, and grand-total, for every topic. The roll-up rows are **not**
  blank-separated — filter out any row where Venue and/or Year read `(all)` before
  pivoting, or you will double-count.
- **`Config`** — the four band cut-offs (Direct / Strong / Moderate / Keep) as editable
  yellow cells, **shared by every topic's Score column**. Type a new number and every
  topic's Papers bands and the entire Summary re-derive themselves; no re-run, no
  re-scoring.
- **`Meta`** — one row per topic (its recall query), one row per venue (description +
  papers fetched), which sources were cross-checked, and **papers without an abstract
  per venue** — so a coverage gap (VLSI, chiefly) is visible on the sheet, not
  discovered by surprise.

## How it works

Three deterministic Python scripts do the API calls and file I/O; the **agent** does
the relevance scoring (that's the part only judgment can do):

```
theme(s) + venue(s) + years
        │
        ▼
scripts/fetch_candidates.py   ── Semantic Scholar Graph API ──▶  candidates.json
        │                        one bulk pass per venue          (title, abstract,
        │                        (--all: one pass total,           tldr, authors,
        │                         topics checked locally)           links, per-topic
        │                                                            keyword_hits)
        ▼
scripts/crosscheck_sources.py ── dblp / OpenAlex / Crossref ───▶  crosscheck.json
        │                        (+ IEEE Xplore with a key),        + missing papers
        │                        one index PER VENUE so a           merged back in,
        │                        title can't cross venues           venue_key stamped
        ▼
  AGENT scores EVERY candidate for EVERY topic  ─────────────▶  candidates.scored.json
  in one pass over the paper (never N passes)
        │
        ▼
scripts/build_ods.py          ── formulas, not baked-in cutoffs, ─▶  results.ods
                                  N topic blocks, --in repeatable
                                  to merge several scored files
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
>
> Use paper-scout to survey LLM and compute-in-memory papers across ISSCC, VLSI, and
> CICC 2021-2024.

The subagent will expand each theme, fetch candidates across the requested venue(s),
score every candidate for every theme in one pass, and drop `results.ods` in the
working directory. It only asks you for genuinely missing inputs (theme(s), years) and
for the API key if it isn't set up yet.

### Manually (if you want to drive the scripts yourself)

```bash
# 1. Fetch candidates — one or more venues, one or more topics.
#    Either a keyword-recall subset (default, single topic only)...
python3 scripts/fetch_candidates.py \
  --venue ISSCC --years 2021-2024 \
  --topic 'LLM=("LLM" | "large language model" | "transformer" | "attention")' \
  --out candidates.json

# ...or EVERY paper in the venue(s)/years (for complete surveys, statistics, and
#    whenever more than one topic is in play -- topics are free in this mode):
python3 scripts/fetch_candidates.py \
  --venue ISSCC,VLSI,CICC,ASSCC,ESSERC --years 2021-2024 \
  --topic 'LLM=("LLM" | "large language model")' \
  --topic 'CIM=("compute-in-memory")' \
  --all --out candidates.json

# 2. Cross-check that Semantic Scholar's list is complete, per venue, and fold in
#    what it missed (--merge). Essential when the count itself is the result --
#    and mandatory for VLSI, which S2 barely indexes at all (see below).
python3 scripts/crosscheck_sources.py \
  --venue ISSCC,VLSI,CICC,ASSCC,ESSERC --years 2021-2024 \
  --in candidates.json --out crosscheck.json --merge

# 3. Score them: edit candidates.json, filling `scores[topic]` (0..1) and
#    `reasons[topic]` for every topic, for every paper (this is the judgment step
#    the agent automates -- one pass over the paper, all topics at once).

# 4. Build the spreadsheet (--in is repeatable, to merge several scored files;
#    --threshold only seeds the editable Config cell)
python3 scripts/build_ods.py --in candidates.json --out results.ods --threshold 0.3
```

### Cross-checking coverage

Semantic Scholar indexes the citation graph, not conference programs, so its venue
metadata for these conferences has gaps — for VLSI, essentially total (see below).
`crosscheck_sources.py` independently enumerates the same venue(s)/years from sources
that *do* index programs, and reports what S2 missed, **per venue**:

| source | default | key needed | notes |
|--------|---------|-----------|-------|
| `dblp` | ✅ | — | the most reliable conference-program index |
| `crossref` | ✅ | — | DOI registry; sees whatever IEEE deposited |
| `ieee` | — | `IEEE_XPLORE_API_KEY` | authoritative, but institutional |
| `openalex` | — | — (set `OPENALEX_MAILTO` for the polite pool) | broad open catalogue, but see caveat below |

Default `--sources dblp,crossref`. Each venue gets its own lookup index, built only
from that venue's own candidates, so a fuzzy title match can never cross venues.
Papers are matched by DOI, then by normalized title, then by token overlap (blocked by
year internally, so it stays fast across many venues and years); proceedings front
matter (session overviews, indexes, committee pages) is filtered out so it can't
inflate the denominator. `crosscheck.json` gives `{"by_venue": {...}, "rollup": {...}}`
— per-venue and rolled-up `coverage` figures and the full list of missing papers;
`--merge` appends them to `candidates.json`, stamped with `venue_key` and `null`
scores for every topic, for scoring (they arrive **title-only**, with no abstract).
Every source is optional and failures are non-fatal — but if none is reachable for a
venue, treat that venue's coverage as *unverified* rather than confirmed.

> **VLSI caveat:** a direct check of Semantic Scholar found **6 records total, ever**,
> under any of VLSI's known venue aliases — essentially no S2 coverage. For VLSI, the
> dblp/Crossref cross-check with `--merge` is not an optional audit; it is the *only*
> way papers enter the corpus at all, and they arrive title-only. A VLSI run showing
> "0 candidates from Semantic Scholar" is expected, not a bug — don't report it as "no
> papers found" without running the cross-check first.

> **A-SSCC caveat:** Semantic Scholar has mismerged this venue's `publicationVenue`
> entity with an unrelated "International Symposium on Security in Computing and
> Communications" — the raw `venue` string on A-SSCC candidates reads that way, not
> "Asian Solid-State Circuits Conference". Matching still works, because that (mis-
> merged) entity's `alternate_names` correctly lists "A-SSCC" / "Asian Solid-State
> Circuits Conference", which the venue filter checks. This is a Semantic Scholar
> data-quality bug, not cross-venue contamination — see the `_note` on the `ASSCC`
> entry in `scripts/venues.json`.

> **OpenAlex caveat:** it is off by default because its venue metadata for IEEE
> conferences is poor — many ISSCC works are present in OpenAlex with no
> `primary_location` at all, so enumerating by venue silently under-reports and its
> `coverage` number would be misleading. It stays available (`--sources ...,openalex`)
> for venues OpenAlex does index properly.

## Supported conferences

Defined in `scripts/venues.json` — name aliases, venue-verification substrings, the
per-source query hints used by the cross-check, an optional `cli_aliases` list (so
`--venue ESSCIRC` resolves to `ESSERC`, `--venue A-SSCC` to `ASSCC`), and
**`venue_exclude_substrings`**, which keep sibling conferences out. That last one is
not optional bookkeeping: *IEEE **Asian** Solid-State Circuits Conference* (A-SSCC)
contains ISSCC's `"solid-state circuits conference"` substring, and without the
exclusion an ISSCC survey quietly absorbs A-SSCC's entire program.

| key       | conference |
|-----------|------------|
| `ISSCC`   | IEEE International Solid-State Circuits Conference |
| `VLSI`    | IEEE Symposium on VLSI Technology and Circuits (see coverage caveat above) |
| `CICC`    | IEEE Custom Integrated Circuits Conference |
| `ASSCC`   | IEEE Asian Solid-State Circuits Conference (see coverage caveat above) |
| `ESSERC`  | IEEE European Solid-State Electronics Research Conference (formerly ESSCIRC) |

`--venue` accepts a comma list of these keys (or their `cli_aliases`), or the literal
`all`. Add more venues by editing `scripts/venues.json`.

## Scope & limitations

- **Text depth:** For these paywalled IEEE-adjacent conferences the deepest text
  available programmatically is **title + abstract + TLDR**. These abstracts are
  dense and specific, so they support solid judgment, but this is not full-text
  mining. If you have PDFs (via your institution), you can ask the agent to read
  specific ones to refine borderline scores. The pipeline never scrapes or bypasses
  paywalls.
- **Coverage** depends on Semantic Scholar's indexing of the venue; the venue filter
  is verified locally against known name variants. If a run returns too few papers —
  or zero, which `fetch_candidates.py` now warns about explicitly — rerun with
  `--no-venue-filter` or a broader query, and use `crosscheck_sources.py` to quantify
  the gap instead of guessing at it. **VLSI is close to unindexed in Semantic Scholar
  outright** (see the cross-check section above) — treat a zero-candidate VLSI result
  as expected, not broken.
- **Merged-in papers are title-only.** dblp and Crossref expose no abstracts, so
  papers recovered by the cross-check are scored from their titles alone — weaker
  evidence than the S2 abstract+TLDR path, and the `reason` should say so. For VLSI
  this is the norm, not the exception: essentially every VLSI paper in the corpus
  arrives this way.
- **Scores are per topic, calibrated together.** When multiple `--topic` values are in
  play, the agent scores every topic for a paper in one pass, using the shared Config
  thresholds — a "0.7" on one topic and a "0.7" on another are meant to be
  comparable, since Summary compares them side by side.
- **IEEE Xplore API** access is optional and only useful with an institutional
  key (`IEEE_XPLORE_API_KEY`); default DOI links already resolve to Xplore.
