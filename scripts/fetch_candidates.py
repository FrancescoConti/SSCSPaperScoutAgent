#!/usr/bin/env python3
"""
Fetch candidate papers for one or more conferences + a year range from the
Semantic Scholar Graph API, so an agent can score them against one or more
themes in a single pass.

This script does the *deterministic* work only: it queries the API, filters
by venue, and emits a JSON list of candidates with everything needed to judge
relevance (title, abstract, TLDR, authors, links, per-topic keyword-hit
flags) — one fetch, one output file, however many venues and topics are in
play. Studying several themes over the same program (or the same theme
across the SSCS conference family) used to mean re-running the whole loop
once per keyword or per venue; now it is one run with a repeatable --topic
and a comma-separated (or `all`) --venue.

The actual relevance *scoring* is intentionally NOT done here — that is the
agent's job (see AGENT.md).

Usage:
    python3 fetch_candidates.py \
        --venue ISSCC,VLSI,CICC,ASSCC,ESSERC \
        --years 2012-2026 \
        --topic 'LLM=("LLM"|"large language model"|"GPT")' \
        --topic 'CIM=("compute-in-memory"|"CIM"|"SRAM macro")' \
        --all --out candidates.json

    # --venue also accepts the literal 'all' for every configured venue.
    # --topic is repeatable; a bare NAME (no '=') defaults its query to "NAME".
    # --all scans every paper in venue/years and ignores each topic's query
    # (queries only matter in recall mode); prefer --all when several themes
    # are in play, since extra topics then cost nothing extra at fetch time.

Environment:
    S2_API_KEY   Semantic Scholar API key (optional but strongly recommended).
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from common import load_venues, parse_years, resolve_venues, parse_topics

S2_BULK = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
# NOTE: the bulk-search endpoint does not support `tldr`; we fetch that
# separately via the /paper/batch endpoint below.
FIELDS = "paperId,title,abstract,year,venue,publicationVenue,authors,externalIds,openAccessPdf,url,citationCount"


def http_get(url, params, api_key, retries=5):
    headers = {"User-Agent": "paper-scout/1.0"}
    if api_key:
        headers["x-api-key"] = api_key
    full = url + "?" + urlencode(params)
    delay = 2.0
    for attempt in range(retries):
        try:
            req = Request(full, headers=headers)
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                sys.stderr.write(
                    f"  [retry {attempt+1}/{retries}] HTTP {e.code}, waiting {delay:.0f}s\n")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            body = e.read().decode("utf-8", "replace")[:300]
            sys.exit(f"HTTP {e.code} from Semantic Scholar: {body}")
        except URLError as e:
            sys.stderr.write(f"  [retry {attempt+1}/{retries}] network error: {e}\n")
            time.sleep(delay)
            delay = min(delay * 2, 30)
    sys.exit("Gave up after repeated errors from Semantic Scholar.")


def http_post(url, params, payload, api_key, retries=5):
    headers = {"User-Agent": "paper-scout/1.0", "Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    full = url + "?" + urlencode(params)
    body = json.dumps(payload).encode("utf-8")
    delay = 2.0
    for attempt in range(retries):
        try:
            req = Request(full, data=body, headers=headers, method="POST")
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            sys.stderr.write(f"  batch enrichment failed (HTTP {e.code}); skipping TLDRs.\n")
            return None
        except URLError:
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return None


def enrich_tldr(collected, topics, api_key, sleep):
    """Fill in the `tldr` field for collected papers via /paper/batch.

    Runs after collection because the bulk-search endpoint does not return
    `tldr`. Once it arrives, every topic's `keyword_hits` flag is rechecked
    against it and OR'd in (never reset to False) — a paper's generated
    summary can surface a theme keyword that the raw title/abstract omitted,
    but the reverse (losing a hit already found) would just be a bug.
    """
    ids = list(collected.keys())
    filled = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        data = http_post(S2_BATCH, {"fields": "paperId,tldr"},
                         {"ids": chunk}, api_key)
        if not data:
            return filled
        for rec in data:
            if not rec:
                continue
            pid = rec.get("paperId")
            tldr = (rec.get("tldr") or {}).get("text")
            if pid in collected and tldr:
                collected[pid]["tldr"] = tldr
                hits = collected[pid]["keyword_hits"]
                for t in topics:
                    if keyword_hit(t["name"], tldr):
                        hits[t["name"]] = True
                filled += 1
        time.sleep(sleep)
    return filled


def venue_matches(paper_venue, pub_venue, substrings, exclude=()):
    """True if the venue looks like ours and not like a sibling conference.

    The exclusions matter: 'IEEE Asian Solid-State Circuits Conference'
    contains ISSCC's 'solid-state circuits conference' substring, so without
    them an ISSCC survey silently absorbs A-SSCC's program.
    """
    hay = (paper_venue or "").lower()
    if isinstance(pub_venue, dict):
        hay += " " + (pub_venue.get("name") or "").lower()
        for alt in pub_venue.get("alternate_names") or []:
            hay += " " + str(alt).lower()
    if any(s in hay for s in exclude):
        return False
    return any(s in hay for s in substrings)


def best_link(paper):
    """Prefer open-access PDF, then a resolvable DOI, then the S2 page."""
    oa = paper.get("openAccessPdf") or {}
    if oa.get("url"):
        return oa["url"]
    ext = paper.get("externalIds") or {}
    doi = ext.get("DOI")
    if doi:
        return f"https://doi.org/{doi}"
    return paper.get("url") or ""


def keyword_hit(keyword, *texts):
    if not keyword:
        return False
    kw = keyword.lower()
    return any(kw in (t or "").lower() for t in texts)


def fetch_venue(vkey, vconf, topics, year_param, ylo, yhi, api_key, args, assigned):
    """Fetch every candidate for one venue, across one or more topic passes.

    In --all mode this is a single bulk pass with no `query` param (the
    venue/year filter alone returns the whole program), so N topics cost
    nothing extra here — they only add per-candidate keyword-hit bookkeeping.
    In recall mode it is one bulk pass per topic query, merged into a single
    `collected` dict keyed by paper_id so a paper found by several topics'
    queries still appears once.

    `assigned` is a paper_id -> venue_key map shared across all venues in
    this run. If a paper matches two venues' substrings (should not happen
    given venues.json's exclusion lists, but "should not" is not "cannot"),
    the first venue to claim it wins and this one is skipped with a stderr
    warning — a silent double-count would corrupt every venue's denominator.
    """
    params = {
        "fields": FIELDS,
        "year": year_param,
        "venue": ",".join(vconf["s2_aliases"]),
        "sort": "citationCount:desc",
    }
    max_per_venue = args.max
    passes = [None] if args.all_mode else [t["query"] for t in topics]

    collected = {}
    hit_cap = False
    for query in passes:
        label = query or "(ALL venue papers)"
        sys.stderr.write(f"Fetching {vkey} {year_param} | query={label}\n")
        p_base = dict(params)
        if query:
            p_base["query"] = query

        token = None
        page = 0
        total_seen = 0
        while True:
            p = dict(p_base)
            if token:
                p["token"] = token
            data = http_get(S2_BULK, p, api_key)
            page += 1
            batch = data.get("data") or []
            if page == 1:
                sys.stderr.write(
                    f"  API reports ~{data.get('total', '?')} matches for the venue filter.\n")
            for paper in batch:
                total_seen += 1
                yr = paper.get("year")
                if yr is None or not (ylo <= yr <= yhi):
                    continue
                if not args.no_venue_filter and not venue_matches(
                        paper.get("venue"), paper.get("publicationVenue"),
                        vconf["venue_substrings"],
                        vconf.get("venue_exclude_substrings", ())):
                    continue
                pid = paper.get("paperId")
                if not pid or pid in collected:
                    continue  # already have it for this venue, from this pass or an earlier one
                prior = assigned.get(pid)
                if prior is not None and prior != vkey:
                    sys.stderr.write(
                        f"  WARNING: paper {pid} ({paper.get('title', '')!r}) matches both "
                        f"{prior} and {vkey}'s venue filter; keeping {prior} (first-assigned).\n")
                    continue
                authors = [a.get("name", "") for a in (paper.get("authors") or [])]
                title = paper.get("title") or ""
                abstract = paper.get("abstract") or ""
                ext = paper.get("externalIds") or {}
                collected[pid] = {
                    "paper_id": pid,
                    "title": title,
                    "authors": authors,
                    "year": yr,
                    "venue": paper.get("venue") or "",
                    "venue_key": vkey,
                    "abstract": abstract,
                    "tldr": "",
                    "doi": ext.get("DOI", ""),
                    "link": best_link(paper),
                    "keyword_hits": {t["name"]: keyword_hit(t["name"], title, abstract)
                                     for t in topics},
                    # to be filled in by the agent:
                    "scores": {t["name"]: None for t in topics},
                    "reasons": {t["name"]: "" for t in topics},
                }
                assigned[pid] = vkey
                if len(collected) >= max_per_venue:
                    hit_cap = True
                    break
            token = data.get("token")
            sys.stderr.write(f"  page {page}: seen {total_seen}, kept {len(collected)}\n")
            if not token or len(collected) >= max_per_venue or not batch:
                break
            time.sleep(args.sleep)
        if hit_cap:
            break  # venue budget is spent; further topic passes would only be skipped anyway

    if hit_cap:
        sys.stderr.write(
            f"WARNING: {vkey} hit --max={max_per_venue}; some lower-citation papers were "
            "dropped (sort=citationCount:desc means this is a biased truncation, not a "
            "random sample). Raise --max if this venue/year range needs full coverage.\n")

    if not collected:
        sys.stderr.write(
            f"WARNING: {vkey} {year_param} returned ZERO candidates. This can mean the "
            "venue/year genuinely has nothing, but for VLSI it usually means Semantic "
            "Scholar simply has no records under that venue string at all (confirmed: "
            "6 records total across every year, none after 2021) -- run "
            "crosscheck_sources.py --merge for this venue, since dblp is the only way "
            "VLSI papers enter the corpus at all, not just an optional audit. For other "
            "venues, try --no-venue-filter or double-check the year range first.\n")

    return collected


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", required=True,
                    help="Comma list (e.g. 'ISSCC,VLSI,CICC') or 'all'. See venues.json; "
                         "cli_aliases like ESSCIRC/A-SSCC are also accepted.")
    ap.add_argument("--years", required=True, help="'YYYY' or 'YYYY-YYYY'")
    ap.add_argument("--topic", dest="topic", action="append", required=True,
                    metavar="NAME=QUERY",
                    help="Repeatable. 'NAME=QUERY' pairs, split on the first '='. A bare "
                         "NAME (no '=') defaults its query to \"NAME\". QUERY is a boolean "
                         "recall query for the API, e.g. "
                         "'LLM=(\"LLM\" | \"large language model\" | \"transformer\")'. "
                         "Ignored per-topic when --all is set. Each topic gets its own "
                         "Score/Relevance/Reason column downstream, in the order given here.")
    ap.add_argument("--all", dest="all_mode", action="store_true",
                    help="Scan EVERY paper in the venue/years (ignore each topic's query). "
                         "Use when you want the agent to survey the full program rather "
                         "than a keyword-recall subset. Yields many more candidates, and "
                         "with several --topic values it is the cheap mode: one bulk pass "
                         "per venue regardless of topic count.")
    ap.add_argument("--out", default="candidates.json")
    ap.add_argument("--max", type=int, default=5000,
                    help="Max candidates to keep, PER VENUE (default 5000).")
    ap.add_argument("--no-venue-filter", action="store_true",
                    help="Skip local venue verification (use if a venue is being missed).")
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds between API pages.")
    args = ap.parse_args()

    venue_keys = resolve_venues(args.venue)
    all_venues = load_venues()
    topics = parse_topics(args.topic)

    year_param, (ylo, yhi) = parse_years(args.years)
    api_key = os.environ.get("S2_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "WARNING: S2_API_KEY not set — using the shared unauthenticated pool, "
            "which is heavily rate-limited. See .env.example.\n")

    assigned = {}  # paper_id -> venue_key, shared across venues (cross-venue dedup)
    by_venue = {}
    for vkey in venue_keys:
        vconf = all_venues[vkey]
        collected = fetch_venue(vkey, vconf, topics, year_param, ylo, yhi, api_key, args, assigned)
        if collected:
            sys.stderr.write(f"Enriching {vkey} with TLDR summaries via /paper/batch...\n")
            n_tldr = enrich_tldr(collected, topics, api_key, args.sleep)
            sys.stderr.write(f"  filled {n_tldr} TLDRs.\n")
        by_venue[vkey] = collected

    counts_by_venue = {vkey: len(by_venue[vkey]) for vkey in venue_keys}
    all_candidates = [c for vkey in venue_keys for c in by_venue[vkey].values()]
    all_candidates.sort(key=lambda c: (c["venue_key"], c["year"], c["title"]))

    out = {
        "meta": {
            "venues": venue_keys,
            "venue_descriptions": {vkey: all_venues[vkey]["description"] for vkey in venue_keys},
            "years": year_param,
            "topics": topics,
            "mode": "all" if args.all_mode else "recall",
            "candidate_count": len(all_candidates),
            "counts_by_venue": counts_by_venue,
        },
        "candidates": all_candidates,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    sys.stderr.write("\nSummary:\n")
    for vkey in venue_keys:
        collected = by_venue[vkey]
        n_abs = sum(1 for c in collected.values() if c["abstract"])
        sys.stderr.write(f"  {vkey}: {len(collected)} kept ({n_abs} with an abstract)\n")
    for t in topics:
        n_hit = sum(1 for c in all_candidates if c["keyword_hits"].get(t["name"]))
        sys.stderr.write(f"  topic {t['name']!r}: {n_hit} keyword hits\n")
    sys.stderr.write(f"\nWrote {len(all_candidates)} candidates to {args.out}.\n")


if __name__ == "__main__":
    main()
