import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlmodel import Session, col, func, select

from app.config import get_settings
from app.db import get_session
from app.errors import api_error
from app.models import DocumentModel, PageRefModel
from app.schemas import (
    Document,
    PageDocuments,
    PageInfo,
    SearchHit,
    SearchRequest,
    TextBlock,
    UpdateDocumentRequest,
)
from app.services.oplog import OpValidationError, build_state, resolve_pdf_path
from app.services.store import document_dir, original_pdf_path, safe_path, thumb_path

router = APIRouter(prefix="/documents", tags=["documents"])


def _to_schema(row: DocumentModel) -> Document:
    return Document.model_validate(row, from_attributes=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _save_upload_temp(upload: UploadFile, temp_path: Path, max_upload_bytes: int) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    total = 0
    header = b""

    with temp_path.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_upload_bytes:
                raise ValueError("TOO_LARGE")
            if len(header) < 1024:
                need = 1024 - len(header)
                header += chunk[:need]
            digest.update(chunk)
            handle.write(chunk)

    await upload.close()
    return total, digest.hexdigest(), header


def _get_document_or_error(session: Session, document_id: str) -> DocumentModel | None:
    return session.get(DocumentModel, document_id)


def _resolve_page_index(session: Session, document: DocumentModel, page_uuid: str, version: int | None = None) -> tuple[int, list[str]]:
    state = build_state(session, document, version)
    if page_uuid not in state.order:
        raise OpValidationError("PAGE_NOT_FOUND", "Page not found.")
    return state.order.index(page_uuid), state.order


@router.post("", response_model=Document)
async def create_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    tmp_name = f"upload-{uuid4()}.pdf"
    tmp_path = safe_path(settings.store_root, "tmp", tmp_name)

    try:
        size_bytes, sha256, header = await _save_upload_temp(file, tmp_path, settings.max_upload_bytes)
    except ValueError:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return api_error(413, "TOO_LARGE", "Upload exceeds size limit.")

    if size_bytes == 0 or b"%PDF-" not in header:
        tmp_path.unlink(missing_ok=True)
        return api_error(422, "INVALID_PDF", "Uploaded file is not a valid PDF.")

    try:
        reader = PdfReader(str(tmp_path))
        page_count = len(reader.pages)
    except (PdfReadError, OSError, ValueError):
        tmp_path.unlink(missing_ok=True)
        return api_error(422, "INVALID_PDF", "Uploaded file could not be parsed as PDF.")

    if page_count > settings.max_pages:
        tmp_path.unlink(missing_ok=True)
        return api_error(413, "TOO_MANY_PAGES", "PDF exceeds page limit.")

    document_id = str(uuid4())
    now = _utcnow()
    doc_dir = document_dir(settings.store_root, document_id)
    doc_dir.mkdir(parents=True, exist_ok=False)
    output_path = original_pdf_path(settings.store_root, document_id)
    shutil.move(str(tmp_path), str(output_path))

    document = DocumentModel(
        id=document_id,
        title=title or Path(file.filename or "untitled.pdf").stem,
        original_name=file.filename or "untitled.pdf",
        sha256=sha256,
        size_bytes=size_bytes,
        page_count=page_count,
        cursor=0,
        version=0,
        created_at=now,
        updated_at=now,
    )
    session.add(document)

    for index in range(page_count):
        session.add(
            PageRefModel(
                document_id=document_id,
                page_uuid=str(uuid4()),
                origin="original",
                origin_index=index,
            )
        )

    session.commit()
    session.refresh(document)
    return _to_schema(document)


@router.get("", response_model=PageDocuments)
def list_documents(
    q: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    statement = select(DocumentModel)
    count_statement = select(func.count(col(DocumentModel.id)))

    if q:
        statement = statement.where(DocumentModel.title.contains(q))
        count_statement = count_statement.where(DocumentModel.title.contains(q))

    statement = statement.order_by(DocumentModel.updated_at.desc()).offset(offset).limit(limit)

    items = session.exec(statement).all()
    total = session.exec(count_statement).one()
    return PageDocuments(items=[_to_schema(item) for item in items], total=total, limit=limit, offset=offset)


@router.get("/{document_id}", response_model=Document)
def get_document(document_id: str, session: Session = Depends(get_session)):
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")
    return _to_schema(document)


@router.patch("/{document_id}", response_model=Document)
def update_document(
    document_id: str,
    request: UpdateDocumentRequest,
    session: Session = Depends(get_session),
):
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    document.title = request.title
    document.updated_at = _utcnow()
    session.add(document)
    session.commit()
    session.refresh(document)
    return _to_schema(document)


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, session: Session = Depends(get_session)):
    settings = get_settings()
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    session.delete(document)
    session.commit()

    doc_root = document_dir(settings.store_root, document_id)
    if doc_root.exists():
        shutil.rmtree(doc_root)

    return None


@router.get("/{document_id}/file")
def get_document_file(
    document_id: str,
    version: int | None = Query(default=None),
    session: Session = Depends(get_session),
):
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        path = resolve_pdf_path(session, document, version)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)

    if not path.exists():
        return api_error(404, "NOT_FOUND", "Document file missing.")

    safe_name = (document.original_name or "document.pdf").replace('"', "")
    return FileResponse(path=path, media_type="application/pdf", filename=safe_name)


