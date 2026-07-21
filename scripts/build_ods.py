#!/usr/bin/env python3
"""
Turn a scored candidates JSON file into an ODS spreadsheet designed for
*topic statistics*.

Reads the JSON produced by fetch_candidates.py *after the agent has filled in
the `score` and `reason` fields*.

Unlike earlier versions, this script does **not** bake the relevance thresholds
into the output. Every scored candidate is written to the Papers sheet, and the
band cut-offs live in a `Config` sheet as editable cells. The `Relevance`,
`Direct` and `Kept` columns, and the whole Summary sheet, are ODF **formulas**
that recompute the moment the user edits a threshold in `Config`.

The workbook has four sheets:

  * Papers  — one tidy row per scored paper:
              Topic | Paper ID | Title | Authors | Year | Venue | Score |
              Relevance | Direct | Kept | Sources | Reason | Link
              (`Relevance`, `Direct`, `Kept` are formulas driven by Config.)
  * Summary — a per-year cross-tab (Fetched, Matched, Direct, Strong,
              Moderate, Weak, Avg score, Matched share) plus a TOTAL row,
              entirely built from COUNTIFS/AVERAGEIFS over Papers.
  * Config  — the editable thresholds. Change a number here and Papers +
              Summary follow.
  * Meta    — the run parameters, so the export is self-describing.

No charts are produced (by design).

Usage:
    python3 build_ods.py --in candidates.scored.json --out results.ods --threshold 0.3
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

PAPER_COLUMNS = [
    ("Topic", "3cm"),
    ("Paper ID", "3.5cm"),
    ("Title", "9cm"),
    ("Authors", "6cm"),
    ("Year", "1.4cm"),
    ("Venue", "5cm"),
    ("Score", "1.6cm"),
    ("Relevance", "2.4cm"),
    ("Direct", "1.6cm"),
    ("Kept", "1.4cm"),
    ("Sources", "4cm"),
    ("Reason", "9cm"),
    ("Link", "6cm"),
]

# Column letters on the Papers sheet, derived from PAPER_COLUMNS order.
COL_YEAR = "E"
COL_SCORE = "G"
COL_DIRECT = "I"
COL_KEPT = "J"

SUMMARY_COLUMNS = [
    ("Year", "1.8cm"),
    ("Fetched", "2cm"),
    ("Matched", "2cm"),
    ("Direct", "2.4cm"),
    ("Strong", "2.6cm"),
    ("Moderate", "2.8cm"),
    ("Weak", "2.4cm"),
    ("Avg score", "2.2cm"),
    ("Matched share", "2.8cm"),
]

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
        "Edit the yellow cells above. The Relevance / Direct / Kept columns on "
        "Papers and every number on Summary are formulas that reference them, "
        "so the whole workbook re-bands itself on recalculation.",
        styles["wrap"]))
    table.addElement(hint)
    doc.spreadsheet.addElement(table)


def build_papers_sheet(doc, styles, papers, topic, cfg):
    wrap = styles["wrap"]
    table = Table(name="Papers")
    add_columns(doc, table, PAPER_COLUMNS)
    table.addElement(header_row(PAPER_COLUMNS, styles["header"]))

    direct_t, strong_t, moderate_t, keep_t = cfg
    for i, c in enumerate(papers):
        r = i + 2  # 1-based sheet row (row 1 is the header)
        score = float(c["score"])
        authors = c.get("authors", [])
        authors = ", ".join(authors) if isinstance(authors, list) else authors
        sources = c.get("sources", [])
        sources = ", ".join(sources) if isinstance(sources, list) else sources

        band = relevance_band(score, direct_t, strong_t, moderate_t)
        is_direct = 1 if score >= direct_t else 0
        is_kept = 1 if score >= keep_t else 0

        row = TableRow()
        row.addElement(str_cell(topic, wrap))
        row.addElement(str_cell(c.get("paper_id", ""), wrap))
        row.addElement(str_cell(c.get("title", ""), wrap))
        row.addElement(str_cell(authors, wrap))
        row.addElement(num_cell(c.get("year", 0), text=str(c.get("year", "")), style=wrap)
                       if c.get("year") else str_cell("", wrap))
        row.addElement(str_cell(c.get("venue", ""), wrap))
        row.addElement(num_cell(score, text=f"{score:.2f}", style=styles["score"]))
        row.addElement(formula_cell(
            f'of:=IF([.{COL_SCORE}{r}]>={CFG_DIRECT};"Direct";'
            f'IF([.{COL_SCORE}{r}]>={CFG_STRONG};"Strong";'
            f'IF([.{COL_SCORE}{r}]>={CFG_MODERATE};"Moderate";"Weak")))',
            band, style=wrap, valuetype="string"))
        row.addElement(formula_cell(
            f'of:=IF([.{COL_SCORE}{r}]>={CFG_DIRECT};1;0)',
            is_direct, text=str(is_direct), style=wrap))
        row.addElement(formula_cell(
            f'of:=IF([.{COL_SCORE}{r}]>={CFG_KEEP};1;0)',
            is_kept, text=str(is_kept), style=wrap))
        row.addElement(str_cell(sources, wrap))
        row.addElement(str_cell(c.get("reason", ""), wrap))
        row.addElement(link_cell(c.get("link", "") or "", wrap))
        table.addElement(row)

    doc.spreadsheet.addElement(table)


def build_summary_sheet(doc, styles, candidates, papers, cfg):
    """Every cell here is a formula over the Papers sheet, so editing a
    threshold on Config re-derives the whole table."""
    wrap, total = styles["wrap"], styles["total"]
    table = Table(name="Summary")
    add_columns(doc, table, SUMMARY_COLUMNS)
    table.addElement(header_row(SUMMARY_COLUMNS, styles["header"]))

    direct_t, strong_t, moderate_t, keep_t = cfg
    last = len(papers) + 1  # last populated Papers row
    if last < 2:
        last = 2  # keep the ranges syntactically valid on an empty export

    years_rng = f"[Papers.${COL_YEAR}$2:.${COL_YEAR}${last}]"
    score_rng = f"[Papers.${COL_SCORE}$2:.${COL_SCORE}${last}]"
    kept_rng = f"[Papers.${COL_KEPT}$2:.${COL_KEPT}${last}]"

    years = sorted({c.get("year") for c in candidates if c.get("year")})
    fetched_by_year = {}
    for c in candidates:
        if c.get("year"):
            fetched_by_year[c["year"]] = fetched_by_year.get(c["year"], 0) + 1
    scores_by_year = {}
    for p in papers:
        scores_by_year.setdefault(p.get("year"), []).append(float(p["score"]))

    def cached(scores, lo=None, hi=None):
        """Cached count of kept scores in [lo, hi)."""
        n = 0
        for s in scores:
            if s < keep_t:
                continue
            if lo is not None and s < lo:
                continue
            if hi is not None and s >= hi:
                continue
            n += 1
        return n

    def emit_row(r, label, fetched, scores, is_total, style, pct_style, dec_style):
        """`r` is the 1-based sheet row this lands on."""
        # A year criterion only makes sense on the per-year rows; TOTAL counts
        # everything by matching on the Kept flag alone.
        def countifs(extra=""):
            if is_total:
                return f'of:=COUNTIFS({kept_rng};1{extra})'
            return f'of:=COUNTIFS({years_rng};[.$A{r}];{kept_rng};1{extra})'

        kept_scores = [s for s in scores if s >= keep_t]
        matched = len(kept_scores)
        avg = sum(kept_scores) / matched if matched else 0.0
        share = matched / fetched if fetched else 0.0

        row = TableRow()
        row.addElement(str_cell(label, style) if isinstance(label, str)
                       else num_cell(label, text=str(label), style=style))
        row.addElement(num_cell(fetched, text=str(fetched), style=style))
        row.addElement(formula_cell(countifs(), matched, text=str(matched), style=style))

        n_direct = cached(scores, lo=direct_t)
        row.addElement(formula_cell(
            countifs(f';{score_rng};">="&{CFG_DIRECT}'),
            n_direct, text=str(n_direct), style=style))

        n_strong = cached(scores, lo=strong_t, hi=direct_t)
        row.addElement(formula_cell(
            countifs(f';{score_rng};">="&{CFG_STRONG};{score_rng};"<"&{CFG_DIRECT}'),
            n_strong, text=str(n_strong), style=style))

        n_moderate = cached(scores, lo=moderate_t, hi=strong_t)
        row.addElement(formula_cell(
            countifs(f';{score_rng};">="&{CFG_MODERATE};{score_rng};"<"&{CFG_STRONG}'),
            n_moderate, text=str(n_moderate), style=style))

        n_weak = cached(scores, hi=moderate_t)
        row.addElement(formula_cell(
            countifs(f';{score_rng};"<"&{CFG_MODERATE}'),
            n_weak, text=str(n_weak), style=style))

        if is_total:
            avg_f = f'of:=IF([.C{r}]>0;SUMPRODUCT({kept_rng};{score_rng})/[.C{r}];0)'
        else:
            avg_f = (f'of:=IF([.C{r}]>0;AVERAGEIFS({score_rng};{years_rng};[.$A{r}];'
                     f'{kept_rng};1);0)')
        row.addElement(formula_cell(avg_f, avg, text=f"{avg:.2f}", style=dec_style))

        row.addElement(formula_cell(
            f'of:=IF([.B{r}]>0;[.C{r}]/[.B{r}];0)',
            share, text=f"{share:.0%}", style=pct_style, valuetype="percentage"))
        table.addElement(row)

    r = 2
    for y in years:
        emit_row(r, y, fetched_by_year.get(y, 0), scores_by_year.get(y, []),
                 False, wrap, styles["pct"], styles["score"])
        r += 1

    all_scores = [float(p["score"]) for p in papers]
    emit_row(r, "TOTAL", sum(fetched_by_year.values()), all_scores,
             True, total, styles["pct_total"], styles["dec2_total"])

    doc.spreadsheet.addElement(table)


def build_meta_sheet(doc, styles, meta, n_candidates, n_scored, n_unscored,
                     crosscheck):
    header, wrap = styles["header"], styles["wrap"]
    table = Table(name="Meta")
    add_columns(doc, table, [("Field", "5cm"), ("Value", "12cm")])
    table.addElement(header_row([("Field", ""), ("Value", "")], header))
    rows = [
        ("Topic (keyword)", meta.get("keyword", "")),
        ("Venue", meta.get("venue", "")),
        ("Venue description", meta.get("venue_description", "")),
        ("Years", meta.get("years", "")),
        ("Fetch mode", meta.get("mode", "")),
        ("Recall query", meta.get("query", "")),
        ("Sources cross-checked", ", ".join(crosscheck) if crosscheck
         else "Semantic Scholar only (no cross-check run)"),
        ("Papers fetched", n_candidates),
        ("Papers scored (listed)", n_scored),
        ("Papers unscored (omitted)", n_unscored),
        ("Thresholds", "editable on the Config sheet"),
        ("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")),
    ]
    for field, value in rows:
        row = TableRow()
        row.addElement(str_cell(field, wrap))
        row.addElement(str_cell(value, wrap))
        table.addElement(row)
    doc.spreadsheet.addElement(table)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="infile", required=True)
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

    with open(args.infile, encoding="utf-8") as fh:
        data = json.load(fh)
    meta = data.get("meta", {})
    candidates = data.get("candidates", [])
    topic = meta.get("keyword", "")
    crosscheck = meta.get("crosschecked_sources", [])

    papers, unscored = [], 0
    for c in candidates:
        if c.get("score") is None:
            unscored += 1
            continue
        papers.append(c)
    papers.sort(key=lambda c: (-float(c["score"]), c.get("year", 0)))

    if unscored:
        sys.stderr.write(
            f"WARNING: {unscored} candidates had no score and were omitted. "
            f"Did the agent finish scoring?\n")

    doc = OpenDocumentSpreadsheet()
    styles = make_styles(doc)
    build_papers_sheet(doc, styles, papers, topic, cfg)
    build_summary_sheet(doc, styles, candidates, papers, cfg)
    build_config_sheet(doc, styles, cfg)
    build_meta_sheet(doc, styles, meta, len(candidates), len(papers), unscored,
                     crosscheck)
    doc.save(args.out)

    below = sum(1 for p in papers if float(p["score"]) < args.threshold)
    sys.stderr.write(
        f"Wrote {len(papers)} scored papers to {args.out} across sheets "
        f"Papers / Summary / Config / Meta.\n"
        f"  {len(papers) - below} are above the initial keep threshold "
        f"({args.threshold}); {below} are listed but excluded from the Summary "
        f"counts until the threshold is lowered on the Config sheet.\n")


if __name__ == "__main__":
    main()
