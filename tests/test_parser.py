"""Body and attachment parser tests (design section 8.4)."""

from __future__ import annotations

import io
import shutil
import zipfile

import pytest

from bank_emails.parser import (
    PARSE_FAILED,
    PARSE_OK,
    PARSE_SKIPPED,
    html_to_text,
    parse_attachment,
    parse_body,
)


def _xlsx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Trades" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="worksheets/sheet1.xml"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>Symbol</t></si><si><t>Quantity</t></si><si><t>600887.SH</t></si>
</sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
    <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>500000</v></c></row>
  </sheetData>
</worksheet>""",
        )
    return output.getvalue()


def _docx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Trade Confirmation</w:t></w:r></w:p>
    <w:tbl><w:tr>
      <w:tc><w:p><w:r><w:t>Symbol</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>0386.HK</w:t></w:r></w:p></w:tc>
    </w:tr></w:tbl>
  </w:body>
</w:document>""",
        )
    return output.getvalue()


def _pdf_bytes() -> bytes:
    content = b"BT /F1 18 Tf 72 720 Td (Trade Confirmation) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def test_html_tables_become_tab_separated_rows() -> None:
    html = """
    <html><head><style>.x{}</style></head><body>
      <p>Trade summary</p>
      <table>
        <tr><th>Symbol</th><th>Quantity</th></tr>
        <tr><td>600887.SH</td><td>500,000</td></tr>
      </table>
    </body></html>
    """
    text = html_to_text(html)
    assert "Trade summary" in text
    assert "Symbol\tQuantity" in text
    assert "600887.SH\t500,000" in text
    assert ".x{}" not in text


def test_parse_body_handles_plain_and_html() -> None:
    assert parse_body("text", "  hello  ") == "hello"
    assert "hello" in parse_body("html", "<p>hello</p>")


def test_csv_supports_utf8_and_gb18030() -> None:
    utf8 = parse_attachment("trades.csv", "text/csv", "代码,数量\n600887.SH,500\n".encode())
    assert utf8.status == PARSE_OK
    assert "代码\t数量" in (utf8.text or "")

    excel_csv = parse_attachment(
        "trades.csv",
        "application/vnd.ms-excel",
        "symbol,quantity\n0386.HK,13582000\n".encode(),
    )
    assert excel_csv.status == PARSE_OK
    assert "0386.HK\t13582000" in (excel_csv.text or "")

    gb18030 = parse_attachment(
        "trades.csv",
        "text/csv",
        "证券代码,证券名称\n300308,中际旭创\n".encode("gb18030"),
    )
    assert gb18030.status == PARSE_OK
    assert "中际旭创" in (gb18030.text or "")


def test_xlsx_parser_reads_shared_strings_and_numbers() -> None:
    result = parse_attachment("trades.xlsx", "application/octet-stream", _xlsx_bytes())
    assert result.status == PARSE_OK
    assert "## Trades" in (result.text or "")
    assert "600887.SH\t500000" in (result.text or "")


def test_docx_parser_preserves_paragraphs_and_tables() -> None:
    result = parse_attachment("trade.docx", "application/octet-stream", _docx_bytes())
    assert result.status == PARSE_OK
    assert "Trade Confirmation" in (result.text or "")
    assert "Symbol\t0386.HK" in (result.text or "")


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext is not installed")
def test_pdf_parser_extracts_text_layer() -> None:
    result = parse_attachment("trade.pdf", "application/pdf", _pdf_bytes())
    assert result.status == PARSE_OK
    assert "Trade Confirmation" in (result.text or "")


def test_image_and_corrupt_pdf_are_not_silently_accepted() -> None:
    image = parse_attachment("scan.png", "image/png", b"\x89PNG")
    assert image.status == PARSE_SKIPPED
    assert image.error

    corrupt = parse_attachment("broken.pdf", "application/pdf", b"not a pdf")
    assert corrupt.status == PARSE_FAILED
    assert corrupt.error
