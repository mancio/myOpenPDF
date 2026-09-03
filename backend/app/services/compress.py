import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf
from PIL import Image


@dataclass
class CompressionResult:
    data: bytes
    output_bytes: int
    note: str | None = None


def _compress_lossless(
    doc: pymupdf.Document,
    strip_metadata: bool,
    profile: Literal["light", "balanced"],
) -> bytes:
    if strip_metadata:
        doc.set_metadata({})

    save_kwargs = {
        "garbage": 3 if profile == "light" else 4,
        "deflate": True,
        "deflate_images": True,
        "deflate_fonts": True,
        "clean": True,
    }

    buffer = io.BytesIO()
    doc.save(buffer, **save_kwargs)
    return buffer.getvalue()


def _compress_strong(doc: pymupdf.Document, strip_metadata: bool, image_dpi: int) -> tuple[bytes, str]:
    out = pymupdf.open()
    scale = image_dpi / 72.0
    matrix = pymupdf.Matrix(scale, scale)

    for index in range(doc.page_count):
        page = doc[index]
        pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
        image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        img_buf = io.BytesIO()
        image.save(img_buf, format="JPEG", quality=56, optimize=True)

        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=img_buf.getvalue())

    if strip_metadata:
        out.set_metadata({})

    output = io.BytesIO()
    out.save(output, garbage=4, deflate=True, clean=True)
    out.close()
    return output.getvalue(), "Strong profile converts pages to images and removes the text layer."


def compress_pdf_file(
    source_path: Path,
    profile: Literal["light", "balanced", "strong"],
    strip_metadata: bool,
    image_dpi: int,
) -> CompressionResult:
    doc = pymupdf.open(source_path)
    try:
        if profile in ("light", "balanced"):
            data = _compress_lossless(doc, strip_metadata, profile)
            return CompressionResult(data=data, output_bytes=len(data), note=None)

        data, note = _compress_strong(doc, strip_metadata, image_dpi)
        return CompressionResult(data=data, output_bytes=len(data), note=note)
    finally:
        doc.close()
