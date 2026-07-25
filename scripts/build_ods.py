#!/usr/bin/env python3
"""
Turn one or more scored candidates JSON files into an ODS spreadsheet
designed for *cross-topic, cross-venue* statistics.

Reads the JSON produced by fetch_candidates.py *after the agent has filled in
each topic's `scores`/`reasons` fields*. `--in` is repeatable: pass several
scored files and they are merged by `paper_id` into one workbook with one
3-column block per topic — this is what turns four separate single-topic
exports of the same fetch into one side-by-side comparison, with no
re-scoring. Legacy single-topic/single-venue files (the old scalar
`score`/`reason`/`keyword`/`venue` schema) are upgraded on load by
`common.normalize_payload()`, so old and new exports merge freely.

Unlike earlier versions, this script does **not** bake the relevance
thresholds into the output. Every scored candidate is written to the Papers
sheet, and the band cut-offs live in a `Config` sheet as editable cells.
`Relevance`, `Best score`/`Best topic`/`Kept (any)`, and the whole Summary
sheet are ODF **formulas** that recompute the moment a threshold changes.

The workbook has four sheets:

  * Papers  — one row per paper that has a numeric score for at least one
              topic: Paper ID | Title | Authors | Year | Venue key | Venue |
              Sources | Link | Best score | Best topic | Kept (any), then a
              Score / Relevance / Reason triple per topic, in the order
              topics were given on the command line. A paper missing a
              score for a given topic gets an EMPTY cell there (not 0 —
              0 would mean "judged unrelated" and would corrupt that
              topic's Avg score and Weak count).
  * Summary — tidy long form, one row per Topic x Venue x Year, plus
              "(all)" roll-up rows per venue, per year, and grand-total,
              for every topic. Entirely COUNTIFS/AVERAGEIFS formulas over
              Papers. The roll-up rows are not blank-row separated, so
              pivoting over this sheet must filter them out or it will
              double-count — noted on the sheet and on Meta.
  * Config  — the editable thresholds, shared across every topic's Score
              column. Change a number here and Papers + Summary follow.
  * Meta    — per-topic recall queries, per-venue descriptions and fetch
              counts, per-venue abstract coverage, the pivot caveat, and
              the run parameters, so the export is self-describing.

No charts are produced (by design).

Column letters are never hardcoded: the Papers sheet has 11 + 3N columns for
N topics, which runs past 'Z' into two-letter addresses ('AA', 'AB', ...) as
soon as a survey covers more than about five topics, so every address is
derived from common.col_letter().

Usage:
    python3 build_ods.py --in candidates.scored.json --out results.ods

    # --in is repeatable: merge several scored files into one workbook.
    python3 build_ods.py \
        --in candidates.scored.json --in candidates.scored.transformers.json \
        --in candidates.scored.ssm.json --in candidates.scored.cnn_dnn.json \
        --out results.ods
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from odf.opendocument import OpenDocumentSpreadsheet
from odf.style import Style, TableColumnProperties, TableCellProperties, TextProperties
from odf.number import NumberStyle, PercentageStyle, Number, Text as NumberText
from odf.table import Table, TableColumn, TableRow, TableCell
from odf.text import P, A

from common import col_letter, parse_years, normalize_payload

# --- Papers sheet layout -----------------------------------------------
# Fixed block is 11 columns (A..K); every topic after that adds a 3-column
# Score/Relevance/Reason triple. Only the fixed-block letters are constant
# across runs -- the topic columns depend on how many topics are in play,
# so those are computed via score_col()/relevance_col()/reason_col().
FIXED_COLUMNS = [
    ("Paper ID", "3.5cm"),
    ("Title", "9cm"),
    ("Authors", "6cm"),
    ("Year", "1.4cm"),
    ("Venue key", "2cm"),
    ("Venue", "5cm"),
    ("Sources", "4cm"),
    ("Link", "6cm"),
    ("Best score", "1.8cm"),
    ("Best topic", "2.6cm"),
    ("Kept (any)", "1.8cm"),
]
FIXED_N = len(FIXED_COLUMNS)

PID_COL, TITLE_COL, AUTHORS_COL, YEAR_COL, VENUE_KEY_COL, VENUE_COL, \
    SOURCES_COL, LINK_COL, BEST_SCORE_COL, BEST_TOPIC_COL, KEPT_COL = \
    [col_letter(i) for i in range(FIXED_N)]


def score_col(topic_index):
    return col_letter(FIXED_N + 3 * topic_index)


def relevance_col(topic_index):
    return col_letter(FIXED_N + 3 * topic_index + 1)


def reason_col(topic_index):
    return col_letter(FIXED_N + 3 * topic_index + 2)


# --- Summary sheet layout ------------------------------------------------
# Always 11 static columns (A..K) regardless of topic count -- only the
# *ranges Summary formulas point into* on Papers need dynamic letters.
SUMMARY_COLUMNS = [
    ("Topic", "3cm"),
    ("Venue", "2.4cm"),
    ("Year", "1.8cm"),
    ("Fetched", "2cm"),
    ("Matched", "2cm"),
    ("Direct", "2.2cm"),
    ("Strong", "2.4cm"),
    ("Moderate", "2.6cm"),
    ("Weak", "2.2cm"),
    ("Avg score", "2.2cm"),
    ("Matched share", "2.8cm"),
]
SUM_TOPIC_L, SUM_VENUE_L, SUM_YEAR_L, SUM_FETCHED_L, SUM_MATCHED_L = \
    [col_letter(i) for i in range(5)]

# Config sheet cell addresses (B column holds the values).
CFG_DIRECT = "[Config.$B$2]"
CFG_STRONG = "[Config.$B$3]"
CFG_MODERATE = "[Config.$B$4]"
CFG_KEEP = "[Config.$B$5]"

CONFIG_ROWS = [
    ("Direct threshold (score >=)", 0.99,
     "At or above this a paper counts as directly about the topic."),
    ("Strong threshold (score >=)", 0.7,
     "Strongly related: at or above this but below the Direct threshold."),
    ("Moderate threshold (score >=)", 0.4,
     "Moderately related: at or above this but below the Strong threshold."),
    ("Keep threshold (score >=)", 0.3,
     "Papers below this are excluded from every Summary count (Kept = 0). "
     "They stay listed on Papers so you can lower the bar and see them again."),
]

PIVOT_CAVEAT = (
    "Summary rows where Venue and/or Year read '(all)' are roll-ups, not "
    "additional fetched papers -- filter them out before pivoting over this "
    "sheet or counts will be double- or triple-counted.")


def relevance_band(score, direct, strong, moderate):
    """Python mirror of the in-sheet formula, used for the cached cell values."""
    if score >= direct:
        return "Direct"
    if score >= strong:
        return "Strong"
    if score >= moderate:
        return "Moderate"
    return "Weak"


def make_styles(doc):
    styles = {}

    header = Style(name="Header", family="table-cell")
    header.addElement(TableCellProperties(backgroundcolor="#d9e1f2"))
    header.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(header)
    styles["header"] = header

    wrap = Style(name="Wrap", family="table-cell")
    wrap.addElement(TableCellProperties(wrapoption="wrap", verticalalign="top"))
    doc.automaticstyles.addElement(wrap)
    styles["wrap"] = wrap

    total = Style(name="Total", family="table-cell")
    total.addElement(TableCellProperties(backgroundcolor="#eef2f9"))
    total.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(total)
    styles["total"] = total

    # Editable-input look, so the Config values are visibly the knobs.
    knob = Style(name="Knob", family="table-cell")
    knob.addElement(TableCellProperties(backgroundcolor="#fff2cc"))
    knob.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(knob)
    styles["knob"] = knob

    dec2 = NumberStyle(name="Dec2")
    dec2.addElement(Number(decimalplaces="2", minintegerdigits="1"))
    doc.styles.addElement(dec2)

    pct = PercentageStyle(name="Pct0")
    pct.addElement(Number(decimalplaces="0", minintegerdigits="1"))
    pct.addElement(NumberText(text="%"))
    doc.styles.addElement(pct)

    score_style = Style(name="ScoreCell", family="table-cell", datastylename="Dec2")
    score_style.addElement(TableCellProperties(verticalalign="top"))
    doc.automaticstyles.addElement(score_style)
    styles["score"] = score_style

    pct_style = Style(name="PctCell", family="table-cell", datastylename="Pct0")
    doc.automaticstyles.addElement(pct_style)
    styles["pct"] = pct_style

    pct_total = Style(name="PctTotal", family="table-cell", datastylename="Pct0")
    pct_total.addElement(TableCellProperties(backgroundcolor="#eef2f9"))
    pct_total.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(pct_total)
    styles["pct_total"] = pct_total

    dec2_total = Style(name="Dec2Total", family="table-cell", datastylename="Dec2")
    dec2_total.addElement(TableCellProperties(backgroundcolor="#eef2f9"))
    dec2_total.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(dec2_total)
    styles["dec2_total"] = dec2_total

    return styles


def add_columns(doc, table, columns):
    for _, width in columns:
        col_style = Style(name=f"co_{table.getAttribute('name')}_{width}",
                          family="table-column")
        col_style.addElement(TableColumnProperties(columnwidth=width))
        doc.automaticstyles.addElement(col_style)
        table.addElement(TableColumn(stylename=col_style))


def header_row(columns, style):
    row = TableRow()
    for name, _ in columns:
        cell = TableCell(stylename=style, valuetype="string")
        cell.addElement(P(text=name))
        row.addElement(cell)
    return row


def str_cell(value, style=None):
    cell = TableCell(valuetype="string", stylename=style) if style \
        else TableCell(valuetype="string")
    cell.addElement(P(text="" if value is None else str(value)))
    return cell


def num_cell(value, text=None, style=None):
    kwargs = {"valuetype": "float", "value": float(value)}
    if style:
        kwargs["stylename"] = style
    cell = TableCell(**kwargs)
    cell.addElement(P(text=text if text is not None else str(value)))
    return cell


def blank_cell(style=None):
    """A genuinely empty cell -- no office:value-type at all. Used for a
    topic's Score when a paper was never scored against it, so COUNTIFS /
    AVERAGEIFS / MAX on that column correctly treat it as absent rather than
    as a 0 ("judged unrelated")."""
    return TableCell(stylename=style) if style else TableCell()


def formula_cell(formula, value, text=None, style=None, valuetype="float"):
    """A cell whose content is a formula, carrying a cached value for viewers
    that render the file without recalculating."""
    kwargs = {"formula": formula, "valuetype": valuetype}
    if style:
        kwargs["stylename"] = style
    if valuetype == "string":
        kwargs["stringvalue"] = str(value)
    elif valuetype == "percentage":
        kwargs["value"] = float(value)
    else:
        kwargs["value"] = float(value)
    cell = TableCell(**kwargs)
    cell.addElement(P(text=text if text is not None else str(value)))
    return cell


def link_cell(url, style):
    cell = TableCell(stylename=style, valuetype="string")
    para = P()
    if url:
        para.addElement(A(href=url, text=url))
    cell.addElement(para)
    return cell


def build_config_sheet(doc, styles, defaults):
    """The knobs. Everything else in the workbook points here."""
    table = Table(name="Config")
    add_columns(doc, table, [("Setting", "7cm"), ("Value", "2.5cm"), ("Notes", "14cm")])
    table.addElement(header_row(
        [("Setting", ""), ("Value", ""), ("Notes", "")], styles["header"]))
    for (label, fallback, note), value in zip(CONFIG_ROWS, defaults):
        row = TableRow()
        row.addElement(str_cell(label, styles["wrap"]))
        row.addElement(num_cell(value, text=f"{value:g}", style=styles["knob"]))
        row.addElement(str_cell(note, styles["wrap"]))
        table.addElement(row)

    table.addElement(TableRow())
    hint = TableRow()
    hint.addElement(str_cell("How to use", styles["header"]))
    hint.addElement(str_cell("", styles["header"]))
    hint.addElement(str_cell(
        "Edit the yellow cells above. Every topic's Relevance column on Papers, "
        "and every number on Summary, is a formula that references these cells "
        "-- one shared threshold block applies to every 'Score: <topic>' column, "
        "so the whole workbook re-bands itself on recalculation.",
        styles["wrap"]))
    table.addElement(hint)
    doc.spreadsheet.addElement(table)


def best_topic_formula(topics, r, score_cols):
    """Nested IF picking the (first, in topic order) topic whose Score
    column equals this row's Best score -- mirrors the Python tie-break in
    _attach_best()."""
    expr = '""'
    for i in reversed(range(len(topics))):
        name = topics[i]["name"].replace('"', '""')
        expr = f'IF([.{BEST_SCORE_COL}{r}]=[.{score_cols[i]}{r}];"{name}";{expr})'
    return f"of:={expr}"


def build_papers_sheet(doc, styles, papers, topics, cfg):
    wrap, score_style = styles["wrap"], styles["score"]
    table = Table(name="Papers")

    columns = list(FIXED_COLUMNS)
    for t in topics:
        name = t["name"]
        columns += [(f"Score: {name}", "1.6cm"), (f"Relevance: {name}", "2.4cm"),
                    (f"Reason: {name}", "9cm")]
    add_columns(doc, table, columns)
    table.addElement(header_row(columns, styles["header"]))

    direct_t, strong_t, moderate_t, keep_t = cfg
    score_cols = [score_col(i) for i in range(len(topics))]

    for row_i, c in enumerate(papers):
        r = row_i + 2  # 1-based sheet row (row 1 is the header)
        authors = c.get("authors", [])
        authors = ", ".join(authors) if isinstance(authors, list) else (authors or "")
        sources = c.get("sources", [])
        sources = ", ".join(sources) if isinstance(sources, list) else (sources or "")

        best_score = c["_best_score"]
        best_topic = c["_best_topic"] or ""
        is_kept = 1 if best_score >= keep_t else 0

        row = TableRow()
        row.addElement(str_cell(c.get("paper_id", ""), wrap))
        row.addElement(str_cell(c.get("title", ""), wrap))
        row.addElement(str_cell(authors, wrap))
        row.addElement(num_cell(c["year"], text=str(c["year"]), style=wrap)
                       if c.get("year") else str_cell("", wrap))
        row.addElement(str_cell(c.get("venue_key", ""), wrap))
        row.addElement(str_cell(c.get("venue", ""), wrap))
        row.addElement(str_cell(sources, wrap))
        row.addElement(link_cell(c.get("link", "") or "", wrap))

        row.addElement(formula_cell(
            f'of:=MAX({";".join(f"[.{sc}{r}]" for sc in score_cols)})',
            best_score, text=f"{best_score:.2f}", style=score_style))
        row.addElement(formula_cell(
            best_topic_formula(topics, r, score_cols),
            best_topic, style=wrap, valuetype="string"))
        row.addElement(formula_cell(
            f'of:=IF([.{BEST_SCORE_COL}{r}]>={CFG_KEEP};1;0)',
            is_kept, text=str(is_kept), style=wrap))

        for i, t in enumerate(topics):
            name = t["name"]
            sc = score_cols[i]
            score_val = c["scores"].get(name)

            if score_val is None:
                row.addElement(blank_cell(wrap))
            else:
                row.addElement(num_cell(score_val, text=f"{score_val:.2f}", style=score_style))

            relevance_val = "" if score_val is None \
                else relevance_band(score_val, direct_t, strong_t, moderate_t)
            row.addElement(formula_cell(
                f'of:=IF([.{sc}{r}]="";"";'
                f'IF([.{sc}{r}]>={CFG_DIRECT};"Direct";'
                f'IF([.{sc}{r}]>={CFG_STRONG};"Strong";'
                f'IF([.{sc}{r}]>={CFG_MODERATE};"Moderate";"Weak"))))',
                relevance_val, style=wrap, valuetype="string"))

            reason_val = c["reasons"].get(name, "") if score_val is not None else ""
            row.addElement(str_cell(reason_val, wrap))

        table.addElement(row)

    doc.spreadsheet.addElement(table)


def build_score_index(papers, topics):
    """topic name -> venue_key -> year -> [scores]. Built only from the
    scores that actually made it onto Papers, so a paper missing a topic's
    score simply contributes nothing to that topic's buckets -- the same
    "absent, not zero" rule the sheet's blank cells encode."""
    idx = {t["name"]: {} for t in topics}
    for c in papers:
        vk = c.get("venue_key", "")
        yr = c.get("year")
        if yr is None:
            continue
        for t in topics:
            name = t["name"]
            val = c["scores"].get(name)
            if val is None:
                continue
            idx[name].setdefault(vk, {}).setdefault(yr, []).append(val)
    return idx


