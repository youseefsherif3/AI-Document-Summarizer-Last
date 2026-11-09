from io import BytesIO

import pytest
from docx import Document
from docx.enum.text import WD_BREAK
from fastapi.testclient import TestClient
from pypdf import PdfReader

import app.main as main
from app.extraction import Page, extract_pages, extract_text
from tests.pdf_builder import build_text_pdf


def docx_bytes(document) -> bytes:
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def numbers_and_text(pages):
    return [(page.number, page.text) for page in pages]


# --- PDF -------------------------------------------------------------------


def test_pdf_pages_are_numbered_from_one():
    pdf = build_text_pdf(["Page one: alpha", "Page two: beta", "Page three: gamma"])

    pages = extract_pages(pdf, ".pdf")

    assert [page.number for page in pages] == [1, 2, 3]
    assert "alpha" in pages[0].text
    assert "beta" in pages[1].text
    assert "gamma" in pages[2].text


def test_blank_pdf_page_keeps_numbering():
    pdf = build_text_pdf(["First page", "", "Third page"])

    pages = extract_pages(pdf, ".pdf")

    assert [page.number for page in pages] == [1, 2, 3]
    assert pages[1].text.strip() == ""
    assert "Third page" in pages[2].text


def test_pdf_text_is_exactly_what_the_summary_used_before():
    pdf = build_text_pdf(["Line A\nLine B", "Line C (with parens)", "", "Last line"])
    # The pre-step-4 implementation, kept here as the reference.
    before = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)

    assert extract_text(pdf, ".pdf") == before


# --- TXT -------------------------------------------------------------------


def test_txt_form_feeds_split_pages():
    content = "Chapter one\fChapter two\fChapter three".encode()

    pages = extract_pages(content, ".txt")

    assert numbers_and_text(pages) == [
        (1, "Chapter one"),
        (2, "Chapter two"),
        (3, "Chapter three"),
    ]


def test_txt_without_form_feeds_is_one_unnumbered_page():
    assert extract_pages(b"\xef\xbb\xbfJust text", ".txt") == [Page(number=None, text="Just text")]


@pytest.mark.parametrize(
    "content",
    [
        b"Plain text.",
        b"\xef\xbb\xbfWith a BOM",
        b"Windows\r\nline endings\r\n",
        b"Pages\fwith\fform feeds\f",
        "نص عربي\fصفحة ثانية".encode(),
    ],
    ids=["plain", "bom", "crlf", "form-feeds", "arabic"],
)
def test_txt_text_is_exactly_what_the_summary_used_before(content):
    assert extract_text(content, ".txt") == content.decode("utf-8-sig")


# --- DOCX ------------------------------------------------------------------


def test_docx_without_page_breaks_is_one_unnumbered_page():
    document = Document()
    document.add_paragraph("Intro paragraph.")
    document.add_paragraph("")
    document.add_paragraph("Second paragraph.")
    content = docx_bytes(document)

    pages = extract_pages(content, ".docx")

    assert pages == [Page(number=None, text="Intro paragraph.\n\nSecond paragraph.")]
    # Without tables, the summary text is unchanged from before.
    before = "\n".join(p.text for p in Document(BytesIO(content)).paragraphs)
    assert extract_text(content, ".docx") == before


def test_docx_tables_are_included_in_document_order():
    document = Document()
    document.add_paragraph("Before the table.")
    table = document.add_table(rows=3, cols=3)
    for row, values in zip(table.rows, [["Item", "Qty", "Price"], ["Pens", "4", "2.50"], ["", "", ""]]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    document.add_paragraph("After the table.")

    [page] = extract_pages(docx_bytes(document), ".docx")

    assert page.text.split("\n") == [
        "Before the table.",
        "Item | Qty | Price",
        "Pens | 4 | 2.50",
        "After the table.",
    ]


def test_docx_merged_table_cells_appear_once():
    document = Document()
    table = document.add_table(rows=1, cols=3)
    merged = table.cell(0, 0).merge(table.cell(0, 1))
    merged.text = "Merged"
    table.cell(0, 2).text = "Single"

    [page] = extract_pages(docx_bytes(document), ".docx")

    assert page.text == "Merged | Single"


def test_docx_explicit_page_breaks_number_the_pages():
    document = Document()
    document.add_paragraph("Page one text.")
    document.add_page_break()
    document.add_paragraph("Page two text.")

    pages = extract_pages(docx_bytes(document), ".docx")

    assert numbers_and_text(pages) == [(1, "Page one text."), (2, "Page two text.")]


def test_docx_page_break_inside_a_paragraph_splits_it():
    document = Document()
    paragraph = document.add_paragraph("Before the break.")
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    paragraph.add_run("After the break.")

    pages = extract_pages(docx_bytes(document), ".docx")

    assert numbers_and_text(pages) == [(1, "Before the break."), (2, "After the break.")]


def test_docx_page_break_before_starts_a_new_page():
    document = Document()
    document.add_paragraph("Chapter one.")
    document.add_paragraph("Chapter two.").paragraph_format.page_break_before = True

    pages = extract_pages(docx_bytes(document), ".docx")

    assert numbers_and_text(pages) == [(1, "Chapter one."), (2, "Chapter two.")]


# --- Other -----------------------------------------------------------------


def test_unsupported_extension_has_no_pages():
    assert extract_pages(b"Some content", ".jpg") == []


def test_upload_runs_extraction_and_saving_in_a_threadpool(monkeypatch):
    calls = []
    real_run_in_threadpool = main.run_in_threadpool

    async def spy(func, *args, **kwargs):
        calls.append(func)
        return await real_run_in_threadpool(func, *args, **kwargs)

    async def mock_stream(text):
        yield "A summary."

    monkeypatch.setattr(main, "run_in_threadpool", spy)
    monkeypatch.setattr(main, "stream_summary", mock_stream)

    with TestClient(main.app) as client:
        response = client.post(
            "/documents/upload/stream",
            files={"file": ("doc.txt", b"Some text.", "text/plain")},
        )

    assert '"type": "done"' in response.text
    # Extraction + chunking, then the database write: both off the event loop.
    assert calls == [main._read_document, main.save_document_with_chunks]
