import io
from collections.abc import Callable
from typing import Any

import numpy as np
import pymupdf
from PIL import Image, ImageEnhance, ImageFilter

from app.schemas import ScanParams


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) != 6:
        return (255, 255, 255)
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _render_page(page: pymupdf.Page, dpi: int) -> Image.Image:
    scale = dpi / 72.0
    matrix = pymupdf.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=True)
    image = Image.frombytes("RGBA", [pix.width, pix.height], pix.samples)
    return image


def _apply_pipeline(image: Image.Image, params: ScanParams, rng: np.random.Generator) -> Image.Image:
    paper = Image.new("RGBA", image.size, _hex_to_rgb(params.paper_tint) + (255,))
    merged = Image.alpha_composite(paper, image).convert("RGB")

    gamma = max(0.4, params.gamma)
    arr = np.array(merged, dtype=np.float32)
    arr = 255.0 * np.power(arr / 255.0, 1.0 / gamma)

    arr = np.clip(arr * params.brightness, 0, 255)

    luma = arr.mean(axis=2, keepdims=True)
    arr = np.clip(luma + params.contrast * (arr - luma), 0, 255)

    if params.noise_sigma > 0:
        if params.noise_mono:
            noise = rng.normal(0, params.noise_sigma, (arr.shape[0], arr.shape[1], 1))
            arr = arr + noise
        else:
            arr = arr + rng.normal(0, params.noise_sigma, arr.shape)
        arr = np.clip(arr, 0, 255)

    output = Image.fromarray(arr.astype(np.uint8), mode="RGB")

    if params.blur_sigma > 0:
        output = output.filter(ImageFilter.GaussianBlur(radius=params.blur_sigma))

    if params.color_mode == "gray":
        output = output.convert("L").convert("RGB")
    elif params.color_mode == "bw":
        gray = output.convert("L")
        if params.bw_dither:
            output = gray.convert("1", dither=Image.FLOYDSTEINBERG).convert("RGB")
        else:
            output = gray.point(lambda p: 255 if p >= params.bw_threshold else 0).convert("RGB")

    if params.downsample < 1.0:
        new_size = (
            max(1, int(output.width * params.downsample)),
            max(1, int(output.height * params.downsample)),
        )
        output = output.resize(new_size, Image.Resampling.LANCZOS)

    # Small optics pass for scanner feel.
    output = ImageEnhance.Sharpness(output).enhance(0.92)
    return output


def page_preview_bytes(page: pymupdf.Page, params: ScanParams, preview_dpi: int, seed: int) -> bytes:
    image = _render_page(page, preview_dpi)
    rng = np.random.default_rng([seed, page.number])
    scanned = _apply_pipeline(image, params, rng)
    buffer = io.BytesIO()
    scanned.save(buffer, format="WEBP", quality=min(95, max(40, params.jpeg_quality)))
    return buffer.getvalue()


def export_document_bytes(
    doc: pymupdf.Document,
    params: ScanParams,
    seed: int,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> bytes:
    out = pymupdf.open()
    page_count = max(1, doc.page_count)

    for index in range(doc.page_count):
        if cancel_callback and cancel_callback():
            raise RuntimeError("Job cancelled")

        source_page = doc[index]
        image = _render_page(source_page, params.dpi)
        rng = np.random.default_rng([seed, index])
        scanned = _apply_pipeline(image, params, rng)
        img_buf = io.BytesIO()
        scanned.save(img_buf, format="JPEG", quality=params.jpeg_quality)

        page = out.new_page(width=source_page.rect.width, height=source_page.rect.height)
        page.insert_image(page.rect, stream=img_buf.getvalue())

        if progress_callback:
            progress_callback(index + 1, page_count)

    out.set_metadata({})
    out_buf = io.BytesIO()
    out.save(out_buf, garbage=4, deflate=True, clean=True)
    out.close()
    return out_buf.getvalue()


def params_from_payload(payload: dict[str, Any]) -> ScanParams:
    return ScanParams.model_validate(payload)