def gather_scores(idx, topic_name, venue, year):
    """venue=None means every venue; year=None means every year."""
    per_venue = idx.get(topic_name, {})
    vks = list(per_venue) if venue is None else ([venue] if venue in per_venue else [])
    out = []
    for vk in vks:
        per_year = per_venue.get(vk, {})
        if year is None:
            for lst in per_year.values():
                out.extend(lst)
        else:
            out.extend(per_year.get(year, []))
    return out


def band_counts(scores, direct_t, strong_t, moderate_t, keep_t):
    """Python mirror of the Summary COUNTIFS/AVERAGEIFS cascade, for cached values."""
    kept = [s for s in scores if s >= keep_t]
    matched = len(kept)
    n_direct = sum(1 for s in kept if s >= direct_t)
    n_strong = sum(1 for s in kept if strong_t <= s < direct_t)
    n_moderate = sum(1 for s in kept if moderate_t <= s < strong_t)
    n_weak = sum(1 for s in kept if keep_t <= s < moderate_t)
    avg = sum(kept) / matched if matched else 0.0
    return matched, n_direct, n_strong, n_moderate, n_weak, avg


def build_summary_sheet(doc, styles, topics, venues_list, years_list, fetched_by_vy,
                        score_index, cfg, last_row):
    """Tidy long form: one row per Topic x Venue x Year, then '(all)'
    roll-ups per venue, per year, and grand-total -- repeated for every
    topic in CLI order. Every numeric cell is a formula over Papers;
    AVERAGEIFS replaces the old TOTAL-row SUMPRODUCT special case because
    gating is by score range now, not by a per-topic Kept flag column, so
    one formula shape covers ordinary rows and roll-ups alike."""
    wrap, total = styles["wrap"], styles["total"]
    table = Table(name="Summary")
    add_columns(doc, table, SUMMARY_COLUMNS)
    table.addElement(header_row(SUMMARY_COLUMNS, styles["header"]))

    direct_t, strong_t, moderate_t, keep_t = cfg

    fetched_by_venue = {v: sum(n for (vv, _yy), n in fetched_by_vy.items() if vv == v)
                        for v in venues_list}
    fetched_by_year = {y: sum(n for (_vv, yy), n in fetched_by_vy.items() if yy == y)
                       for y in years_list}
    fetched_total = sum(fetched_by_vy.values())

    present_vy = [(v, y) for v in venues_list for y in years_list
                 if fetched_by_vy.get((v, y), 0) > 0]

    venue_rng = f"[Papers.${VENUE_KEY_COL}$2:.${VENUE_KEY_COL}${last_row}]"
    year_rng = f"[Papers.${YEAR_COL}$2:.${YEAR_COL}${last_row}]"

    r = 2
    for ti, t in enumerate(topics):
        name = t["name"]
        score_letter = score_col(ti)
        score_rng = f"[Papers.${score_letter}$2:.${score_letter}${last_row}]"

        def emit(venue, year, fetched, is_rollup):
            """venue/year is a real value for an ordinary row, or Python
            None for a roll-up -- None is the actual sentinel gather_scores()
            understands as "every venue"/"every year"; "(all)" is only ever
            a display string, never passed down into the aggregation."""
            nonlocal r
            scores = gather_scores(score_index, name, venue, year)
            matched, n_direct, n_strong, n_moderate, n_weak, avg = band_counts(
                scores, direct_t, strong_t, moderate_t, keep_t)
            share = matched / fetched if fetched else 0.0
            style = total if is_rollup else wrap
            pct_style = styles["pct_total"] if is_rollup else styles["pct"]
            dec_style = styles["dec2_total"] if is_rollup else styles["score"]

            parts = []
            if venue is not None:
                parts.append(f'{venue_rng};[.${SUM_VENUE_L}{r}]')
            if year is not None:
                parts.append(f'{year_rng};[.${SUM_YEAR_L}{r}]')
            parts.append(f'{score_rng};">="&{CFG_KEEP}')
            base = ";".join(parts)

            row = TableRow()
            row.addElement(str_cell(name, style))
            row.addElement(str_cell(venue if venue is not None else "(all)", style))
            row.addElement(str_cell("(all)", style) if year is None
                           else num_cell(year, text=str(year), style=style))
            row.addElement(num_cell(fetched, text=str(fetched), style=style))
            row.addElement(formula_cell(
                f'of:=COUNTIFS({base})', matched, text=str(matched), style=style))
            row.addElement(formula_cell(
                f'of:=COUNTIFS({base};{score_rng};">="&{CFG_DIRECT})',
                n_direct, text=str(n_direct), style=style))
            row.addElement(formula_cell(
                f'of:=COUNTIFS({base};{score_rng};">="&{CFG_STRONG};{score_rng};"<"&{CFG_DIRECT})',
                n_strong, text=str(n_strong), style=style))
            row.addElement(formula_cell(
                f'of:=COUNTIFS({base};{score_rng};">="&{CFG_MODERATE};{score_rng};"<"&{CFG_STRONG})',
                n_moderate, text=str(n_moderate), style=style))
            row.addElement(formula_cell(
                f'of:=COUNTIFS({base};{score_rng};">="&{CFG_KEEP};{score_rng};"<"&{CFG_MODERATE})',
                n_weak, text=str(n_weak), style=style))
            row.addElement(formula_cell(
                f'of:=IF([.{SUM_MATCHED_L}{r}]>0;AVERAGEIFS({score_rng};{base});0)',
                avg, text=f"{avg:.2f}", style=dec_style))
            row.addElement(formula_cell(
                f'of:=IF([.{SUM_FETCHED_L}{r}]>0;[.{SUM_MATCHED_L}{r}]/[.{SUM_FETCHED_L}{r}];0)',
                share, text=f"{share:.0%}", style=pct_style, valuetype="percentage"))
            table.addElement(row)
            r += 1

        for v, y in present_vy:
            emit(v, y, fetched_by_vy.get((v, y), 0), False)
        for v in venues_list:
            emit(v, None, fetched_by_venue.get(v, 0), True)
        for y in years_list:
            emit(None, y, fetched_by_year.get(y, 0), True)
        emit(None, None, fetched_total, True)

    table.addElement(TableRow())
    note = TableRow()
    note.addElement(str_cell(PIVOT_CAVEAT, wrap))
    table.addElement(note)

    doc.spreadsheet.addElement(table)


