#!/usr/bin/env python3
"""
Shared, dependency-free vocabulary for the paper-scout pipeline.

`fetch_candidates.py`, `build_ods.py` and `crosscheck_sources.py` all need to
agree on what a venue key is, how a year range is spelled, how a `--topic`
argument is parsed, and how spreadsheet column letters are derived — this
module is the one place that agreement lives, so the three scripts cannot
quietly drift apart on any of it.

Deliberately standard-library only (no odfpy, no requests): `crosscheck_sources.py`
needs `resolve_venues()`/`parse_years()` but must stay importable without
pulling in the ODS toolchain, and vice versa for a bare fetch-only environment.
"""
import json
import os
import re
import sys

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


def resolve_venues(spec):
    """'ISSCC,VLSI' / 'all' -> ordered list of canonical venue keys.

    Case-insensitive, and resolves each venue's optional `cli_aliases`
    (`ESSCIRC` -> `ESSERC`, `A-SSCC` -> `ASSCC`) so the CLI can accept the
    name people actually type without venues.json growing duplicate top-level
    entries for the same conference. Order follows the CLI spec, deduplicated;
    `all` follows venues.json's own (curated) order.
    """
    venues = load_venues()
    spec = (spec or "").strip()
    if not spec:
        sys.exit("--venue must name at least one venue, or 'all'.")
    if spec.lower() == "all":
        return list(venues.keys())

    alias_to_key = {}
    for key, vconf in venues.items():
        alias_to_key[key.upper()] = key
        for alias in vconf.get("cli_aliases", ()):
            alias_to_key[str(alias).upper()] = key

    result = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        canon = alias_to_key.get(tok.upper())
        if canon is None:
            sys.exit(f"Unknown venue {tok!r}. Known: {', '.join(venues)} (or 'all').")
        if canon not in result:
            result.append(canon)
    if not result:
        sys.exit("--venue must name at least one venue, or 'all'.")
    return result


def parse_topics(topic_args):
    """['LLM', 'CIM=(\"compute-in-memory\"|\"CIM\")'] -> ordered topic dicts.

    Each `--topic` value is `name=query`, split on the *first* `=` only — a
    boolean recall query legitimately contains `=` inside a quoted term or an
    S2 field filter, and a second split there would silently truncate it. A
    value with no `=` is shorthand: the query is just the quoted name
    (`'LLM'` -> query `"LLM"`), which is the common case for a keyword that
    needs no synonym expansion.

    Names become dict keys and Papers-sheet column headers downstream, so
    duplicate or empty names are a hard error here rather than a confusing
    collision three scripts later.
    """
    topics = []
    seen = set()
    for raw in topic_args or []:
        if "=" in raw:
            name, _, query = raw.partition("=")
            query = query.strip()
        else:
            name, query = raw, None
        name = name.strip()
        if not name:
            sys.exit(f"--topic name must not be empty (got {raw!r}).")
        if name in seen:
            sys.exit(
                f"--topic name {name!r} given more than once; each topic needs "
                "a distinct name (it becomes a dict key and a column header).")
        seen.add(name)
        if query is None:
            query = f'"{name}"'
        elif not query:
            sys.exit(f"--topic {name!r} has an empty query after '='.")
        topics.append({"name": name, "query": query})
    if not topics:
        sys.exit("At least one --topic is required.")
    return topics


def normalize_payload(data):
    """Upgrade a legacy single-topic/single-venue candidates file in place.

    Before multi-topic/multi-venue support, a candidate carried a scalar
    `score`/`reason`/`keyword_hit` and `meta` carried a scalar
    `keyword`/`venue`. This is what lets `build_ods.py --in` merge the four
    pre-existing single-topic scored files in this repo (candidates.scored.json,
    .transformers.json, .ssm.json, .cnn_dnn.json) into one workbook without
    re-scoring anything. Detection is per-field, not per-file, so a
    partially-upgraded payload (e.g. hand-edited to add `venue_key`) only
    fills in what is actually missing.
    """
    meta = data.setdefault("meta", {})
    legacy_keyword = meta.get("keyword")
    legacy_venue = meta.get("venue")

    if "topics" not in meta and legacy_keyword:
        meta["topics"] = [{"name": legacy_keyword, "query": meta.get("query", "")}]
    if "venues" not in meta and legacy_venue:
        meta["venues"] = [legacy_venue]

    for cand in data.get("candidates", []):
        if "scores" not in cand and "score" in cand and legacy_keyword:
            cand["scores"] = {legacy_keyword: cand.get("score")}
            cand["reasons"] = {legacy_keyword: cand.get("reason", "")}
            cand["keyword_hits"] = {legacy_keyword: cand.get("keyword_hit", False)}
        if "venue_key" not in cand:
            cand["venue_key"] = legacy_venue or cand.get("venue", "")

    return data


def col_letter(idx):
    """0-based column index -> spreadsheet letter(s): 0->A, 25->Z, 26->AA, 27->AB.

    Needed because the Papers sheet grows by 3 columns per topic (11 + 3N),
    so hardcoded single-letter column constants stop working once a survey
    covers more than about five topics.
    """
    idx += 1  # switch to 1-based so the bijective base-26 division works
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters
