import json
import threading
import time
from collections.abc import Callable, Generator
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session

from app.db import get_engine
from app.models import JobModel

_TERMINAL = {"done", "error", "cancelled"}
_cancel_events: dict[str, threading.Event] = {}
_lock = threading.Lock()


class JobCancelledError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_payload(job: JobModel) -> dict:
    return {
        "id": job.id,
        "progress": float(job.progress),
        "status": job.status,
        "message": job.message,
        "updated_at": job.updated_at.isoformat(),
    }


def _sse_frame(event: str, payload: dict) -> str:
    return f"event: {event}\\ndata: {json.dumps(payload, separators=(',', ':'))}\\n\\n"


class JobContext:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def _mutate(self, mutator: Callable[[JobModel], None]) -> bool:
        with Session(get_engine()) as session:
            job = session.get(JobModel, self.job_id)
            if not job:
                return False
            mutator(job)
            job.updated_at = _now()
            session.add(job)
            session.commit()
            return True

    def is_cancelled(self) -> bool:
        with _lock:
            event = _cancel_events.get(self.job_id)
            if event and event.is_set():
                return True

        with Session(get_engine()) as session:
            job = session.get(JobModel, self.job_id)
            if not job:
                return True
            return job.status == "cancelled"

    def set_running(self, message: str | None = None) -> None:
        def _apply(job: JobModel) -> None:
            job.status = "running"
            if message is not None:
                job.message = message

        self._mutate(_apply)

    def update_progress(self, progress: float, message: str | None = None) -> None:
        capped = max(0.0, min(1.0, progress))

        def _apply(job: JobModel) -> None:
            if job.status in _TERMINAL:
                return
            job.status = "running"
            job.progress = capped
            if message is not None:
                job.message = message

        self._mutate(_apply)

    def complete(self, result_path: Path | None = None, message: str | None = None) -> None:
        def _apply(job: JobModel) -> None:
            job.status = "done"
            job.progress = 1.0
            if message is not None:
                job.message = message
            if result_path is not None:
                job.result_path = str(result_path)

        self._mutate(_apply)

    def fail(self, message: str) -> None:
        def _apply(job: JobModel) -> None:
            if job.status == "cancelled":
                return
            job.status = "error"
            job.message = message

        self._mutate(_apply)

    def ensure_not_cancelled(self) -> None:
        if self.is_cancelled():
            raise JobCancelledError("Job cancelled")


def start_job(job_id: str, runner: Callable[[JobContext], None]) -> None:
    with _lock:
        _cancel_events[job_id] = threading.Event()

    def _run() -> None:
        ctx = JobContext(job_id)
        try:
            ctx.set_running("Job started")
            runner(ctx)
        except JobCancelledError:
            def _mark_cancelled(job: JobModel) -> None:
                job.status = "cancelled"
                job.message = "Job cancelled."

            ctx._mutate(_mark_cancelled)
        except Exception as error:  # noqa: BLE001  # pragma: no cover
            ctx.fail(str(error))
        finally:
            with _lock:
                _cancel_events.pop(job_id, None)

    thread = threading.Thread(target=_run, name=f"job-{job_id[:8]}", daemon=True)
    thread.start()


def cancel_job(job_id: str) -> bool:
    with Session(get_engine()) as session:
        job = session.get(JobModel, job_id)
        if not job:
            return False
        if job.status not in _TERMINAL:
            job.status = "cancelled"
            job.message = "Job cancellation requested."
            job.updated_at = _now()
            session.add(job)
            session.commit()

    with _lock:
        event = _cancel_events.get(job_id)
        if event:
            event.set()

    return True


def stream_job_events(job_id: str, poll_interval: float = 0.25) -> Generator[str, None, None]:
    sent_updated_at: str | None = None

    while True:
        with Session(get_engine()) as session:
            job = session.get(JobModel, job_id)

        if not job:
            yield _sse_frame("error", {"code": "NOT_FOUND", "message": "Job not found."})
            break

        payload = _job_payload(job)
        if payload["updated_at"] != sent_updated_at:
            sent_updated_at = payload["updated_at"]
            yield _sse_frame("progress", payload)

        if job.status == "done":
            yield _sse_frame("done", payload)
            break
        if job.status in {"error", "cancelled"}:
            yield _sse_frame("error", payload)
            break

        time.sleep(poll_interval)