def build_meta_sheet(doc, styles, merged, all_candidates, papers, unscored):
    header, wrap = styles["header"], styles["wrap"]
    table = Table(name="Meta")
    add_columns(doc, table, [("Field", "6cm"), ("Value", "13cm")])
    table.addElement(header_row([("Field", ""), ("Value", "")], header))

    rows = []
    for t in merged["topics"]:
        rows.append((f"Topic: {t['name']}", t.get("query", "")))

    for v in merged["venues"]:
        desc = merged["venue_descriptions"].get(v, "")
        fetched = sum(n for (vv, _yy), n in merged["fetched_by_vy"].items() if vv == v)
        label = f"{desc} -- {fetched} papers fetched" if desc else f"{fetched} papers fetched"
        rows.append((f"Venue: {v}", label))

    rows.append(("Years", merged.get("years_label", "")))
    rows.append(("Fetch mode(s)", "; ".join(merged["modes"])))
    rows.append(("Sources cross-checked", ", ".join(merged["crosschecked_sources"])
                 if merged["crosschecked_sources"]
                 else "Semantic Scholar only (no cross-check run)"))
    rows.append(("Papers fetched (union across inputs)", len(all_candidates)))
    rows.append(("Papers scored (listed on Papers, >=1 topic)", len(papers)))
    rows.append(("Papers with no score for any topic (omitted)", unscored))

    by_venue_abs = {}
    for c in all_candidates:
        vk = c.get("venue_key", "")
        total, missing = by_venue_abs.get(vk, (0, 0))
        total += 1
        if not c.get("abstract"):
            missing += 1
        by_venue_abs[vk] = (total, missing)
    for v in merged["venues"]:
        total, missing = by_venue_abs.get(v, (0, 0))
        pct = f"{missing / total:.0%}" if total else "n/a"
        rows.append((f"Papers without an abstract: {v}", f"{missing} of {total} ({pct})"))

    rows.append(("Thresholds", "editable on the Config sheet; shared by every "
                                "'Score: <topic>' column"))
    rows.append(("Pivot caveat", PIVOT_CAVEAT))
    rows.append(("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")))

    for field, value in rows:
        row = TableRow()
        row.addElement(str_cell(field, wrap))
        row.addElement(str_cell(value, wrap))
        table.addElement(row)
    doc.spreadsheet.addElement(table)


