"""Text extraction, page by page, so RAG citations can name a page."""

from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

# TXT files mark page boundaries with a form feed, if they mark them at all.
TXT_PAGE_SEPARATOR = "\f"


@dataclass(frozen=True)
class Page:
    # 1-based page number, or None when the file has no reliable pages
    # (a TXT without form feeds, a DOCX without explicit page breaks).
    number: int | None
    text: str


def extract_pages(file_content: bytes, file_extension: str) -> list[Page]:
    if file_extension == ".pdf":
        return _pdf_pages(file_content)
    if file_extension == ".docx":
        return _docx_pages(file_content)
    if file_extension == ".txt":
        return _txt_pages(file_content)
    return []


def extract_text(file_content: bytes, file_extension: str) -> str:
    """The whole document as one string, used for the summary.

    PDF and TXT give exactly the same text as before per-page extraction existed.
    """
    return join_pages(extract_pages(file_content, file_extension), file_extension)


def join_pages(pages: list[Page], file_extension: str) -> str:
    """The pages joined back into the whole-document text that extract_text returns."""
    separator = TXT_PAGE_SEPARATOR if file_extension == ".txt" else "\n"
    return separator.join(page.text for page in pages)


def _pdf_pages(file_content: bytes) -> list[Page]:
    reader = PdfReader(BytesIO(file_content))
    return [
        Page(number=index, text=page.extract_text() or "")
        for index, page in enumerate(reader.pages, start=1)
    ]


def _txt_pages(file_content: bytes) -> list[Page]:
    parts = file_content.decode("utf-8-sig").split(TXT_PAGE_SEPARATOR)
    if len(parts) == 1:
        return [Page(number=None, text=parts[0])]
    return [Page(number=index, text=part) for index, part in enumerate(parts, start=1)]


def _is_page_break(element) -> bool:
    return element.tag == qn("w:br") and element.get(qn("w:type")) == "page"


def _split_on_page_breaks(paragraph: Paragraph) -> list[str]:
    """The paragraph's text, split wherever it contains an explicit page break."""
    segments = [""]
    for item in paragraph.iter_inner_content():
        if not hasattr(item, "_r"):  # a hyperlink: no page breaks inside
            segments[-1] += item.text
            continue
        for element in item._r:
            if _is_page_break(element):
                segments.append("")
            elif element.tag == qn("w:t"):
                segments[-1] += element.text or ""
            elif element.tag == qn("w:tab"):
                segments[-1] += "\t"
            elif element.tag in (qn("w:br"), qn("w:cr")):
                segments[-1] += "\n"
    return segments


def _table_lines(table: Table) -> list[str]:
    lines = []
    for row in table.rows:
        cells = []
        seen = set()
        for cell in row.cells:
            # Merged cells are returned once per grid column; keep each cell once.
            if id(cell._tc) in seen:
                continue
            seen.add(id(cell._tc))
            cells.append(" ".join(cell.text.split()))
        if any(cells):
            lines.append(" | ".join(cells))
    return lines


def _docx_pages(file_content: bytes) -> list[Page]:
    document = Document(BytesIO(file_content))
    pages: list[list[str]] = [[]]
    has_page_breaks = False

    for block in document.iter_inner_content():
        if isinstance(block, Table):
            pages[-1].extend(_table_lines(block))
            continue

        if block.paragraph_format.page_break_before and pages[-1]:
            pages.append([])
            has_page_breaks = True

        if not any(_is_page_break(e) for e in block._p.iter(qn("w:br"))):
            pages[-1].append(block.text)
            continue

        has_page_breaks = True
        segments = _split_on_page_breaks(block)
        for index, segment in enumerate(segments):
            if index > 0:
                pages.append([])
            if segment:
                pages[-1].append(segment)

    if not has_page_breaks:
        return [Page(number=None, text="\n".join(pages[0]))]
    return [
        Page(number=index, text="\n".join(lines))
        for index, lines in enumerate(pages, start=1)
    ]
