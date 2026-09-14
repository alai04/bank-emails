"""Email body and attachment text extraction."""

from __future__ import annotations

import csv
import io
import posixpath
import re
import subprocess
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

PARSE_OK = "OK"
PARSE_SKIPPED = "SKIPPED"
PARSE_FAILED = "FAILED"

MAX_TABLE_ROWS = 20_000
XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass(frozen=True)
class AttachmentParseResult:
    status: str
    text: str | None
    error: str | None = None


class _HTMLTextExtractor(HTMLParser):
    """Small HTML-to-text converter with table row preservation."""

    _BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "li", "br"}
    _SUPPRESSED_TAGS = {"script", "style", "noscript", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._suppressed = 0
        self._text: list[str] = []
        self._table_depth = 0
        self._row: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._SUPPRESSED_TAGS:
            self._suppressed += 1
            return
        if self._suppressed:
            return
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth:
            self._row = []
        elif tag in {"th", "td"} and self._table_depth:
            self._cell = []
        elif tag in self._BLOCK_TAGS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SUPPRESSED_TAGS:
            self._suppressed = max(0, self._suppressed - 1)
            return
        if self._suppressed:
            return
        if tag in {"th", "td"} and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._table_depth:
            if any(self._row):
                self._append("\n" + "\t".join(self._row) + "\n")
            self._row = []
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            self._append("\n")
        elif tag in self._BLOCK_TAGS:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self._append(data)

    def _append(self, value: str) -> None:
        if self._cell is not None:
            self._cell.append(value)
        else:
            self._text.append(value)

    def text(self) -> str:
        lines = [
            "\t".join(" ".join(cell.split()) for cell in line.split("\t"))
            for line in "".join(self._text).splitlines()
        ]
        compact: list[str] = []
        for line in lines:
            if line or (compact and compact[-1]):
                compact.append(line)
        return "\n".join(compact).strip()


def html_to_text(html: str) -> str:
    """Convert HTML to readable text while preserving table rows and columns."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


def parse_body(content_type: str, content: str) -> str:
    if content_type.casefold() == "html":
        return html_to_text(content)
    return content.strip()


def parse_attachment(
    filename: str,
    content_type: str,
    content: bytes,
) -> AttachmentParseResult:
    suffix = Path(filename).suffix.casefold()
    media_type = content_type.split(";", 1)[0].strip().casefold()
    try:
        if media_type == "application/pdf" or suffix == ".pdf":
            return AttachmentParseResult(PARSE_OK, _parse_pdf(content))
        if suffix == ".xls":
            return AttachmentParseResult(PARSE_SKIPPED, None, "legacy .xls is not supported")
        if suffix == ".xlsx" or media_type == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ):
            return AttachmentParseResult(PARSE_OK, _parse_xlsx(content))
        if suffix == ".csv" or media_type in {
            "text/csv",
            "application/csv",
            "application/vnd.ms-excel",
        }:
            return AttachmentParseResult(PARSE_OK, _parse_csv(content))
        if suffix == ".docx" or media_type == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ):
            return AttachmentParseResult(PARSE_OK, _parse_docx(content))
        if media_type.startswith("text/") or suffix in {".txt", ".md"}:
            return AttachmentParseResult(PARSE_OK, _decode_text(content).strip())
        if media_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            return AttachmentParseResult(
                PARSE_SKIPPED,
                None,
                "image requires multimodal extraction",
            )
        return AttachmentParseResult(
            PARSE_SKIPPED,
            None,
            f"unsupported attachment type: {content_type or suffix or 'unknown'}",
        )
    except Exception as exc:
        return AttachmentParseResult(PARSE_FAILED, None, f"{type(exc).__name__}: {exc}")


def _parse_pdf(content: bytes) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=content,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pdftotext executable is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pdftotext timed out after 60 seconds") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"pdftotext exited with {result.returncode}")
    pages = result.stdout.decode("utf-8", errors="replace").split("\f")
    text = "\n\n".join(
        f"--- page {index} ---\n{page.strip()}"
        for index, page in enumerate(pages, start=1)
        if page.strip()
    )
    if not text:
        raise RuntimeError("PDF contains no extractable text layer")
    return text


def _parse_xlsx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall(f"{{{XLSX_NS}}}si"):
                shared_strings.append(
                    "".join(node.text or "" for node in item.iter(f"{{{XLSX_NS}}}t"))
                )

        workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships_root = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        relationships = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships_root.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        }

        sections: list[str] = []
        for sheet in workbook_root.findall(f".//{{{XLSX_NS}}}sheet"):
            title = sheet.attrib.get("name", "Sheet")
            relation_id = sheet.attrib.get(f"{{{XLSX_REL_NS}}}id", "")
            target = relationships.get(relation_id, "")
            path = (
                posixpath.normpath(target.lstrip("/"))
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target))
            )
            if not target or path not in archive.namelist():
                sections.append(f"## {title}\n[worksheet data not found]")
                continue
            root = ElementTree.fromstring(archive.read(path))
            lines = [f"## {title}"]
            for row_index, row in enumerate(root.iter(f"{{{XLSX_NS}}}row"), start=1):
                if row_index > MAX_TABLE_ROWS:
                    lines.append(f"[truncated after {MAX_TABLE_ROWS} rows]")
                    break
                cells: list[str] = []
                for fallback_index, cell in enumerate(row.findall(f"{{{XLSX_NS}}}c"), start=1):
                    reference = cell.attrib.get("r", "")
                    column_index = _xlsx_column_index(reference) or fallback_index
                    while len(cells) < column_index - 1:
                        cells.append("")
                    cells.append(_xlsx_cell_text(cell, shared_strings).strip())
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    lines.append("\t".join(cells))
            sections.append("\n".join(lines))
    return "\n\n".join(sections).strip()


def _xlsx_column_index(reference: str) -> int:
    match = re.match(r"([A-Za-z]+)", reference)
    if not match:
        return 0
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def _xlsx_cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{XLSX_NS}}}t"))
    value = cell.find(f"{{{XLSX_NS}}}v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        try:
            return shared_strings[int(value.text)]
        except (ValueError, IndexError):
            return ""
    return value.text


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_csv(content: bytes) -> str:
    text = _decode_text(content)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(io.StringIO(text), dialect)
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        if index > MAX_TABLE_ROWS:
            lines.append(f"[truncated after {MAX_TABLE_ROWS} rows]")
            break
        lines.append("\t".join(cell.strip() for cell in row))
    return "\n".join(lines)


def _parse_docx(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{{{DOCX_NS}}}body")
    if body is None:
        raise ValueError("DOCX document body is missing")

    sections: list[str] = []
    table_rows = 0
    for child in body:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = _docx_paragraph_text(child)
            if text:
                sections.append(text)
        elif tag == "tbl":
            rows: list[str] = []
            for row in child.findall(f"{{{DOCX_NS}}}tr"):
                if table_rows >= MAX_TABLE_ROWS:
                    rows.append(f"[truncated after {MAX_TABLE_ROWS} rows]")
                    break
                table_rows += 1
                cells = [
                    " ".join(
                        _docx_paragraph_text(paragraph)
                        for paragraph in cell.findall(f"{{{DOCX_NS}}}p")
                    ).strip()
                    for cell in row.findall(f"{{{DOCX_NS}}}tc")
                ]
                rows.append("\t".join(cells))
            if rows:
                sections.append("\n".join(rows))
    return "\n\n".join(sections).strip()


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    return " ".join(
        (node.text or "").strip()
        for node in paragraph.iter(f"{{{DOCX_NS}}}t")
        if (node.text or "").strip()
    )
