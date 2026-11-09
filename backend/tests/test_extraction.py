from io import BytesIO

from docx import Document
from pypdf import PdfReader, PdfWriter

from app.main import extract_text


def test_extract_text_from_txt():
    content = b"Hello, this is a test document."

    result = extract_text(content, ".txt")

    assert result == "Hello, this is a test document."


def test_extract_text_from_docx():
    document = Document()
    document.add_paragraph("This is a DOCX test document.")

    buffer = BytesIO()
    document.save(buffer)

    result = extract_text(buffer.getvalue(), ".docx")

    assert result == "This is a DOCX test document."


def test_extract_text_from_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)

    buffer = BytesIO()
    writer.write(buffer)

    result = extract_text(buffer.getvalue(), ".pdf")

    assert result == ""
    
def test_extract_text_from_txt_with_bom():
    content = b"\xef\xbb\xbfHello with BOM"

    result = extract_text(content, ".txt")

    assert result == "Hello with BOM"


def test_extract_text_with_unsupported_extension():
    content = b"Some content"

    result = extract_text(content, ".jpg")

    assert result == ""