"""A minimal .xlsx writer — enough for a report, with no dependency.

An .xlsx file is a zip of XML parts. For tables of strings and numbers
that is a few hundred bytes of boilerplate plus one ``<row>`` per line,
which is not worth a 300-module dependency (openpyxl) that would also
be the platform's only spreadsheet consumer. What this does *not* do,
on purpose: styles, formulas, dates as dates, merged cells. A number is
written as a number so it sums; everything else is text.

Text is written as inline strings and XML-escaped, and a value that
Excel would read as a formula (``=``, ``+``, ``-``, ``@`` first) is
prefixed the way the CSV export does — the same protection, because it
is the same threat: a lead "named" ``=cmd|…`` must never execute on the
laptop of whoever opens the export.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

Cell = str | int | float | None
Sheet = tuple[str, list[list[Cell]]]     # (name, rows) — first row is the header

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    "{sheets}"
    "</Types>"
)
_SHEET_TYPE = (
    '<Override PartName="/xl/worksheets/sheet{n}.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    "<sheets>{sheets}</sheets></workbook>"
)
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    "{rels}</Relationships>"
)
_SHEET_REL = (
    '<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet{n}.xml"/>'
)
_WORKSHEET = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<sheetData>{rows}</sheetData></worksheet>"
)

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _column(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell(ref: str, value: Cell) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = str(value)
    if text[:1] in _FORMULA_STARTS:
        text = f"'{text}"
    # Control characters are not legal in XML 1.0; drop them rather than
    # produce a file Excel refuses to open.
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def _sheet_name(name: str) -> str:
    """Excel: at most 31 characters, none of []:*?/\\."""
    cleaned = "".join(ch for ch in name if ch not in '[]:*?/\\').strip() or "Sheet"
    return cleaned[:31]


def workbook(sheets: list[Sheet]) -> bytes:
    """Build an .xlsx from (name, rows) sheets and return its bytes."""
    if not sheets:
        raise ValueError("a workbook needs at least one sheet")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            _CONTENT_TYPES.format(sheets="".join(_SHEET_TYPE.format(n=i + 1) for i in range(len(sheets)))),
        )
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr(
            "xl/workbook.xml",
            _WORKBOOK.format(sheets="".join(
                f'<sheet name="{escape(_sheet_name(name), {chr(34): "&quot;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                for i, (name, _rows) in enumerate(sheets)
            )),
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            _WORKBOOK_RELS.format(rels="".join(_SHEET_REL.format(n=i + 1) for i in range(len(sheets)))),
        )
        for i, (_name, rows) in enumerate(sheets):
            xml_rows = "".join(
                f'<row r="{r + 1}">' + "".join(
                    _cell(f"{_column(c)}{r + 1}", value) for c, value in enumerate(row)
                ) + "</row>"
                for r, row in enumerate(rows)
            )
            zf.writestr(f"xl/worksheets/sheet{i + 1}.xml", _WORKSHEET.format(rows=xml_rows))
    return buffer.getvalue()
