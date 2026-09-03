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


@dataclass
class CompressionEstimate:
    source_bytes: int
    estimated_bytes: int
    estimated_reduction_percent: float
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


def _estimate_ratio_for_profile(
    profile: Literal["light", "balanced", "strong"],
    strip_metadata: bool,
) -> float:
    if profile == "light":
        return 0.9 if strip_metadata else 0.94
    if profile == "balanced":
        return 0.76 if strip_metadata else 0.8
    return 0.55 if strip_metadata else 0.58


def estimate_compression(
    source_path: Path,
    profile: Literal["light", "balanced", "strong"],
    strip_metadata: bool,
    image_dpi: int,
) -> CompressionEstimate:
    source_bytes = source_path.stat().st_size
    doc = pymupdf.open(source_path)
    try:
        note: str | None = None

        if profile == "strong":
            page_count = max(1, doc.page_count)
            sample_count = min(3, page_count)
            scale = image_dpi / 72.0
            matrix = pymupdf.Matrix(scale, scale)
            sampled_bytes = 0

            for index in range(sample_count):
                page = doc[index]
                pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                img_buf = io.BytesIO()
                image.save(img_buf, format="JPEG", quality=56, optimize=True)
                sampled_bytes += len(img_buf.getvalue())

            average_per_page = sampled_bytes / sample_count
            estimated_bytes = int((average_per_page * page_count) + (32 * 1024))
            if strip_metadata:
                estimated_bytes = int(estimated_bytes * 0.98)
            note = "Strong profile estimate assumes rasterization and text-layer removal."
        else:
            ratio = _estimate_ratio_for_profile(profile, strip_metadata)
            estimated_bytes = int(source_bytes * ratio)

        estimated_bytes = max(1, estimated_bytes)
        reduction = ((source_bytes - estimated_bytes) / source_bytes) * 100 if source_bytes else 0.0
        return CompressionEstimate(
            source_bytes=source_bytes,
            estimated_bytes=estimated_bytes,
            estimated_reduction_percent=round(reduction, 2),
            note=note,
        )
    finally:
        doc.close()
