import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine, get_session
from app.errors import api_error
from app.models import DocumentModel, JobModel, PresetModel
from app.scan.params import BUILTIN_PRESETS
from app.scan.pipeline import export_document_bytes, page_preview_bytes
from app.schemas import (
    CreatePresetRequest,
    Job,
    OpRequest,
    Preset,
    ScanPreviewRequest,
    ScanRunRequest,
)
from app.services.jobs import (
    JobCancelledError,
    JobContext,
    start_job,
    stream_job_events,
)
from app.services.jobs import (
    cancel_job as request_job_cancel,
)
from app.services.oplog import (
    OpValidationError,
    append_op,
    build_state,
    resolve_pdf_path,
)
from app.services.store import safe_path

scan_router = APIRouter(prefix="/documents", tags=["scan"])
preset_router = APIRouter(prefix="/scan", tags=["scan"])
job_router = APIRouter(prefix="/jobs", tags=["jobs"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_schema(row: JobModel) -> Job:
    return Job.model_validate(row, from_attributes=True)


def _preset_schema(row: PresetModel) -> Preset:
    return Preset(
        id=row.id,
        name=row.name,
        params=json.loads(row.params),
        builtin=row.builtin,
        created_at=row.created_at,
    )


@preset_router.get("/presets", response_model=list[Preset])
def list_presets(session: Session = Depends(get_session)):
    presets: list[Preset] = []
    for name, params in BUILTIN_PRESETS.items():
        presets.append(
            Preset(
                id=f"builtin-{name}",
                name=name,
                params=params.model_dump(),
                builtin=True,
                created_at=_now(),
            )
        )

    rows = session.exec(select(PresetModel).where(PresetModel.builtin.is_(False))).all()
    presets.extend(_preset_schema(row) for row in rows)
    return presets


@preset_router.post("/presets", response_model=Preset)
def create_preset(request: CreatePresetRequest, session: Session = Depends(get_session)):
    now = _now()
    row = PresetModel(
        id=str(uuid4()),
        name=request.name,
        params=json.dumps(request.params.model_dump(), sort_keys=True),
        builtin=False,
        created_at=now,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _preset_schema(row)


@preset_router.delete("/presets/{preset_id}", status_code=204)
def delete_preset(preset_id: str, session: Session = Depends(get_session)):
    row = session.get(PresetModel, preset_id)
    if not row:
        return api_error(404, "NOT_FOUND", "Preset not found.")
    if row.builtin:
        return api_error(409, "OP_NOT_APPLICABLE", "Built-in preset cannot be deleted.")
    session.delete(row)
    session.commit()
    return None


@scan_router.post("/{document_id}/scan/preview")
def scan_preview(document_id: str, request: ScanPreviewRequest, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        pdf_path = resolve_pdf_path(session, document, None)
        state = build_state(session, document)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)

    if request.pageUuid not in state.order:
        return api_error(404, "PAGE_NOT_FOUND", "Page not found.")

    page_index = state.order.index(request.pageUuid)
    seed = request.params.seed if request.params.seed is not None else int(uuid4().int & 0x7FFFFFFF)

    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    body = page_preview_bytes(page, request.params, request.previewDpi, seed)
    doc.close()
    return Response(content=body, media_type="image/webp")


@scan_router.post("/{document_id}/scan", response_model=Job)
def scan_document(document_id: str, request: ScanRunRequest, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    now = _now()
    job = JobModel(
        id=str(uuid4()),
        document_id=document_id,
        kind="scan",
        status="queued",
        progress=0.0,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    if request.mode == "in_place":
        try:
            params = request.params.model_dump()
            if params.get("seed") is None:
                params["seed"] = int(uuid4().int & 0x7FFFFFFF)

            append_op(
                session,
                document,
                OpRequest(kind="scan.apply", payload={"params": params}),
            )

            job.status = "done"
            job.progress = 1.0
            job.message = "In-place scan committed and undo/redo disabled for this branch."
            job.updated_at = _now()
            session.add(job)
            session.commit()
            session.refresh(job)
            return _job_schema(job)
        except OpValidationError as error:
            job.status = "error"
            job.message = error.message
            job.updated_at = _now()
            session.add(job)
            session.commit()
            session.refresh(job)
            return _job_schema(job)

    seed = request.params.seed if request.params.seed is not None else int(uuid4().int & 0x7FFFFFFF)

    def _run_scan_export(ctx: JobContext) -> None:
        with Session(get_engine()) as worker:
            live_doc = worker.get(DocumentModel, document_id)
            if not live_doc:
                raise ValueError("Document not found.")
            pdf_path = resolve_pdf_path(worker, live_doc, None)

        doc = pymupdf.open(pdf_path)
        try:
            ctx.update_progress(0.04, "Preparing scan export")
            data = export_document_bytes(
                doc,
                request.params,
                seed,
                progress_callback=lambda done, total: ctx.update_progress(
                    0.08 + (0.84 * (done / max(1, total))),
                    f"Scanning page {done}/{total}",
                ),
                cancel_callback=ctx.is_cancelled,
            )
            ctx.ensure_not_cancelled()

            settings = get_settings()
            output = safe_path(settings.store_root, "documents", document_id, "derived", f"scan-{job.id}.pdf")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            ctx.complete(result_path=output, message=f"Completed with seed {seed}.")
        except (OSError, RuntimeError, ValueError) as error:
            if "cancelled" in str(error).lower():
                raise JobCancelledError("cancelled") from error
            ctx.fail(str(error))
        finally:
            doc.close()

    start_job(job.id, _run_scan_export)
    session.refresh(job)
    return _job_schema(job)


@job_router.get("/{job_id}", response_model=Job)
def get_job(job_id: str, session: Session = Depends(get_session)):
    job = session.get(JobModel, job_id)
    if not job:
        return api_error(404, "NOT_FOUND", "Job not found.")
    return _job_schema(job)


@job_router.get("/{job_id}/events")
def get_job_events(job_id: str, session: Session = Depends(get_session)):
    job = session.get(JobModel, job_id)
    if not job:
        return api_error(404, "NOT_FOUND", "Job not found.")

    return StreamingResponse(
        stream_job_events(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@job_router.post("/{job_id}/cancel", status_code=202)
def cancel_job_endpoint(job_id: str, session: Session = Depends(get_session)):
    if not session.get(JobModel, job_id):
        return api_error(404, "NOT_FOUND", "Job not found.")
    request_job_cancel(job_id)
    return {"accepted": True}


@job_router.get("/{job_id}/result")
def get_job_result(job_id: str, session: Session = Depends(get_session)):
    job = session.get(JobModel, job_id)
    if not job:
        return api_error(404, "NOT_FOUND", "Job not found.")
    if job.status != "done" or not job.result_path:
        return api_error(409, "JOB_FAILED", "Job has no downloadable result.")

    path = Path(job.result_path)
    if not path.exists():
        return api_error(404, "NOT_FOUND", "Result file is missing.")

    mime_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path=path,
        media_type=mime_type or "application/octet-stream",
        filename=path.name,
    )