def merge_payloads(datas):
    """Merge several normalize_payload()-d JSON payloads into one view:
    union of topics (first-seen CLI/file order), union of candidates by
    paper_id (per-paper scores/reasons merged key-wise, first input to
    score a given topic for a given paper wins), and per-(venue_key, year)
    Fetched counts taken as the MAX across inputs rather than summed --
    the four legacy files in this repo are largely the same underlying
    fetch re-scored for different topics, so summing would multiply-count
    the same papers instead of reporting how many the venue/year actually
    has.
    """
    topics, seen_topics = [], set()
    venues, seen_venues = [], set()
    venue_descriptions = {}
    modes, seen_modes = [], set()
    crosschecked, seen_cc = [], set()
    year_lo = year_hi = None
    candidates = {}
    fetched_by_vy = {}

    for data in datas:
        meta = data.get("meta", {})

        for t in meta.get("topics", []):
            tname = t.get("name")
            if tname and tname not in seen_topics:
                seen_topics.add(tname)
                topics.append({"name": tname, "query": t.get("query", "")})

        for v in meta.get("venues", []):
            if v and v not in seen_venues:
                seen_venues.add(v)
                venues.append(v)

        vdescs = meta.get("venue_descriptions")
        if vdescs:
            for k, desc in vdescs.items():
                venue_descriptions.setdefault(k, desc)
        elif meta.get("venue_description") and meta.get("venue"):
            venue_descriptions.setdefault(meta["venue"], meta["venue_description"])

        mode = meta.get("mode")
        if mode and mode not in seen_modes:
            seen_modes.add(mode)
            modes.append(mode)

        for s in meta.get("crosschecked_sources", []):
            if s not in seen_cc:
                seen_cc.add(s)
                crosschecked.append(s)

        yspec = meta.get("years")
        if yspec:
            try:
                _, (lo, hi) = parse_years(str(yspec))
                year_lo = lo if year_lo is None else min(year_lo, lo)
                year_hi = hi if year_hi is None else max(year_hi, hi)
            except SystemExit:
                pass  # tolerate an odd/legacy years string; best-effort only

        this_counts = {}
        for c in data.get("candidates", []):
            yr = c.get("year")
            if yr is None:
                continue
            key = (c.get("venue_key", ""), yr)
            this_counts[key] = this_counts.get(key, 0) + 1
        for key, n in this_counts.items():
            fetched_by_vy[key] = max(fetched_by_vy.get(key, 0), n)

        for c in data.get("candidates", []):
            pid = c.get("paper_id")
            if not pid:
                continue
            merged = candidates.get(pid)
            if merged is None:
                merged = {
                    "paper_id": pid,
                    "title": c.get("title", ""),
                    "authors": c.get("authors", []),
                    "year": c.get("year"),
                    "venue": c.get("venue", ""),
                    "venue_key": c.get("venue_key", ""),
                    "sources": c.get("sources", []),
                    "abstract": c.get("abstract", ""),
                    "scores": {},
                    "reasons": {},
                }
                candidates[pid] = merged
            elif not merged.get("abstract") and c.get("abstract"):
                merged["abstract"] = c["abstract"]

            cscores = c.get("scores") or {}
            creasons = c.get("reasons") or {}
            for tname, val in cscores.items():
                if val is not None and tname not in merged["scores"]:
                    merged["scores"][tname] = val
                    merged["reasons"][tname] = creasons.get(tname, "")

    # Every merged candidate needs an explicit (possibly-missing) entry per
    # topic so build_score_index()/build_papers_sheet() can treat a topic
    # this paper was never scored for as an ordinary lookup miss.
    for cand in candidates.values():
        for t in topics:
            cand["scores"].setdefault(t["name"], None)
            cand["reasons"].setdefault(t["name"], "")

    if year_lo is None:
        years_label = ""
    elif year_lo == year_hi:
        years_label = str(year_lo)
    else:
        years_label = f"{year_lo}-{year_hi}"

    return {
        "topics": topics,
        "venues": venues,
        "venue_descriptions": venue_descriptions,
        "modes": modes,
        "crosschecked_sources": crosschecked,
        "years_label": years_label,
        "candidates": candidates,
        "fetched_by_vy": fetched_by_vy,
    }


