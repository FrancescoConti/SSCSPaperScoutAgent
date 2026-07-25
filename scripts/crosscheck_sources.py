#!/usr/bin/env python3
"""
Cross-check a Semantic Scholar candidate list against *other* bibliographic
sources, to find papers S2 missed -- across one or more venues at once.

Semantic Scholar's venue metadata for solid-state-circuits conferences is
patchy: some years are indexed under an alternate venue string, some papers
have no venue at all, and for VLSI it is close to absent (see venues.json /
AGENT.md). A survey built on S2 alone therefore has an unknown recall gap.
This script independently enumerates the same conference/years from sources
that index the *program* rather than the citation graph, and reports what is
present there but absent from candidates.json.

`--venue` takes the same comma list / `all` as fetch_candidates.py. Each
venue is checked, and matched, independently: the S2 lookup index for a
given venue is built from *only* that venue's own candidates (via
`venue_key`), so a title fuzzy-match can never cross venues -- otherwise an
ISSCC candidate could silently absorb a VLSI dblp hit and both venues'
coverage numbers would be meaningless.

Sources (all queried independently; each is optional and failures are
non-fatal):

  * dblp     — the most reliable conference-program index, no key needed.
               Default, and the one to trust when sources disagree.
  * crossref — DOI registry; sees whatever IEEE deposited, no key needed.
               Default.
  * ieee     — IEEE Xplore metadata API; authoritative, but needs
               IEEE_XPLORE_API_KEY (institutional). Opt-in.
  * openalex — broad open catalogue, no key needed. Opt-in, because its
               venue metadata for IEEE conferences is poor: many ISSCC works
               exist in OpenAlex with no `primary_location` at all, so a
               venue-based enumeration silently under-reports. Useful for
               venues that OpenAlex does index properly.

Matching is by DOI first, then by normalized title (case/punctuation folded),
then by a token-overlap fallback that tolerates the truncated or
differently-punctuated titles these indexes disagree on. The fuzzy fallback
is blocked by year (see PaperIndex) so it stays fast at multi-venue,
multi-year scale.

Usage:
    # report only, one venue
    python3 crosscheck_sources.py --venue ISSCC --years 2021-2024 \
        --in candidates.json --out crosscheck.json

    # report AND fold the missing papers into candidates.json for scoring,
    # across every configured venue
    python3 crosscheck_sources.py --venue all --years 2021-2024 \
        --in candidates.json --out crosscheck.json --merge

Environment:
    OPENALEX_MAILTO        e-mail for the OpenAlex polite pool (optional).
    IEEE_XPLORE_API_KEY    IEEE Xplore API key (optional, institutional).
"""
import argparse
import html
import json
import os
import re
import sys
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from common import load_venues, parse_years, resolve_venues, normalize_payload

DBLP_API = "https://dblp.org/search/publ/api"
OPENALEX_SOURCES = "https://api.openalex.org/sources"
OPENALEX_WORKS = "https://api.openalex.org/works"
CROSSREF_WORKS = "https://api.crossref.org/works"
IEEE_API = "https://ieeexploreapi.ieee.org/api/v1/search/articles"

ALL_SOURCES = ["dblp", "openalex", "crossref", "ieee"]

# Words that carry no discriminating power in a circuits-conference title;
# dropped before the token-overlap comparison.
STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "with", "in", "on", "to",
    "using", "based", "via", "by", "at", "from", "into",
}

# Proceedings front matter. Crossref and IEEE deposit these with a DOI exactly
# like a real paper, and letting them through would inflate the per-year
# denominator that the whole survey statistic rests on.
NON_PAPER_PATTERNS = [
    r"^session\s+\d+\s+overview",
    r"\bsession\s+\d+\s+overview\b",
    r"^(isscc|vlsi|esscirc|esserc)\b.*\b(overview|layout|committee|program|"
    r"welcome|reception|schedule|floor plan|index)\b",
    r"^(table of )?contents\b",
    r"^(author|subject) index\b",
    r"^front[- ]?matter\b",
    r"^back[- ]?matter\b",
    r"^copyright\b",
    r"^title page\b",
    r"^cover\b",
    r"^committees?\b",
    r"^organizing committee\b",
    r"^technical program\b",
    r"^advance program\b",
    r"^conference (space )?(layout|information|map)\b",
    r"^welcome( message| to)?\b",
    r"^message from the\b",
    r"^plenary session\b",
    r"^opening remarks\b",
    r"^list of (reviewers|authors|papers)\b",
    r"^\[?(copyright|blank page|publisher'?s? information)\]?\b",
]
NON_PAPER_RE = re.compile("|".join(NON_PAPER_PATTERNS), re.IGNORECASE)


def is_front_matter(title):
    t = (title or "").strip()
    if not t:
        return True
    return bool(NON_PAPER_RE.search(t))


def http_json(url, params=None, headers=None, retries=4, timeout=60):
    """GET returning parsed JSON, or None if the source is unreachable.

    A dead source must not sink the run — the whole point is to consult
    several and report what we managed to reach.
    """
    hdrs = {"User-Agent": "paper-scout/1.0 (cross-check)"}
    hdrs.update(headers or {})
    full = url + ("?" + urlencode(params) if params else "")
    delay = 2.0
    for attempt in range(retries):
        try:
            with urlopen(Request(full, headers=hdrs), timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                sys.stderr.write(
                    f"    [retry {attempt+1}/{retries}] HTTP {e.code}, "
                    f"waiting {delay:.0f}s\n")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            body = e.read().decode("utf-8", "replace")[:200]
            sys.stderr.write(f"    HTTP {e.code}: {body}\n")
            return None
        except (URLError, TimeoutError) as e:
            sys.stderr.write(f"    [retry {attempt+1}/{retries}] network error: {e}\n")
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except json.JSONDecodeError:
            sys.stderr.write("    malformed JSON in response\n")
            return None
    return None


# ---------------------------------------------------------------- matching --

def norm_title(title):
    # Crossref and IEEE deliver titles containing HTML/MathML ("0.0006-mm
    # <sup>2</sup>") while Semantic Scholar delivers plain text, so the markup
    # has to go before anything is compared or the same paper looks like two.
    t = re.sub(r"<[^>]+>", "", title or "")
    t = html.unescape(t).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean_title(title):
    """Human-readable title with markup removed, for the report and for merged
    candidate records."""
    t = re.sub(r"<[^>]+>", "", title or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def venue_ok(venue, vconf):
    """Reject sibling conferences that share a name fragment.

    'IEEE International Solid-State Circuits Conference' and '2024 IEEE Asian
    Solid-State Circuits Conference (A-SSCC)' both contain the substring
    'solid-state circuits conference'. Without this check an ISSCC survey
    quietly absorbs A-SSCC's program, which is a different conference.
    """
    v = (venue or "").lower()
    if not v:
        return True  # nothing to judge on; let the source's own filter stand
    return not any(s in v for s in vconf.get("venue_exclude_substrings", []))


def title_tokens(title):
    return {w for w in norm_title(title).split() if w not in STOPWORDS and len(w) > 2}


def norm_doi(doi):
    if not doi:
        return ""
    d = doi.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d


class PaperIndex:
    """Lookup of one venue's S2 candidates by DOI / title / token set.

    Build one of these PER VENUE, from only that venue's candidates
    (`venue_key`-filtered by the caller) -- otherwise an ISSCC title can
    fuzzy-absorb a VLSI dblp hit and both venues' coverage numbers become
    meaningless.

    The fuzzy fallback is O(n*m) over token sets, which is fine for one
    venue-year but not for a multi-venue, multi-year run: five venues x 15
    years is ~12k candidates against ~12k source records, i.e. ~10^8 set
    intersections if done naively. `self.tokens` is therefore blocked by
    year (`year -> [(tokens, candidate)]`) and `find()` only compares within
    the query's own year -- safe because `dedupe()` already drops
    out-of-range/yearless *source* records before they reach `find()`, and
    every candidate that made it through fetch_candidates.py also carries a
    year. Candidates that somehow lack one still need to be reachable, so
    they sit in `self.tokens_no_year` and are always added to whichever
    year-bucket is being scanned; a query with no year at all falls back to
    scanning every bucket (the pre-blocking behaviour).
    """

    def __init__(self, candidates):
        self.by_doi = {}
        self.by_title = {}
        self.tokens = {}          # year -> [(token_set, candidate)]
        self.tokens_no_year = []  # candidates with no year: always in scope
        for c in candidates:
            doi = norm_doi(c.get("doi"))
            if doi:
                self.by_doi[doi] = c
            nt = norm_title(c.get("title"))
            if nt:
                self.by_title[nt] = c
            toks = title_tokens(c.get("title"))
            if not toks:
                continue
            year = c.get("year")
            if year is None:
                self.tokens_no_year.append((toks, c))
            else:
                self.tokens.setdefault(year, []).append((toks, c))

    def _fuzzy_candidates(self, year):
        if year is None:
            # The query itself has no year to block on -- fall back to a
            # full scan across every year, same as the pre-blocking index.
            bucket = list(self.tokens_no_year)
            for lst in self.tokens.values():
                bucket.extend(lst)
            return bucket
        return self.tokens.get(year, []) + self.tokens_no_year

    def find(self, doi, title, year=None):
        d = norm_doi(doi)
        if d and d in self.by_doi:
            return self.by_doi[d], "doi"
        nt = norm_title(title)
        if nt and nt in self.by_title:
            return self.by_title[nt], "title"
        toks = title_tokens(title)
        if len(toks) >= 4:
            for other, cand in self._fuzzy_candidates(year):
                overlap = len(toks & other)
                # Jaccard-ish: near-identical titles that differ only in
                # punctuation, subtitle, or a trailing "(Invited)".
                if overlap >= 4 and overlap / max(len(toks), len(other)) >= 0.8:
                    return cand, "fuzzy"
        return None, None


# ----------------------------------------------------------------- sources --

def fetch_dblp(vconf, ylo, yhi, sleep):
    """dblp indexes conference proceedings volume by volume — the closest
    thing to an authoritative program listing."""
    out = []
    for stream in vconf.get("dblp_streams", []):
        for year in range(ylo, yhi + 1):
            first = 0
            while True:
                q = f"streamid:{stream}: year:{year}:"
                data = http_json(DBLP_API, {
                    "q": q, "h": 1000, "f": first, "format": "json",
                })
                if not data:
                    break
                hits = ((data.get("result") or {}).get("hits") or {})
                items = hits.get("hit") or []
                for hit in items:
                    info = hit.get("info") or {}
                    authors = (info.get("authors") or {}).get("author") or []
                    if isinstance(authors, dict):
                        authors = [authors]
                    names = [a.get("text", "") if isinstance(a, dict) else str(a)
                             for a in authors]
                    out.append({
                        "title": (info.get("title") or "").rstrip("."),
                        "year": int(info["year"]) if info.get("year") else None,
                        "doi": info.get("doi", ""),
                        "authors": names,
                        "link": info.get("ee") or info.get("url") or "",
                        "venue": info.get("venue") or stream,
                    })
                total = int(hits.get("@total", 0))
                first += len(items)
                if not items or first >= total:
                    break
                time.sleep(sleep)
            time.sleep(sleep)
    return out


def resolve_openalex_sources(vconf, mailto, sleep, max_pages=5):
    """OpenAlex needs source IDs; resolve them from the venue name so we never
    hardcode IDs that may churn.

    OpenAlex registers each *edition* of a conference as its own source
    ("2022 IEEE International Solid-State Circuits Conference (ISSCC)", ...),
    so a single page of results would silently cover only a few years. Page
    through until the matches run out.
    """
    ids, seen = [], set()
    substrings = [s.lower() for s in vconf["venue_substrings"]]
    for query in vconf.get("openalex_queries", []):
        for page in range(1, max_pages + 1):
            params = {"search": query, "per-page": 200, "page": page}
            if mailto:
                params["mailto"] = mailto
            data = http_json(OPENALEX_SOURCES, params)
            if not data:
                break
            results = data.get("results") or []
            for src in results:
                name = (src.get("display_name") or "").lower()
                alt = " ".join(str(a).lower()
                               for a in (src.get("alternate_titles") or []))
                if not any(s in name or s in alt for s in substrings):
                    continue
                sid = (src.get("id") or "").rsplit("/", 1)[-1]
                if sid and sid not in seen:
                    seen.add(sid)
                    ids.append((sid, src.get("display_name")))
            if len(results) < 200:
                break
            time.sleep(sleep)
        time.sleep(sleep)
    return ids


def fetch_openalex(vconf, ylo, yhi, sleep):
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    sources = resolve_openalex_sources(vconf, mailto, sleep)
    if not sources:
        sys.stderr.write("    no matching OpenAlex source found for this venue\n")
        return []
    sys.stderr.write(f"    matched {len(sources)} OpenAlex source records "
                     f"(one per conference edition)\n")
    out = []
    # OpenAlex caps the number of OR'd values in a single filter, so ask in
    # chunks rather than building one enormous filter string.
    for i in range(0, len(sources), 25):
        src_filter = "|".join(sid for sid, _ in sources[i:i + 25])
        cursor = "*"
        while cursor:
            params = {
                "filter": f"primary_location.source.id:{src_filter},"
                          f"publication_year:{ylo}-{yhi}",
                "per-page": 200,
                "cursor": cursor,
                "select": "id,doi,title,publication_year,authorships,primary_location",
            }
            if mailto:
                params["mailto"] = mailto
            data = http_json(OPENALEX_WORKS, params)
            if not data:
                break
            for w in data.get("results") or []:
                names = [(a.get("author") or {}).get("display_name", "")
                         for a in (w.get("authorships") or [])]
                loc = w.get("primary_location") or {}
                out.append({
                    "title": w.get("title") or "",
                    "year": w.get("publication_year"),
                    "doi": w.get("doi", "") or "",
                    "authors": names,
                    "link": (loc.get("landing_page_url")
                             or w.get("doi") or w.get("id") or ""),
                    "venue": ((loc.get("source") or {}).get("display_name") or ""),
                })
            cursor = (data.get("meta") or {}).get("next_cursor")
            time.sleep(sleep)
    return out


def fetch_crossref(vconf, ylo, yhi, sleep, max_pages=60, patience=3):
    """Crossref sees whatever IEEE deposited.

    `query.container-title` is a *relevance* search, not a filter: it happily
    returns every proceedings-article in Crossref (hundreds of thousands),
    best-matching first. So we re-verify each hit's container title locally and
    stop once `patience` consecutive pages contain nothing from this venue,
    rather than paging to the end of the corpus.
    """
    substrings = [s.lower() for s in vconf["venue_substrings"]]
    out, seen = [], set()
    for query in vconf.get("crossref_queries", []):
        cursor = "*"
        pages = 0
        dry_streak = 0
        while cursor and pages < max_pages and dry_streak < patience:
            params = {
                "query.container-title": query,
                "filter": (f"type:proceedings-article,"
                           f"from-pub-date:{ylo}-01-01,"
                           f"until-pub-date:{yhi}-12-31"),
                "rows": 500,
                "cursor": cursor,
                "select": "DOI,title,container-title,issued,author,URL",
            }
            data = http_json(CROSSREF_WORKS, params,
                             headers={"User-Agent":
                                      "paper-scout/1.0 (mailto:noreply@example.com)"})
            if not data:
                break
            msg = data.get("message") or {}
            items = msg.get("items") or []
            pages += 1
            hits_this_page = 0
            for it in items:
                container = " ".join(it.get("container-title") or []).lower()
                if not any(s in container for s in substrings):
                    continue
                hits_this_page += 1
                doi = norm_doi(it.get("DOI"))
                if doi in seen:
                    continue
                seen.add(doi)
                parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
                year = parts[0] if parts else None
                if year is None or not (ylo <= year <= yhi):
                    continue
                names = [" ".join(x for x in (a.get("given"), a.get("family")) if x)
                         for a in (it.get("author") or [])]
                out.append({
                    "title": " ".join(it.get("title") or []),
                    "year": year,
                    "doi": it.get("DOI", ""),
                    "authors": names,
                    "link": it.get("URL", ""),
                    "venue": " ".join(it.get("container-title") or []),
                })
            dry_streak = 0 if hits_this_page else dry_streak + 1
            cursor = msg.get("next-cursor")
            if not items:
                break
            time.sleep(sleep)
    return out


def fetch_ieee(vconf, ylo, yhi, sleep):
    api_key = os.environ.get("IEEE_XPLORE_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("    IEEE_XPLORE_API_KEY not set — skipping IEEE Xplore\n")
        return []
    out = []
    for title in vconf.get("ieee_publication_titles", []):
        start = 1
        while True:
            params = {
                "apikey": api_key,
                "publication_title": title,
                "start_year": ylo,
                "end_year": yhi,
                "max_records": 200,
                "start_record": start,
                "format": "json",
            }
            data = http_json(IEEE_API, params)
            if not data:
                break
            articles = data.get("articles") or []
            for a in articles:
                authors = ((a.get("authors") or {}).get("authors") or [])
                names = [x.get("full_name", "") for x in authors]
                year = a.get("publication_year")
                try:
                    year = int(year)
                except (TypeError, ValueError):
                    year = None
                out.append({
                    "title": a.get("title") or "",
                    "year": year,
                    "doi": a.get("doi", "") or "",
                    "authors": names,
                    "link": a.get("html_url") or a.get("pdf_url") or "",
                    "venue": a.get("publication_title") or title,
                })
            total = data.get("total_records", 0)
            start += len(articles)
            if not articles or start > total:
                break
            time.sleep(sleep)
    return out


FETCHERS = {
    "dblp": fetch_dblp,
    "openalex": fetch_openalex,
    "crossref": fetch_crossref,
    "ieee": fetch_ieee,
}


# -------------------------------------------------------------------- main --

def dedupe(records, ylo, yhi, vconf):
    """Collapse a source's records by DOI/title and drop everything that isn't
    a real paper from *this* conference in range.

    Returns (records, {reason: n_dropped}).
    """
    out, seen = [], set()
    dropped = {"front_matter": 0, "wrong_venue": 0}
    for r in records:
        if r.get("year") is None or not (ylo <= r["year"] <= yhi):
            continue
        if not venue_ok(r.get("venue"), vconf):
            dropped["wrong_venue"] += 1
            continue
        if is_front_matter(r.get("title")):
            dropped["front_matter"] += 1
            continue
        r["title"] = clean_title(r.get("title"))
        key = norm_doi(r.get("doi")) or norm_title(r.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out, dropped


def crosscheck_venue(vkey, vconf, ylo, yhi, vcandidates, requested, sleep):
    """Run every requested source against one venue's own candidates and
    return (per_venue_report, missing_records). `vcandidates` must already
    be filtered to this venue's `venue_key` -- see the caller."""
    index = PaperIndex(vcandidates)
    sys.stderr.write(f"\n-- {vkey} ({len(vcandidates)} S2 candidates) --\n")

    per_source = {}
    missing_by_key = {}
    reached = []

    for name in requested:
        sys.stderr.write(f"  querying {name}...\n")
        try:
            records = FETCHERS[name](vconf, ylo, yhi, sleep)
        except Exception as e:                        # noqa: BLE001 - report, continue
            sys.stderr.write(f"    {name} failed: {e}\n")
            per_source[name] = {"reachable": False, "error": str(e)}
            continue
        records, dropped = dedupe(records, ylo, yhi, vconf)
        if not records:
            sys.stderr.write(f"    {name} returned nothing (unreachable or no match)\n")
            per_source[name] = {"reachable": False, "found": 0}
            continue
        reached.append(name)
        if dropped["front_matter"]:
            sys.stderr.write(f"    dropped {dropped['front_matter']} front-matter "
                             f"entries (session overviews, indexes, ...)\n")
        if dropped["wrong_venue"]:
            sys.stderr.write(f"    dropped {dropped['wrong_venue']} entries from "
                             f"sibling conferences (e.g. A-SSCC)\n")

        found_missing, by_year = [], {}
        for r in records:
            by_year[r["year"]] = by_year.get(r["year"], 0) + 1
            hit, how = index.find(r.get("doi"), r.get("title"), r.get("year"))
            if hit:
                srcs = hit.setdefault("sources", ["semantic_scholar"])
                if name not in srcs:
                    srcs.append(name)
                continue
            k = norm_doi(r.get("doi")) or norm_title(r.get("title"))
            if k in missing_by_key:
                if name not in missing_by_key[k]["sources"]:
                    missing_by_key[k]["sources"].append(name)
            else:
                rec = dict(r)
                rec["sources"] = [name]
                missing_by_key[k] = rec
            found_missing.append(r)

        per_source[name] = {
            "reachable": True,
            "found": len(records),
            "dropped": dropped,
            "by_year": {str(y): n for y, n in sorted(by_year.items())},
            "matched_in_s2": len(records) - len(found_missing),
            "missing_from_s2": len(found_missing),
            "coverage": round((len(records) - len(found_missing)) / len(records), 3),
        }
        sys.stderr.write(
            f"    {len(records)} papers; S2 covers "
            f"{per_source[name]['matched_in_s2']} "
            f"({per_source[name]['coverage']:.0%}), "
            f"{len(found_missing)} missing\n")

    missing = sorted(missing_by_key.values(),
                     key=lambda r: (r.get("year") or 0, r.get("title") or ""))
    for r in missing:
        r["venue_key"] = vkey  # so a merged record and the flat report both know
    missing_by_year = {}
    for r in missing:
        y = str(r.get("year"))
        missing_by_year[y] = missing_by_year.get(y, 0) + 1

    report = {
        "venue": vkey,
        "years": f"{ylo}-{yhi}",
        "s2_candidate_count": len(vcandidates),
        "sources": per_source,
        "sources_reached": reached,
        "missing_count": len(missing),
        "missing_by_year": missing_by_year,
        "missing": missing,
    }

    if not reached:
        sys.stderr.write(
            f"  WARNING: no cross-check source was reachable for {vkey} — "
            f"coverage is UNVERIFIED, not confirmed.\n")
    else:
        worst = min((per_source[s]["coverage"] for s in reached), default=1.0)
        sys.stderr.write(
            f"  {vkey}: {len(missing)} papers found by {'/'.join(reached)} but "
            f"absent from Semantic Scholar (worst per-source coverage {worst:.0%}).\n")

    return report, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", required=True,
                    help="Comma list (e.g. 'ISSCC,VLSI,CICC') or 'all'. See "
                         "venues.json; cli_aliases like ESSCIRC/A-SSCC are also "
                         "accepted.")
    ap.add_argument("--years", required=True, help="'YYYY' or 'YYYY-YYYY'")
    ap.add_argument("--in", dest="infile", required=True,
                    help="candidates.json from fetch_candidates.py (or a legacy "
                         "single-venue/single-topic file -- upgraded on load).")
    ap.add_argument("--out", default="crosscheck.json",
                    help="Where to write the coverage report.")
    ap.add_argument("--sources", default="dblp,crossref",
                    help="Comma-separated subset of: " + ",".join(ALL_SOURCES)
                         + " (default: dblp,crossref). 'ieee' needs "
                           "IEEE_XPLORE_API_KEY; 'openalex' indexes IEEE "
                           "conference venues poorly and will under-report.")
    ap.add_argument("--merge", action="store_true",
                    help="Append papers missing from candidates.json back into it "
                         "(scores=null for every topic) so the agent can score "
                         "them too.")
    ap.add_argument("--merge-out", default=None,
                    help="Write the merged candidate list here instead of "
                         "overwriting --in.")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="Seconds between API pages.")
    args = ap.parse_args()

    venues = load_venues()
    venue_keys = resolve_venues(args.venue)
    _, (ylo, yhi) = parse_years(args.years)

    requested = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in requested if s not in FETCHERS]
    if unknown:
        sys.exit(f"Unknown source(s): {', '.join(unknown)}. "
                 f"Known: {', '.join(ALL_SOURCES)}")

    with open(args.infile, encoding="utf-8") as fh:
        data = json.load(fh)
    data = normalize_payload(data)
    candidates = data.get("candidates", [])
    topics = data.get("meta", {}).get("topics", [])

    sys.stderr.write(
        f"Cross-checking {', '.join(venue_keys)} {ylo}-{yhi} against "
        f"{', '.join(requested)}\n"
        f"  candidates.json holds {len(candidates)} papers total from Semantic Scholar\n")

    by_venue = {}
    all_missing = []
    rollup = {name: {"total_found": 0, "total_matched_in_s2": 0,
                     "total_missing_from_s2": 0, "reachable_in": []}
             for name in requested}

    for vkey in venue_keys:
        vconf = venues[vkey]
        vcandidates = [c for c in candidates if c.get("venue_key") == vkey]
        report, missing = crosscheck_venue(vkey, vconf, ylo, yhi, vcandidates,
                                           requested, args.sleep)
        by_venue[vkey] = report
        all_missing.extend(missing)
        for name in report["sources_reached"]:
            rollup[name]["reachable_in"].append(vkey)
        for name, s in report["sources"].items():
            if s.get("reachable"):
                rollup[name]["total_found"] += s["found"]
                rollup[name]["total_matched_in_s2"] += s["matched_in_s2"]
                rollup[name]["total_missing_from_s2"] += s["missing_from_s2"]

    for name in requested:
        rr = rollup[name]
        rr["coverage"] = (round(rr["total_matched_in_s2"] / rr["total_found"], 3)
                          if rr["total_found"] else None)

    report = {
        "venues": venue_keys,
        "years": f"{ylo}-{yhi}",
        "sources_requested": requested,
        "by_venue": by_venue,
        "rollup": rollup,
        "missing_count_total": len(all_missing),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    reached_any = sorted({n for vkey in venue_keys for n in by_venue[vkey]["sources_reached"]})

    if args.merge and all_missing:
        for r in all_missing:
            vkey = r.get("venue_key", "")
            pid = (f"{r['sources'][0]}:"
                  f"{norm_doi(r.get('doi')) or norm_title(r.get('title'))[:60]}")
            candidates.append({
                "paper_id": pid,
                "title": r.get("title", ""),
                "authors": r.get("authors", []),
                "year": r.get("year"),
                "venue": r.get("venue", ""),
                "venue_key": vkey,
                "abstract": "",
                "tldr": "",
                "doi": r.get("doi", ""),
                "link": r.get("link", ""),
                "sources": r["sources"],
                "keyword_hits": {t["name"]: False for t in topics},
                "scores": {t["name"]: None for t in topics},
                "reasons": {t["name"]: "" for t in topics},
            })
        data["candidates"] = sorted(
            candidates, key=lambda c: (c.get("venue_key") or "", c.get("year") or 0,
                                       c.get("title") or ""))
        meta = data.setdefault("meta", {})
        prior = meta.get("crosschecked_sources") or []
        meta["crosschecked_sources"] = list(dict.fromkeys(
            prior + ["semantic_scholar"] + reached_any))
        meta["merged_from_crosscheck"] = len(all_missing)
        meta["candidate_count"] = len(data["candidates"])
        target = args.merge_out or args.infile
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        sys.stderr.write(
            f"\nMerged {len(all_missing)} missing papers into {target}. "
            f"They have no abstract — score them from the title, or lower their "
            f"weight, and re-run build_ods.py.\n")
    elif args.merge:
        # Still record that a cross-check happened, so Meta can say so.
        meta = data.setdefault("meta", {})
        prior = meta.get("crosschecked_sources") or []
        meta["crosschecked_sources"] = list(dict.fromkeys(
            prior + ["semantic_scholar"] + reached_any))
        target = args.merge_out or args.infile
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    if not reached_any:
        sys.stderr.write(
            "\nWARNING: no cross-check source was reachable for ANY venue — "
            "coverage is UNVERIFIED, not confirmed.\n")
    else:
        sys.stderr.write(
            f"\nWrote {args.out}: {len(all_missing)} papers found across "
            f"{', '.join(venue_keys)} by sources not in Semantic Scholar. "
            f"Per-source rollup coverage: " +
            ", ".join(f"{n}={rollup[n]['coverage']:.0%}" for n in requested
                      if rollup[n]["coverage"] is not None) + "\n")


if __name__ == "__main__":
    main()
