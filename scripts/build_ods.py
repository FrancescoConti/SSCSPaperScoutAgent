#!/usr/bin/env python3
"""
Turn a scored candidates JSON file into an ODS spreadsheet.

Reads the JSON produced by fetch_candidates.py *after the agent has filled in
the `score` and `reason` fields*. Any candidate with score == null or score
below --threshold is dropped (i.e. "poorly related or unrelated papers are
removed"). Remaining rows are sorted best-first.

Columns: Paper ID | Title | Authors | Link | Score | Reason | Year | Venue

Usage:
    python3 build_ods.py --in candidates.json --out results.ods --threshold 0.3
"""
import argparse
import json
import sys

from odf.opendocument import OpenDocumentSpreadsheet
from odf.style import (Style, TableColumnProperties, TableCellProperties,
                       ParagraphProperties, TextProperties)
from odf.table import Table, TableColumn, TableRow, TableCell
from odf.text import P, A

COLUMNS = [
    ("Paper ID", "3.5cm"),
    ("Title", "9cm"),
    ("Authors", "6cm"),
    ("Link", "6cm"),
    ("Score", "1.6cm"),
    ("Reason", "9cm"),
    ("Year", "1.4cm"),
    ("Venue", "5cm"),
]


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

    kept = []
    unscored = 0
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

    header_style = Style(name="Header", family="table-cell")
    header_style.addElement(TableCellProperties(backgroundcolor="#d9e1f2"))
    header_style.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(header_style)

    wrap_style = Style(name="Wrap", family="table-cell")
    wrap_style.addElement(TableCellProperties(wrapoption="wrap",
                                              verticalalign="top"))
    doc.automaticstyles.addElement(wrap_style)

    sheet_name = f"{meta.get('venue', 'papers')}_{meta.get('keyword', '')}"[:31]
    table = Table(name=sheet_name or "papers")

    for _, width in COLUMNS:
        col_style = Style(name=f"co{width}", family="table-column")
        col_style.addElement(TableColumnProperties(columnwidth=width))
        doc.automaticstyles.addElement(col_style)
        table.addElement(TableColumn(stylename=col_style))

    # Header row
    hrow = TableRow()
    for name, _ in COLUMNS:
        cell = TableCell(stylename=header_style, valuetype="string")
        cell.addElement(P(text=name))
        hrow.addElement(cell)
    table.addElement(hrow)

    for c in kept:
        row = TableRow()

        def text_cell(value):
            cell = TableCell(stylename=wrap_style, valuetype="string")
            cell.addElement(P(text=str(value) if value is not None else ""))
            return cell

        row.addElement(text_cell(c.get("paper_id", "")))
        row.addElement(text_cell(c.get("title", "")))
        authors = c.get("authors", [])
        row.addElement(text_cell(", ".join(authors) if isinstance(authors, list) else authors))

        # Link as a real clickable hyperlink
        link = c.get("link", "") or ""
        link_cell = TableCell(stylename=wrap_style, valuetype="string")
        para = P()
        if link:
            para.addElement(A(href=link, text=link))
        link_cell.addElement(para)
        row.addElement(link_cell)

        score_cell = TableCell(valuetype="float", value=float(c["score"]))
        score_cell.addElement(P(text=f"{float(c['score']):.2f}"))
        row.addElement(score_cell)

        row.addElement(text_cell(c.get("reason", "")))
        row.addElement(text_cell(c.get("year", "")))
        row.addElement(text_cell(c.get("venue", "")))
        table.addElement(row)

    doc.spreadsheet.addElement(table)
    doc.save(args.out)

    sys.stderr.write(
        f"Wrote {len(kept)} papers (threshold >= {args.threshold}) to {args.out}. "
        f"Dropped {len(candidates) - len(kept) - unscored} as too weakly related.\n")


if __name__ == "__main__":
    main()
