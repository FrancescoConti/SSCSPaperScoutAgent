#!/usr/bin/env python3
"""
Turn a scored candidates JSON file into an ODS spreadsheet designed for
*topic statistics*.

Reads the JSON produced by fetch_candidates.py *after the agent has filled in
the `score` and `reason` fields*. Any candidate with score == null or score
below --threshold is dropped from the Papers sheet (i.e. "poorly related or
unrelated papers are removed"), but dropped papers still count toward the
per-year denominators on the Summary sheet.

The workbook has three sheets:

  * Papers  — one tidy row per kept paper, with analysis-friendly typed
              columns so pivot tables / COUNTIFS are trivial:
              Topic | Paper ID | Title | Authors | Year | Venue | Score |
              Relevance | Direct | Reason | Link
              (`Topic` = the keyword, so several single-topic exports can be
               stacked into one dataset for cross-topic statistics; `Year` is
               numeric; `Score` is a float; `Relevance` is a band; `Direct` is
               1 for a direct match else 0.)
  * Summary — a per-year cross-tab (Fetched, Matched, Direct, Strong,
              Moderate, Weak, Avg score, Matched share) plus a TOTAL row.
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
    ("Reason", "9cm"),
    ("Link", "6cm"),
]

SUMMARY_COLUMNS = [
    ("Year", "1.8cm"),
    ("Fetched", "2cm"),
    ("Matched", "2cm"),
    ("Direct (1.0)", "2.4cm"),
    ("Strong (0.7-0.99)", "3.2cm"),
    ("Moderate (0.4-0.69)", "3.4cm"),
    ("Weak (<0.4)", "2.4cm"),
    ("Avg score", "2cm"),
    ("Matched share", "2.6cm"),
]

BANDS = ["Direct", "Strong", "Moderate", "Weak"]


def relevance_band(score):
    if score >= 0.99:
        return "Direct"
    if score >= 0.7:
        return "Strong"
    if score >= 0.4:
        return "Moderate"
    return "Weak"


def make_styles(doc):
    header = Style(name="Header", family="table-cell")
    header.addElement(TableCellProperties(backgroundcolor="#d9e1f2"))
    header.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(header)

    wrap = Style(name="Wrap", family="table-cell")
    wrap.addElement(TableCellProperties(wrapoption="wrap", verticalalign="top"))
    doc.automaticstyles.addElement(wrap)

    total = Style(name="Total", family="table-cell")
    total.addElement(TableCellProperties(backgroundcolor="#eef2f9"))
    total.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(total)
    return header, wrap, total


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


def link_cell(url, style):
    cell = TableCell(stylename=style, valuetype="string")
    para = P()
    if url:
        para.addElement(A(href=url, text=url))
    cell.addElement(para)
    return cell


def build_papers_sheet(doc, styles, kept, topic):
    header, wrap, _ = styles
    table = Table(name="Papers")
    add_columns(doc, table, PAPER_COLUMNS)
    table.addElement(header_row(PAPER_COLUMNS, header))
    for c in kept:
        score = float(c["score"])
        authors = c.get("authors", [])
        authors = ", ".join(authors) if isinstance(authors, list) else authors
        row = TableRow()
        row.addElement(str_cell(topic, wrap))
        row.addElement(str_cell(c.get("paper_id", ""), wrap))
        row.addElement(str_cell(c.get("title", ""), wrap))
        row.addElement(str_cell(authors, wrap))
        row.addElement(num_cell(c.get("year", 0), text=str(c.get("year", "")), style=wrap)
                       if c.get("year") else str_cell("", wrap))
        row.addElement(str_cell(c.get("venue", ""), wrap))
        row.addElement(num_cell(score, text=f"{score:.2f}", style=wrap))
        row.addElement(str_cell(relevance_band(score), wrap))
        row.addElement(num_cell(1 if score >= 0.99 else 0,
                                text="1" if score >= 0.99 else "0", style=wrap))
        row.addElement(str_cell(c.get("reason", ""), wrap))
        row.addElement(link_cell(c.get("link", "") or "", wrap))
        table.addElement(row)
    doc.spreadsheet.addElement(table)


def build_summary_sheet(doc, styles, candidates, kept, threshold):
    header, wrap, total = styles
    table = Table(name="Summary")
    add_columns(doc, table, SUMMARY_COLUMNS)
    table.addElement(header_row(SUMMARY_COLUMNS, header))

    years = sorted({c.get("year") for c in candidates if c.get("year")})
    kept_by_year = {}
    for c in kept:
        kept_by_year.setdefault(c.get("year"), []).append(float(c["score"]))
    fetched_by_year = {}
    for c in candidates:
        if c.get("year"):
            fetched_by_year[c["year"]] = fetched_by_year.get(c["year"], 0) + 1

    tot = {"fetched": 0, "matched": 0, "scores": [],
           **{b: 0 for b in BANDS}}

    def band_counts(scores):
        counts = {b: 0 for b in BANDS}
        for s in scores:
            counts[relevance_band(s)] += 1
        return counts

    def emit_row(label, fetched, scores, style=None):
        counts = band_counts(scores)
        matched = len(scores)
        avg = sum(scores) / matched if matched else 0.0
        share = matched / fetched if fetched else 0.0
        row = TableRow()
        row.addElement(str_cell(label, style) if isinstance(label, str)
                       else num_cell(label, text=str(label), style=style))
        row.addElement(num_cell(fetched, text=str(fetched), style=style))
        row.addElement(num_cell(matched, text=str(matched), style=style))
        for b in BANDS:
            row.addElement(num_cell(counts[b], text=str(counts[b]), style=style))
        row.addElement(num_cell(avg, text=f"{avg:.2f}" if matched else "-", style=style))
        row.addElement(num_cell(share, text=f"{share:.0%}" if fetched else "-", style=style))
        table.addElement(row)

    for y in years:
        scores = kept_by_year.get(y, [])
        fetched = fetched_by_year.get(y, 0)
        emit_row(y, fetched, scores, style=wrap)
        tot["fetched"] += fetched
        tot["matched"] += len(scores)
        tot["scores"].extend(scores)

    emit_row("TOTAL", tot["fetched"], tot["scores"], style=total)
    doc.spreadsheet.addElement(table)


def build_meta_sheet(doc, styles, meta, threshold, n_candidates, n_kept):
    header, wrap, _ = styles
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
        ("Score threshold", threshold),
        ("Papers fetched", n_candidates),
        ("Papers matched (kept)", n_kept),
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
                    help="Drop papers scoring below this (default 0.3).")
    args = ap.parse_args()

    with open(args.infile, encoding="utf-8") as fh:
        data = json.load(fh)
    meta = data.get("meta", {})
    candidates = data.get("candidates", [])
    topic = meta.get("keyword", "")

    kept, unscored = [], 0
    for c in candidates:
        s = c.get("score")
        if s is None:
            unscored += 1
            continue
        if float(s) >= args.threshold:
            kept.append(c)
    kept.sort(key=lambda c: (-float(c["score"]), c.get("year", 0)))

    if unscored:
        sys.stderr.write(
            f"WARNING: {unscored} candidates had no score and were skipped. "
            f"Did the agent finish scoring?\n")

    doc = OpenDocumentSpreadsheet()
    styles = make_styles(doc)
    build_papers_sheet(doc, styles, kept, topic)
    build_summary_sheet(doc, styles, candidates, kept, args.threshold)
    build_meta_sheet(doc, styles, meta, args.threshold, len(candidates), len(kept))
    doc.save(args.out)

    sys.stderr.write(
        f"Wrote {len(kept)} papers (threshold >= {args.threshold}) to {args.out} "
        f"across sheets Papers / Summary / Meta. "
        f"Dropped {len(candidates) - len(kept) - unscored} as too weakly related.\n")


if __name__ == "__main__":
    main()
