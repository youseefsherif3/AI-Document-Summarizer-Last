"""Generate the eval corpus files from corpus_documents.py.

Run from backend/:  python -m evals.build_corpus   (writes evals/corpus/, which is gitignored)

Output is deterministic (fixed dates and zip timestamps), so running it again gives
byte-identical files. Needs the dev dependencies (fpdf2 and uharfbuzz for Arabic PDFs).
"""

import hashlib
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .corpus_documents import DOCUMENTS, CorpusDocument

EVALS_DIR = Path(__file__).resolve().parent
CORPUS_DIR = EVALS_DIR / "corpus"
FONT_PATH = EVALS_DIR / "fonts" / "NotoNaskhArabic-Regular.ttf"
FIXED_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
AUTHOR = "Eval corpus generator"


def build_pdf(document: CorpusDocument) -> bytes:
    arabic = document.language == "ar"
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_author(AUTHOR)
    pdf.set_title(document.title)
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)

    if arabic:
        # Real shaping (joined letters, right-to-left), as in real Arabic PDFs.
        pdf.add_font("NotoNaskh", "", str(FONT_PATH))
        pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="arab", language="ara")
        family, bold, body_size, line_height, align = "NotoNaskh", "", 13, 9, "R"
    else:
        family, bold, body_size, line_height, align = "Helvetica", "B", 11, 6, "L"

    def write(text: str, size: int, style: str = "", gap: float = 3):
        pdf.set_font(family, style, size)
        pdf.multi_cell(0, line_height, text, align=align, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(gap)

    for index, page in enumerate(document.pages):
        pdf.add_page()
        if index == 0:
            write(document.title, 18, bold, gap=6)
        for block in page:
            if isinstance(block, str):
                write(block, body_size)
            elif block[0] == "h":
                write(block[1], 14, bold, gap=2)
            else:
                raise ValueError(f"{document.filename}: tables are only supported in DOCX.")

    if pdf.pages_count != len(document.pages):
        raise RuntimeError(
            f"{document.filename}: expected {len(document.pages)} pages, got "
            f"{pdf.pages_count}. Shorten the text so the page numbers stay as planned."
        )
    return bytes(pdf.output())


def _fixed_zip(data: bytes) -> bytes:
    """Rewrite a zip (DOCX) with fixed timestamps so the bytes are reproducible."""
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=(2026, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_DEFLATED
            fixed.external_attr = info.external_attr
            target.writestr(fixed, source.read(info.filename))
    return output.getvalue()


def build_docx(document: CorpusDocument) -> bytes:
    docx = Document()
    properties = docx.core_properties
    properties.author = AUTHOR
    properties.last_modified_by = AUTHOR
    properties.title = document.title
    properties.created = FIXED_DATE.replace(tzinfo=None)
    properties.modified = FIXED_DATE.replace(tzinfo=None)
    properties.revision = 1

    docx.add_heading(document.title, level=1)
    for index, page in enumerate(document.pages):
        if index > 0 and document.numbered_pages:
            docx.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        for block in page:
            if isinstance(block, str):
                docx.add_paragraph(block)
            elif block[0] == "h":
                docx.add_heading(block[1], level=2)
            elif block[0] == "table":
                rows = block[1]
                table = docx.add_table(rows=len(rows), cols=len(rows[0]))
                table.style = "Table Grid"
                for row, values in zip(table.rows, rows):
                    for cell, value in zip(row.cells, values):
                        cell.text = value

    buffer = io.BytesIO()
    docx.save(buffer)
    return _fixed_zip(buffer.getvalue())


def build_txt(document: CorpusDocument) -> bytes:
    pages = ["\n\n".join(block if isinstance(block, str) else block[1] for block in page)
             for page in document.pages]
    pages[0] = f"{document.title}\n\n{pages[0]}"
    separator = "\n\f" if document.numbered_pages else "\n\n"
    return (separator.join(pages) + "\n").encode("utf-8")


BUILDERS = {".pdf": build_pdf, ".docx": build_docx, ".txt": build_txt}


def build_document(document: CorpusDocument) -> bytes:
    extension = Path(document.filename).suffix
    return BUILDERS[extension](document)


def build_corpus(output_dir: Path) -> dict[str, bytes]:
    """Write every corpus file into `output_dir` (removing stale files); return name -> bytes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    expected = {document.filename for document in DOCUMENTS}
    for stale in output_dir.iterdir():
        if stale.is_file() and stale.name not in expected:
            stale.unlink()

    built = {}
    for document in DOCUMENTS:
        data = build_document(document)
        (output_dir / document.filename).write_bytes(data)
        built[document.filename] = data
    return built


def main() -> None:
    built = build_corpus(CORPUS_DIR)
    print(f"Wrote {len(built)} files to {CORPUS_DIR}")
    print(f"{'file':34} {'pages':>5} {'bytes':>8}  sha256")
    for document in DOCUMENTS:
        data = built[document.filename]
        pages = len(document.pages) if document.numbered_pages else "-"
        digest = hashlib.sha256(data).hexdigest()[:12]
        print(f"{document.filename:34} {pages:>5} {len(data):>8}  {digest}")


if __name__ == "__main__":
    main()
