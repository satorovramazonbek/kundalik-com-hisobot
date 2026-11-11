"""Hisobotlarni chiroyli Excel faylga aylantirish."""
from __future__ import annotations

import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from scraper import ClassReport, Report

THIN = Side(style="thin", color="999999")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="D9E1F2")
FOOT_FILL = PatternFill("solid", fgColor="EDEDED")
MARK_FILLS = {5: "C6EFCE", 4: "DDEBF7", 3: "FFEB9C", 2: "FFC7CE"}


def _num(v: str):
    orig = (v or "").strip()
    n = orig.replace(" ", "").replace(",", ".")
    if re.fullmatch(r"-?\d+", n):
        return int(n)
    if re.fullmatch(r"-?\d+\.\d+", n):
        f = float(n)
        return int(f) if f.is_integer() else round(f, 2)
    return orig


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_")[:80]


def _mark_fill(val) -> PatternFill | None:
    if isinstance(val, (int, float)) and 1 <= val <= 5:
        return PatternFill("solid", fgColor=MARK_FILLS.get(int(round(val)), "FFFFFF"))
    return None


def _write_table(ws, title: str, subtitle: str, header: list[str], rows: list[list], footer: list[list],
                 mark_cols: set[int]) -> None:
    width = len(header)
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=14)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(width, 1))
    ws.cell(row=2, column=1, value=subtitle).font = Font(italic=True, color="555555")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(width, 1))

    r0 = 4
    for c, h in enumerate(header, 1):
        cell = ws.cell(row=r0, column=c, value=h)
        cell.font = Font(bold=True)
        cell.fill = HEAD_FILL
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for i, row in enumerate(rows, r0 + 1):
        for c, raw in enumerate(row, 1):
            val = _num(raw) if isinstance(raw, str) else raw
            cell = ws.cell(row=i, column=c, value=val if val != "" else None)
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="left" if c == 2 else "center", vertical="center")
            if c - 1 in mark_cols and (fill := _mark_fill(val)):
                cell.fill = fill

    fr = r0 + 1 + len(rows)
    for j, row in enumerate(footer):
        for c, raw in enumerate(row, 1):
            val = _num(raw) if isinstance(raw, str) else raw
            cell = ws.cell(row=fr + j, column=c, value=val if val != "" else None)
            cell.font = Font(bold=True)
            cell.fill = FOOT_FILL
            cell.border = BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.merge_cells(start_row=fr + j, start_column=1, end_row=fr + j, end_column=2)

    for c in range(1, width + 1):
        col_vals = [str(header[c - 1])] + [str(r[c - 1]) for r in rows if c - 1 < len(r)]
        longest = max(len(v) for v in col_vals)
        ws.column_dimensions[get_column_letter(c)].width = min(max(longest + 2, 6), 32 if c == 2 else 18)
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = max(ws.column_dimensions["B"].width, 22)
    ws.row_dimensions[r0].height = 45
    ws.freeze_panes = ws.cell(row=r0 + 1, column=3)


def _save(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_subject_excel(rep: ClassReport, idx: int) -> tuple[str, bytes]:
    """Bitta fan bo'yicha: №, FISH, chorak bahosi + fan statistikasi."""
    subject = rep.subjects[idx]
    rows = [[no, name, marks[idx] if idx < len(marks) else ""] for no, name, marks, _, _ in rep.students]
    footer = [[label, "", vals[idx] if idx < len(vals) else ""] for label, vals in rep.footer]

    graded = [v for v in (_num(r[2]) for r in rows) if isinstance(v, (int, float))]
    if graded:
        cnt = {k: sum(1 for v in graded if int(round(v)) == k) for k in (5, 4, 3, 2)}
        footer.append([f"Baholanganlar: {len(graded)} / {len(rows)}", "", ""])
        footer.append([f"«5»: {cnt[5]}   «4»: {cnt[4]}   «3»: {cnt[3]}   «2»: {cnt[2]}", "", ""])

    wb = Workbook()
    ws = wb.active
    ws.title = _safe(subject)[:31] or "Fan"
    _write_table(ws, f"{rep.class_name} sinf — {subject}", f"Hisobot davri: {rep.period}",
                 ["№", "F.I.Sh", f"{subject}\n(chorak bahosi)"], rows, footer, mark_cols={2})
    ws.column_dimensions["C"].width = 20
    return f"{_safe(rep.class_name)}_{_safe(subject)}_{_safe(rep.period)}.xlsx", _save(wb)


def build_class_excel(rep: ClassReport) -> tuple[str, bytes]:
    """Barcha fanlar (umumiy) jadvali."""
    header = ["№", "F.I.Sh", *rep.subjects, "O‘rtacha ball", "Reyting"]
    rows = [[no, name, *marks, avg, rating] for no, name, marks, avg, rating in rep.students]
    footer = [[label, "", *vals, "", ""] for label, vals in rep.footer]
    wb = Workbook()
    ws = wb.active
    ws.title = "Umumiy"
    _write_table(ws, f"{rep.class_name} sinf — umumiy hisobot", f"Hisobot davri: {rep.period}",
                 header, rows, footer, mark_cols=set(range(2, 2 + len(rep.subjects) + 1)))
    return f"{_safe(rep.class_name)}_umumiy_{_safe(rep.period)}.xlsx", _save(wb)


def build_excel(report: Report) -> tuple[str, bytes]:
    """Eski umumiy format (Report) — orqaga moslik uchun."""
    safe = _safe(report.title)
    if report.excel_bytes:
        ext = (report.excel_name or "x.xlsx").rsplit(".", 1)[-1]
        return f"{safe}.{ext}", report.excel_bytes
    wb = Workbook()
    ws = wb.active
    width = max(len(r) for r in report.rows)
    rows = [r + [""] * (width - len(r)) for r in report.rows]
    _write_table(ws, report.title, "", rows[0], rows[1:], [], mark_cols=set(range(2, width)))
    return f"{safe}.xlsx", _save(wb)
