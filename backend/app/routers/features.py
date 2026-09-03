import hashlib
import io
import mimetypes
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import APIRouter, Depends, File, UploadFile
from PIL import Image
from pypdf import PdfReader
from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine, get_session
from app.errors import api_error
from app.models import DocumentModel, JobModel, PageRefModel
from app.schemas import (
    AnnotationPayload,
    AssetResponse,
    Document,
    ExportRequest,
    ExtractRequest,
    FormField,
    Job,
    SplitRequest,
)
from app.services.jobs import JobCancelledError, JobContext, start_job
from app.services.oplog import (
    OpValidationError,
    build_state,
    list_annotations,
    resolve_pdf_path,
)
from app.services.store import assets_dir, document_dir, original_pdf_path, safe_path

router = APIRouter(prefix="/documents", tags=["features"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(data: bytes) -> str:
    hasher = hashlib.sha256()
    hasher.update(data)
    return hasher.hexdigest()


def _to_document_schema(model: DocumentModel) -> Document:
    return Document.model_validate(model, from_attributes=True)


def _to_job_schema(model: JobModel) -> Job:
    return Job.model_validate(model, from_attributes=True)


def _create_document_record(
    session: Session,
    title: str,
    original_name: str,
    pdf_bytes: bytes,
) -> DocumentModel:
    settings = get_settings()
    document_id = str(uuid4())
    now = _now()
    doc_dir = document_dir(settings.store_root, document_id)
    doc_dir.mkdir(parents=True, exist_ok=False)

    original_path = original_pdf_path(settings.store_root, document_id)
    original_path.write_bytes(pdf_bytes)

    pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page_count = pdf.page_count
    pdf.close()

    row = DocumentModel(
        id=document_id,
        title=title,
        original_name=original_name,
        sha256=_sha256_bytes(pdf_bytes),
        size_bytes=len(pdf_bytes),
        page_count=page_count,
        cursor=0,
        version=0,
        created_at=now,
        updated_at=now,
    )
    session.add(row)

    for index in range(page_count):
        session.add(
            PageRefModel(
                document_id=document_id,
                page_uuid=str(uuid4()),
                origin="imported",
                origin_index=index,
            )
        )

    session.commit()
    session.refresh(row)
    return row


def _selected_indices(state_order: list[str], selected_pages: list[str] | None) -> list[int]:
    if not selected_pages:
        return list(range(len(state_order)))

    indices: list[int] = []
    for page_uuid in selected_pages:
        if page_uuid not in state_order:
            raise OpValidationError("PAGE_NOT_FOUND", f"Page not found: {page_uuid}")
        indices.append(state_order.index(page_uuid))
    return indices


@router.post("/{document_id}/assets", response_model=AssetResponse)
async def upload_asset(
    document_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    settings = get_settings()
    data = await file.read()
    await file.close()

    if not data:
        return api_error(422, "OP_NOT_APPLICABLE", "Empty file upload is not allowed.")

    ext = Path(file.filename or "asset.bin").suffix.lower()
    if not ext:
        ext = ".bin"
    asset_id = f"{uuid4()}{ext}"
    target = safe_path(assets_dir(settings.store_root, document_id), asset_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    mime_type, _ = mimetypes.guess_type(target.name)
    mime_type = mime_type or "application/octet-stream"

    page_count = None
    width = None
    height = None

    if target.suffix.lower() == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(data))
            page_count = len(reader.pages)
        except (OSError, ValueError):
            page_count = None
    elif target.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        image = Image.open(io.BytesIO(data))
        width, height = image.size

    return AssetResponse(
        assetId=asset_id,
        filename=file.filename or asset_id,
        mimeType=mime_type,
        pageCount=page_count,
        width=width,
        height=height,
    )


@router.get("/{document_id}/forms", response_model=list[FormField])
def list_forms(document_id: str, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        pdf_path = resolve_pdf_path(session, document, None)
        state = build_state(session, document)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)

    doc = pymupdf.open(pdf_path)
    try:
        fields: list[FormField] = []
        for index, page_uuid in enumerate(state.order):
            page = doc[index]
            widgets = page.widgets()
            if not widgets:
                continue
            for widget in widgets:
                field_type_name = (widget.field_type_string or "text").lower()
                mapped: str
                if "check" in field_type_name:
                    mapped = "checkbox"
                elif "radio" in field_type_name:
                    mapped = "radio"
                elif "combo" in field_type_name:
                    mapped = "combo"
                elif "list" in field_type_name:
                    mapped = "list"
                elif "signature" in field_type_name:
                    mapped = "signature"
                else:
                    mapped = "text"

                rect = widget.rect
                fields.append(
                    FormField(
                        page=page_uuid,
                        name=widget.field_name or "",
                        field_type=mapped,
                        value=widget.field_value,
                        rect=(float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
                    )
                )
        return fields
    finally:
        doc.close()


@router.get("/{document_id}/annotations", response_model=list[AnnotationPayload])
def get_annotations(document_id: str, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")
    return list_annotations(session, document)


@router.post("/{document_id}/export", response_model=Job)
def export_document(
    document_id: str,
    request: ExportRequest,
    session: Session = Depends(get_session),
):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    now = _now()
    job = JobModel(
        id=str(uuid4()),
        document_id=document_id,
        kind="export",
        status="queued",
        progress=0.0,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    def _run_export(ctx: JobContext) -> None:
        with Session(get_engine()) as worker:
            live_doc = worker.get(DocumentModel, document_id)
            if not live_doc:
                raise ValueError("Document not found.")

            pdf_path = resolve_pdf_path(worker, live_doc, None)
            state = build_state(worker, live_doc)
            indices = _selected_indices(state.order, request.pages)

        settings = get_settings()
        base_name = f"export-{job.id}"
        doc = pymupdf.open(pdf_path)

        try:
            ctx.update_progress(0.04, "Preparing export")
            ctx.ensure_not_cancelled()

            if request.format == "pdf":
                out = pymupdf.open()
                total = max(1, len(indices))
                for step, idx in enumerate(indices, start=1):
                    ctx.ensure_not_cancelled()
                    out.insert_pdf(doc, from_page=idx, to_page=idx)
                    ctx.update_progress(0.1 + (0.75 * (step / total)), f"Exporting page {step}/{total}")

                if request.flatten:
                    ctx.ensure_not_cancelled()
                    ctx.update_progress(0.9, "Flattening annotations")
                    out.bake(annots=True, widgets=True)

                output = safe_path(settings.store_root, "documents", document_id, "derived", f"{base_name}.pdf")
                output.parent.mkdir(parents=True, exist_ok=True)
                out.save(output, garbage=4, deflate=True, clean=True)
                out.close()
                result_path = output
            elif request.format in {"png", "jpeg"}:
                image_ext = "png" if request.format == "png" else "jpg"
                pil_format = "PNG" if request.format == "png" else "JPEG"
                quality = 85
                scale = request.dpi / 72.0

                if len(indices) == 1:
                    idx = indices[0]
                    ctx.ensure_not_cancelled()
                    page = doc[idx]
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
                    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    output = safe_path(settings.store_root, "documents", document_id, "derived", f"{base_name}.{image_ext}")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    save_kwargs = {"format": pil_format}
                    if pil_format == "JPEG":
                        save_kwargs["quality"] = quality
                    image.save(output, **save_kwargs)
                    result_path = output
                else:
                    output = safe_path(settings.store_root, "documents", document_id, "derived", f"{base_name}.zip")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    total = max(1, len(indices))
                    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                        for num, idx in enumerate(indices, start=1):
                            ctx.ensure_not_cancelled()
                            page = doc[idx]
                            pix = page.get_pixmap(
                                matrix=pymupdf.Matrix(scale, scale),
                                colorspace=pymupdf.csRGB,
                                alpha=False,
                            )
                            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                            buf = io.BytesIO()
                            save_kwargs = {"format": pil_format}
                            if pil_format == "JPEG":
                                save_kwargs["quality"] = quality
                            image.save(buf, **save_kwargs)
                            archive.writestr(f"page-{num:04d}.{image_ext}", buf.getvalue())
                            ctx.update_progress(
                                0.1 + (0.8 * (num / total)),
                                f"Rendering page {num}/{total}",
                            )
                    result_path = output
            else:
                raise OpValidationError("OP_NOT_APPLICABLE", "Unsupported export format.")

            ctx.ensure_not_cancelled()
            ctx.complete(result_path=result_path, message="Export completed.")
        except JobCancelledError:
            raise
        except OpValidationError as error:
            ctx.fail(error.message)
        except (OSError, RuntimeError, ValueError) as error:
            if "cancelled" in str(error).lower():
                raise JobCancelledError("cancelled") from error
            ctx.fail(str(error))
        finally:
            doc.close()

    start_job(job.id, _run_export)
    session.refresh(job)
    return _to_job_schema(job)


@router.post("/{document_id}/extract", response_model=Document)
def extract_pages(
    document_id: str,
    request: ExtractRequest,
    session: Session = Depends(get_session),
):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        pdf_path = resolve_pdf_path(session, document, None)
        state = build_state(session, document)
        indices = _selected_indices(state.order, request.pages)

        source = pymupdf.open(pdf_path)
        out = pymupdf.open()
        for idx in indices:
            out.insert_pdf(source, from_page=idx, to_page=idx)

        data = out.tobytes(garbage=4, deflate=True, clean=True)
        out.close()
        source.close()

        title = request.title or f"{document.title} - extract"
        row = _create_document_record(session, title=title, original_name=f"{title}.pdf", pdf_bytes=data)
        return _to_document_schema(row)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)
    except (OSError, RuntimeError, ValueError) as error:
        return api_error(500, "JOB_FAILED", str(error))


@router.post("/{document_id}/split", response_model=list[Document])
def split_document(
    document_id: str,
    request: SplitRequest,
    session: Session = Depends(get_session),
):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        pdf_path = resolve_pdf_path(session, document, None)
        state = build_state(session, document)
        if request.splitAfterIndex >= len(state.order) - 1:
            return api_error(422, "OP_NOT_APPLICABLE", "splitAfterIndex must leave pages on both sides.")

        source = pymupdf.open(pdf_path)

        left = pymupdf.open()
        for idx in range(0, request.splitAfterIndex + 1):
            left.insert_pdf(source, from_page=idx, to_page=idx)

        right = pymupdf.open()
        for idx in range(request.splitAfterIndex + 1, len(state.order)):
            right.insert_pdf(source, from_page=idx, to_page=idx)

        left_bytes = left.tobytes(garbage=4, deflate=True, clean=True)
        right_bytes = right.tobytes(garbage=4, deflate=True, clean=True)

        left.close()
        right.close()
        source.close()

        left_title = request.leftTitle or f"{document.title} - part 1"
        right_title = request.rightTitle or f"{document.title} - part 2"

        left_doc = _create_document_record(session, left_title, f"{left_title}.pdf", left_bytes)
        right_doc = _create_document_record(session, right_title, f"{right_title}.pdf", right_bytes)

        return [_to_document_schema(left_doc), _to_document_schema(right_doc)]
    except OpValidationError as error:
        return api_error(409, error.code, error.message)
    except (OSError, RuntimeError, ValueError) as error:
        return api_error(500, "JOB_FAILED", str(error))
