"""Get text and tables out of a decrypted statement PDF.

Three tiers, tried in order, because bank statements vary wildly:
  1. text   — pdfplumber layout text. Works for most digitally generated PDFs.
  2. tables — pdfplumber table detection, for statements whose transaction grid
              has ruling lines and whose flat text interleaves columns badly.
  3. ocr    — pytesseract over rendered pages, for scanned/image statements.

OCR needs system binaries (tesseract, poppler) and is skipped with a clear
message when they are absent, rather than crashing the whole sync.
"""

import io
import os

import pdfplumber

MIN_USEFUL_CHARS = 200


class ExtractionError(RuntimeError):
    pass


class Extraction:
    """Extracted content plus which tier produced it."""

    def __init__(self, method: str, lines: list, tables: list, page_count: int):
        self.method = method
        self.lines = lines
        self.tables = tables
        self.page_count = page_count

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _text_lines(pdf) -> list:
    lines = []
    for page in pdf.pages:
        raw = page.extract_text(x_tolerance=1.6, y_tolerance=3) or ""
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    return lines


def _tables(pdf) -> list:
    """Every table on every page, as lists of row-cell lists."""
    found = []
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            rows = [
                [(cell or "").replace("\n", " ").strip() for cell in row]
                for row in table
                if any((cell or "").strip() for cell in row)
            ]
            if rows:
                found.append(rows)
    return found


def _ocr_lines(data: bytes) -> list:
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except ImportError as exc:
        raise ExtractionError(
            "PDF has no extractable text and OCR is unavailable "
            f"({exc}). Install with: pip install pytesseract pdf2image"
        )
    try:
        images = convert_from_bytes(data, dpi=int(os.environ.get("CC_OCR_DPI", "300")))
    except Exception as exc:
        raise ExtractionError(
            f"OCR could not rasterise the PDF ({exc}). Poppler is required: "
            "brew install poppler"
        )
    lines = []
    for image in images:
        try:
            raw = pytesseract.image_to_string(image)
        except Exception as exc:
            raise ExtractionError(
                f"OCR failed ({exc}). Tesseract is required: brew install tesseract"
            )
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    return lines


def extract(data: bytes, allow_ocr: bool = True) -> Extraction:
    """Run the extraction ladder and report which rung succeeded."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_count = len(pdf.pages)
            lines = _text_lines(pdf)
            tables = _tables(pdf)
    except Exception as exc:
        raise ExtractionError(f"Could not read the decrypted PDF: {exc}")

    if len("".join(lines)) >= MIN_USEFUL_CHARS:
        return Extraction("text", lines, tables, page_count)

    if tables:
        flat = [" ".join(cell for cell in row if cell) for table in tables for row in table]
        return Extraction("tables", flat, tables, page_count)

    if not allow_ocr:
        raise ExtractionError(
            "PDF yielded no usable text and OCR was disabled for this run."
        )
    return Extraction("ocr", _ocr_lines(data), [], page_count)
