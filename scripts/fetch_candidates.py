#!/usr/bin/env python3
"""
Fetch candidate papers for a conference + year range from the Semantic Scholar
Graph API, so an agent can score them by theme.

This script does the *deterministic* work only: it queries the API, filters by
venue, and emits a JSON list of candidates with everything needed to judge
relevance (title, abstract, TLDR, authors, links, keyword-hit flag).

The actual relevance *scoring* is intentionally NOT done here — that is the
agent's job (see AGENT.md).

Usage:
    python3 fetch_candidates.py \
        --venue ISSCC \
        --years 2021-2024 \
        --keyword "LLM" \
        --query '("LLM" | "large language model" | "transformer" | "attention" | "generative")' \
        --out candidates.json

Environment:
    S2_API_KEY   Semantic Scholar API key (optional but strongly recommended).
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

S2_BULK = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
# NOTE: the bulk-search endpoint does not support `tldr`; we fetch that
# separately via the /paper/batch endpoint below.
FIELDS = "paperId,title,abstract,year,venue,publicationVenue,authors,externalIds,openAccessPdf,url,citationCount"

HERE = os.path.dirname(os.path.abspath(__file__))


def load_venues():
    with open(os.path.join(HERE, "venues.json"), encoding="utf-8") as fh:
        return json.load(fh)


def parse_years(spec):
    """'2021-2024' -> '2021-2024' (S2 range); '2023' -> '2023'; validated."""
    spec = spec.strip()
    if re.fullmatch(r"\d{4}", spec):
        return spec, (int(spec), int(spec))
    m = re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", spec)
    if not m:
        sys.exit(f"--years must be 'YYYY' or 'YYYY-YYYY', got: {spec!r}")
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo > hi:
        lo, hi = hi, lo
    return f"{lo}-{hi}", (lo, hi)


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


def enrich_tldr(collected, keyword, api_key, sleep):
    """Fill in the `tldr` field for collected papers via /paper/batch."""
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
                if keyword_hit(keyword, tldr):
                    collected[pid]["keyword_hit"] = True
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", required=True, help="ISSCC | VLSI | ESSERC (see venues.json)")
    ap.add_argument("--years", required=True, help="'YYYY' or 'YYYY-YYYY'")
    ap.add_argument("--keyword", required=True,
                    help="The raw theme keyword, used only to flag direct hits (e.g. 'LLM').")
    ap.add_argument("--query", default=None,
                    help="Boolean recall query for the API. Defaults to --keyword. "
                         "Expand with synonyms to raise recall, e.g. "
                         "'(\"LLM\" | \"large language model\" | \"transformer\")'. "
                         "Ignored when --all is set.")
    ap.add_argument("--all", dest="all_mode", action="store_true",
                    help="Scan EVERY paper in the venue/years (ignore --query). Use "
                         "when you want the agent to survey the full program rather "
                         "than a keyword-recall subset. Yields many more candidates.")
    ap.add_argument("--out", default="candidates.json")
    ap.add_argument("--max", type=int, default=2000, help="Max candidates to keep.")
    ap.add_argument("--no-venue-filter", action="store_true",
                    help="Skip local venue verification (use if the venue is being missed).")
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds between API pages.")
    args = ap.parse_args()

    venues = load_venues()
    key = args.venue.upper()
    if key not in venues:
        sys.exit(f"Unknown venue {args.venue!r}. Known: {', '.join(venues)}")
    vconf = venues[key]

    year_param, (ylo, yhi) = parse_years(args.years)
    api_key = os.environ.get("S2_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "WARNING: S2_API_KEY not set — using the shared unauthenticated pool, "
            "which is heavily rate-limited. See .env.example.\n")

    params = {
        "fields": FIELDS,
        "year": year_param,
        "venue": ",".join(vconf["s2_aliases"]),
        "sort": "citationCount:desc",
    }
    if args.all_mode:
        query = "(ALL venue papers)"
        # No `query` param -> the venue/year filter returns the whole program.
    else:
        query = args.query or f'"{args.keyword}"'
        params["query"] = query

    sys.stderr.write(
        f"Fetching {key} {year_param} | query={query}\n")

    collected = {}
    token = None
    total_seen = 0
    page = 0
    while True:
        p = dict(params)
        if token:
            p["token"] = token
        data = http_get(S2_BULK, p, api_key)
        page += 1
        batch = data.get("data") or []
        if page == 1:
            sys.stderr.write(f"  API reports ~{data.get('total', '?')} matches for the venue filter.\n")
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
                continue
            authors = [a.get("name", "") for a in (paper.get("authors") or [])]
            tldr = (paper.get("tldr") or {}).get("text")
            ext = paper.get("externalIds") or {}
            collected[pid] = {
                "paper_id": pid,
                "title": paper.get("title") or "",
                "authors": authors,
                "year": yr,
                "venue": paper.get("venue") or "",
                "abstract": paper.get("abstract") or "",
                "tldr": tldr or "",
                "doi": ext.get("DOI", ""),
                "link": best_link(paper),
                "keyword_hit": keyword_hit(
                    args.keyword, paper.get("title"), paper.get("abstract"), tldr),
                # to be filled in by the agent:
                "score": None,
                "reason": "",
            }
            if len(collected) >= args.max:
                break
        token = data.get("token")
        sys.stderr.write(f"  page {page}: seen {total_seen}, kept {len(collected)}\n")
        if not token or len(collected) >= args.max or not batch:
            break
        time.sleep(args.sleep)

    if collected:
        sys.stderr.write("Enriching with TLDR summaries via /paper/batch...\n")
        n_tldr = enrich_tldr(collected, args.keyword, api_key, args.sleep)
        sys.stderr.write(f"  filled {n_tldr} TLDRs.\n")

    out = {
        "meta": {
            "venue": key,
            "venue_description": vconf["description"],
            "years": year_param,
            "keyword": args.keyword,
            "query": query,
            "mode": "all" if args.all_mode else "recall",
            "candidate_count": len(collected),
        },
        "candidates": sorted(collected.values(),
                             key=lambda c: (c["year"], c["title"])),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    n_hit = sum(1 for c in collected.values() if c["keyword_hit"])
    n_abs = sum(1 for c in collected.values() if c["abstract"])
    sys.stderr.write(
        f"\nWrote {len(collected)} candidates to {args.out} "
        f"({n_hit} with a direct keyword hit, {n_abs} with an abstract).\n")


if __name__ == "__main__":
    main()
