from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine, get_session
from app.errors import api_error
from app.models import DocumentModel, JobModel
from app.schemas import CompressEstimateResponse, CompressRequest, Job
from app.services.compress import compress_pdf_file, estimate_compression
from app.services.jobs import JobCancelledError, JobContext, start_job
from app.services.oplog import OpValidationError, resolve_pdf_path
from app.services.store import safe_path

router = APIRouter(prefix="/documents", tags=["compress"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_schema(row: JobModel) -> Job:
    return Job.model_validate(row, from_attributes=True)


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} B"


@router.post("/{document_id}/compress", response_model=Job)
def compress_document(
    document_id: str,
    request: CompressRequest,
    session: Session = Depends(get_session),
):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    settings = get_settings()
    if request.imageDpi > settings.max_compress_dpi:
        return api_error(
            422,
            "OP_NOT_APPLICABLE",
            f"imageDpi must be <= {settings.max_compress_dpi}.",
        )

    now = _now()
    job = JobModel(
        id=str(uuid4()),
        document_id=document_id,
        kind="compress",
        status="queued",
        progress=0.0,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    def _run_compress(ctx: JobContext) -> None:
        with Session(get_engine()) as worker:
            live_doc = worker.get(DocumentModel, document_id)
            if not live_doc:
                raise ValueError("Document not found.")
            pdf_path = resolve_pdf_path(worker, live_doc, None)

        source_size = Path(pdf_path).stat().st_size
        ctx.update_progress(0.05, "Preparing compression")

        try:
            result = compress_pdf_file(
                source_path=Path(pdf_path),
                profile=request.profile,
                strip_metadata=request.stripMetadata,
                image_dpi=request.imageDpi,
                progress_callback=lambda done, total: ctx.update_progress(
                    0.1 + (0.75 * (done / max(1, total))),
                    f"Compressing page {done}/{total}",
                ),
                cancel_callback=ctx.is_cancelled,
            )
            ctx.ensure_not_cancelled()

            output = safe_path(
                settings.store_root,
                "documents",
                document_id,
                "derived",
                f"compress-{job.id}.pdf",
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(result.data)

            delta = source_size - result.output_bytes
            if delta > 0:
                reduction = (delta / source_size) * 100 if source_size else 0
                message = (
                    f"Reduced {reduction:.1f}% "
                    f"({_format_bytes(source_size)} -> {_format_bytes(result.output_bytes)}) "
                    f"with profile '{request.profile}'."
                )
            else:
                message = (
                    f"Output is larger "
                    f"({_format_bytes(source_size)} -> {_format_bytes(result.output_bytes)}); "
                    f"try profile 'strong'."
                )

            if result.note:
                message = f"{message} {result.note}"

            ctx.complete(result_path=output, message=message)
        except OpValidationError as error:
            ctx.fail(error.message)
        except (OSError, RuntimeError, ValueError) as error:
            if "cancelled" in str(error).lower():
                raise JobCancelledError("cancelled") from error
            ctx.fail(str(error))

    start_job(job.id, _run_compress)
    session.refresh(job)
    return _job_schema(job)


@router.post("/{document_id}/compress/estimate", response_model=CompressEstimateResponse)
def estimate_document_compression(
    document_id: str,
    request: CompressRequest,
    session: Session = Depends(get_session),
):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    settings = get_settings()
    if request.imageDpi > settings.max_compress_dpi:
        return api_error(
            422,
            "OP_NOT_APPLICABLE",
            f"imageDpi must be <= {settings.max_compress_dpi}.",
        )

    try:
        pdf_path = resolve_pdf_path(session, document, None)
        estimate = estimate_compression(
            source_path=Path(pdf_path),
            profile=request.profile,
            strip_metadata=request.stripMetadata,
            image_dpi=request.imageDpi,
        )
        return CompressEstimateResponse(
            sourceBytes=estimate.source_bytes,
            estimatedBytes=estimate.estimated_bytes,
            estimatedReductionPercent=estimate.estimated_reduction_percent,
            profile=request.profile,
            note=estimate.note,
        )
    except OpValidationError as error:
        return api_error(409, error.code, error.message)
    except (OSError, RuntimeError, ValueError) as error:
        return api_error(500, "JOB_FAILED", str(error))