def _attach_best(papers, topics):
    """Compute each paper's Best score / Best topic once, in topic order so
    ties resolve to the earliest topic -- must match best_topic_formula()'s
    nested-IF tie-break exactly."""
    for c in papers:
        best_val, best_name = None, None
        for t in topics:
            v = c["scores"].get(t["name"])
            if v is not None and (best_val is None or v > best_val):
                best_val, best_name = v, t["name"]
        c["_best_score"] = best_val
        c["_best_topic"] = best_name


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="infiles", action="append", required=True,
                    help="Scored candidates JSON. Repeatable: pass it several "
                         "times to merge multiple topics' scored files (or "
                         "legacy single-topic exports) into one workbook.")
    ap.add_argument("--out", default="results.ods")
    ap.add_argument("--threshold", type=float, default=0.3,
                    help="Initial value of the in-sheet keep threshold (default "
                         "0.3). Papers below it are still listed but flagged "
                         "Kept=0 and excluded from the Summary counts; the user "
                         "can change it on the Config sheet.")
    ap.add_argument("--direct-threshold", type=float, default=0.99,
                    help="Initial Direct-band cut-off (default 0.99).")
    ap.add_argument("--strong-threshold", type=float, default=0.7,
                    help="Initial Strong-band cut-off (default 0.7).")
    ap.add_argument("--moderate-threshold", type=float, default=0.4,
                    help="Initial Moderate-band cut-off (default 0.4).")
    args = ap.parse_args()

    cfg = (args.direct_threshold, args.strong_threshold,
           args.moderate_threshold, args.threshold)
    if not (cfg[0] >= cfg[1] >= cfg[2]):
        sys.exit("Thresholds must satisfy direct >= strong >= moderate.")

    datas = []
    for path in args.infiles:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        datas.append(normalize_payload(raw))

    merged = merge_payloads(datas)
    topics = merged["topics"]
    if not topics:
        sys.exit("No topics found across the input file(s).")

    all_candidates = list(merged["candidates"].values())
    papers = [c for c in all_candidates if any(v is not None for v in c["scores"].values())]
    _attach_best(papers, topics)
    papers.sort(key=lambda c: (-c["_best_score"], c.get("year") or 0))

    unscored = len(all_candidates) - len(papers)
    if unscored:
        sys.stderr.write(
            f"WARNING: {unscored} candidates had no score for any topic and "
            f"were omitted. Did the agent finish scoring?\n")

    fetched_by_vy = merged["fetched_by_vy"]
    venues_list = [v for v in merged["venues"] if any(vv == v for vv, _ in fetched_by_vy)]
    if not venues_list:
        venues_list = sorted({vv for vv, _ in fetched_by_vy})
    years_list = sorted({yy for _, yy in fetched_by_vy})

    score_index = build_score_index(papers, topics)

    doc = OpenDocumentSpreadsheet()
    styles = make_styles(doc)
    build_papers_sheet(doc, styles, papers, topics, cfg)
    build_summary_sheet(doc, styles, topics, venues_list, years_list, fetched_by_vy,
                        score_index, cfg, len(papers) + 1)
    build_config_sheet(doc, styles, cfg)
    build_meta_sheet(doc, styles, merged, all_candidates, papers, unscored)
    doc.save(args.out)

    partial = sum(1 for c in papers
                 if any(v is None for v in c["scores"].values()))
    below = sum(1 for c in papers if c["_best_score"] < args.threshold)
    sys.stderr.write(
        f"Wrote {len(papers)} scored papers to {args.out} "
        f"({len(topics)} topic(s): {', '.join(t['name'] for t in topics)}; "
        f"{len(venues_list)} venue(s): {', '.join(venues_list)}).\n"
        f"  {partial} papers have a score for only SOME topics (empty cells "
        f"for the rest, by design).\n"
        f"  {len(papers) - below} are above the initial keep threshold "
        f"({args.threshold}) by best score; {below} are listed but excluded "
        f"from the Summary counts until the threshold is lowered on Config.\n")


if __name__ == "__main__":
    main()