@router.get("/{document_id}/pages", response_model=list[PageInfo])
def get_document_pages(document_id: str, session: Session = Depends(get_session)):
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    pdf_path = resolve_pdf_path(session, document, None)
    state = build_state(session, document)
    doc = pymupdf.open(pdf_path)

    if len(state.order) != doc.page_count:
        doc.close()
        return api_error(409, "OP_NOT_APPLICABLE", "Page metadata is out of sync.")

    items: list[PageInfo] = []
    for index, page_uuid in enumerate(state.order):
        page = doc[index]
        text = page.get_text("text").strip()
        size = page.mediabox_size
        items.append(
            PageInfo(
                uuid=page_uuid,
                index=index,
                width=float(size.x),
                height=float(size.y),
                rotation=page.rotation,
                has_text=bool(text),
                label=str(index + 1),
            )
        )

    doc.close()
    return items


@router.get("/{document_id}/pages/{page_uuid}/thumb")
def get_page_thumbnail(
    document_id: str,
    page_uuid: str,
    dpi: int = Query(default=110, ge=50, le=300),
    version: int | None = Query(default=None),
    session: Session = Depends(get_session),
):
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        page_index, _ = _resolve_page_index(session, document, page_uuid, version)
        pdf_path = resolve_pdf_path(session, document, version)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)

    cache_version = document.cursor if version is None else version
    settings = get_settings()
    cached = thumb_path(settings.store_root, document_id, cache_version, page_uuid)
    if cached.exists():
        return FileResponse(path=cached, media_type="image/webp", filename=f"{page_uuid}.webp")

    cached.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    img.save(cached, format="WEBP", quality=82)
    doc.close()
    return FileResponse(path=cached, media_type="image/webp", filename=f"{page_uuid}.webp")


@router.get("/{document_id}/pages/{page_uuid}/text", response_model=list[TextBlock])
def get_page_text(document_id: str, page_uuid: str, session: Session = Depends(get_session)):
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        page_index, _ = _resolve_page_index(session, document, page_uuid)
        pdf_path = resolve_pdf_path(session, document, None)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)

    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    blocks = page.get_text("blocks")
    results: list[TextBlock] = []
    for block_index, block in enumerate(blocks):
        text = str(block[4]).strip()
        if not text:
            continue
        results.append(
            TextBlock(
                page=page_uuid,
                block_index=block_index,
                rect=(float(block[0]), float(block[1]), float(block[2]), float(block[3])),
                text=text,
            )
        )
    doc.close()
    return results


@router.post("/{document_id}/search", response_model=list[SearchHit])
def search_document(document_id: str, request: SearchRequest, session: Session = Depends(get_session)):
    document = _get_document_or_error(session, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        pdf_path = resolve_pdf_path(session, document, None)
        state = build_state(session, document)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)

    doc = pymupdf.open(pdf_path)
    query = request.query if request.caseSensitive else request.query.lower()
    hits: list[SearchHit] = []

    for page_index, page_uuid in enumerate(state.order):
        page = doc[page_index]
        rects = page.search_for(request.query)
        if not rects:
            continue

        preview_text = ""
        page_text = page.get_text("text")
        if page_text:
            for line in page_text.splitlines():
                line_check = line if request.caseSensitive else line.lower()
                if query in line_check:
                    preview_text = line.strip()
                    break
        if not preview_text:
            preview_text = request.query

        hits.append(
            SearchHit(
                page=page_uuid,
                rects=[(float(r.x0), float(r.y0), float(r.x1), float(r.y1)) for r in rects],
                preview=preview_text,
            )
        )

    doc.close()
    return hits
