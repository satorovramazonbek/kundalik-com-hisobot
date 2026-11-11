"""Hisobot jadvalini chiroyli Excel faylga aylantirish."""
from __future__ import annotations

import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from scraper import Report

THIN = Side(style="thin", color="999999")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="D9E1F2")
MARK_FILLS = {"5": "C6EFCE", "4": "DDEBF7", "3": "FFEB9C", "2": "FFC7CE"}


def _num(v: str):
    v = v.strip().replace(",", ".")
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def build_excel(report: Report) -> tuple[str, bytes]:
    safe = re.sub(r"[^\w\-]+", "_", report.title).strip("_")
    if report.excel_bytes:
        ext = (report.excel_name or "x.xlsx").rsplit(".", 1)[-1]
        return f"{safe}.{ext}", report.excel_bytes

    wb = Workbook()
    ws = wb.active
    ws.title = "Hisobot"
    width = max(len(r) for r in report.rows)

    ws.cell(row=1, column=1, value=report.title).font = Font(bold=True, size=14)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(width, 1))

    for r_idx, row in enumerate(report.rows, start=3):
        for c_idx in range(width):
            raw = row[c_idx] if c_idx < len(row) else ""
            cell = ws.cell(row=r_idx, column=c_idx + 1, value=_num(raw))
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center" if c_idx else "left", vertical="center", wrap_text=True)
            if r_idx == 3:
                cell.font = Font(bold=True)
                cell.fill = HEAD_FILL
            elif str(raw).strip() in MARK_FILLS and c_idx > 0:
                cell.fill = PatternFill("solid", fgColor=MARK_FILLS[str(raw).strip()])

    for c in range(1, width + 1):
        longest = max((len(str(r[c - 1])) for r in report.rows if c - 1 < len(r)), default=5)
        ws.column_dimensions[get_column_letter(c)].width = min(max(longest + 2, 6), 40)
    ws.freeze_panes = ws.cell(row=4, column=2)

    buf = io.BytesIO()
    wb.save(buf)
    return f"{safe}.xlsx", buf.getvalue()
