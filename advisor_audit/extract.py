"""Text extraction from uploads, entirely local and in memory.

PDF: pdfplumber text plus tables (OCR fallback for scanned pages).
CSV: decoded as-is. XLSX: every sheet dumped as CSV. Images: Tesseract.
"""

from __future__ import annotations

import io
from pathlib import Path

PDF_EXT = {".pdf"}
CSV_EXT = {".csv", ".txt"}
XLSX_EXT = {".xlsx", ".xls"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".heic", ".bmp"}


class UnsupportedFile(ValueError):
    pass


def kind_of(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in PDF_EXT or data[:5] == b"%PDF-":
        return "pdf"
    if ext in XLSX_EXT or data[:4] == b"PK\x03\x04":
        return "xlsx"
    if ext in IMAGE_EXT or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return "image"
    if ext in CSV_EXT:
        return "csv"
    raise UnsupportedFile(f"Unsupported file type: {ext or 'unknown'}. Use PDF, CSV, XLSX, or a photo.")


def _ocr(image) -> str:
    import pytesseract
    from PIL import ImageOps

    img = ImageOps.exif_transpose(image).convert("L")
    if img.width < 1800:  # Tesseract does better on larger text
        scale = 1800 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    img = ImageOps.autocontrast(img)
    return pytesseract.image_to_string(img, config="--psm 6")


def _pdf(data: bytes) -> str:
    import pdfplumber

    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for n, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            tables = page.extract_tables()
            if not text.strip() and not tables:
                text = _ocr(page.to_image(resolution=300).original)
            parts.append(f"--- page {n} ---\n{text}")
            for t, table in enumerate(tables, 1):
                rows = [" | ".join((c or "").replace("\n", " ").strip() for c in row) for row in table]
                parts.append(f"[table {n}.{t}]\n" + "\n".join(rows))
    return "\n\n".join(parts)


def _xlsx(data: bytes) -> str:
    import pandas as pd

    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    parts = []
    for name, df in sheets.items():
        parts.append(f"--- sheet {name} ---\n" + df.fillna("").to_csv(index=False, header=False))
    return "\n".join(parts)


def _image(data: bytes) -> str:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return _ocr(img)


def extract_bytes(filename: str, data: bytes) -> str:
    kind = kind_of(filename, data)
    if kind == "pdf":
        return _pdf(data)
    if kind == "xlsx":
        return _xlsx(data)
    if kind == "image":
        return _image(data)
    return data.decode("utf-8-sig", errors="replace")
