import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from pypdf import PdfReader
from sqlmodel import Session, col, func, select

from app.config import get_settings
from app.db import get_session
from app.errors import api_error
from app.models import DocumentModel
from app.schemas import Document, PageDocuments, UpdateDocumentRequest
from app.services.store import document_dir, original_pdf_path, safe_path

router = APIRouter(prefix="/documents", tags=["documents"])


def _to_schema(row: DocumentModel) -> Document:
    return Document.model_validate(row, from_attributes=True)


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
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return api_error(422, "INVALID_PDF", "Uploaded file could not be parsed as PDF.")

    if page_count > settings.max_pages:
        tmp_path.unlink(missing_ok=True)
        return api_error(413, "TOO_MANY_PAGES", "PDF exceeds page limit.")

    document_id = str(uuid4())
    now = datetime.now(timezone.utc)
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
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")
    return _to_schema(document)


@router.patch("/{document_id}", response_model=Document)
def update_document(
    document_id: str,
    request: UpdateDocumentRequest,
    session: Session = Depends(get_session),
):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    document.title = request.title
    document.updated_at = datetime.now(timezone.utc)
    session.add(document)
    session.commit()
    session.refresh(document)
    return _to_schema(document)


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, session: Session = Depends(get_session)):
    settings = get_settings()
    document = session.get(DocumentModel, document_id)
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
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    if version is not None and version != 0:
        return api_error(409, "OP_NOT_APPLICABLE", "Only version 0 is available in M0.")

    settings = get_settings()
    path = original_pdf_path(settings.store_root, document_id)
    if not path.exists():
        return api_error(404, "NOT_FOUND", "Document file missing.")

    safe_name = (document.original_name or "document.pdf").replace('"', "")
    return FileResponse(path=path, media_type="application/pdf", filename=safe_name)
