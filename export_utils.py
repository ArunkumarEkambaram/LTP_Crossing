"""
export_utils.py — builds downloadable CSV/XLSX bytes from a matches table.
Each row already carries its own "comment" field (edited inline in the
table itself), so this module just serializes whatever rows/columns it's
given — no special comment handling needed here.
"""
import csv
import io

from openpyxl import Workbook


def build_csv(rows: list, columns: list) -> bytes:
    """columns: ordered list of (field_key, header_label) tuples."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in columns])
    for r in rows:
        writer.writerow([r.get(key, "") for key, _ in columns])
    return buf.getvalue().encode("utf-8")


def build_xlsx(rows: list, columns: list) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Matches"
    ws.append([label for _, label in columns])
    for r in rows:
        ws.append([r.get(key, "") for key, _ in columns])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
