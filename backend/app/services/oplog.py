import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymupdf
from sqlmodel import Session, delete, func, select

from app.config import get_settings
from app.models import DocumentModel, OpModel, PageRefModel
from app.schemas import OpRequest, OpResult, StoredOp
from app.services.store import derived_pdf_path, original_pdf_path

SUPPORTED_OPS = {"page.rotate", "page.delete", "page.reorder", "scan.apply"}


@dataclass
class EditorState:
    order: list[str]
    rotations: dict[str, int] = field(default_factory=dict)

    def clone(self) -> "EditorState":
        return EditorState(order=list(self.order), rotations=dict(self.rotations))


class OpValidationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_payload(payload: str) -> dict[str, Any]:
    return json.loads(payload)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_ops(session: Session, document_id: str) -> list[OpModel]:
    return session.exec(
        select(OpModel).where(OpModel.document_id == document_id).order_by(OpModel.seq.asc())
    ).all()


def load_base_state(session: Session, document_id: str) -> EditorState:
    refs = session.exec(
        select(PageRefModel)
        .where(PageRefModel.document_id == document_id)
        .order_by(PageRefModel.origin_index.asc())
    ).all()
    return EditorState(order=[item.page_uuid for item in refs])


def apply_state_op(state: EditorState, kind: str, payload: dict[str, Any]) -> None:
    if kind not in SUPPORTED_OPS:
        raise OpValidationError("UNSUPPORTED_FEATURE", f"Operation kind '{kind}' is not implemented yet.")

    if kind == "page.rotate":
        pages = payload.get("pages")
        delta = payload.get("delta")
        if not isinstance(pages, list) or not pages:
            raise OpValidationError("OP_NOT_APPLICABLE", "page.rotate requires non-empty pages list.")
        if delta not in (90, 180, 270):
            raise OpValidationError("OP_NOT_APPLICABLE", "page.rotate delta must be 90, 180 or 270.")
        for page_uuid in pages:
            if page_uuid not in state.order:
                raise OpValidationError("PAGE_NOT_FOUND", f"Page not found: {page_uuid}")
            current = state.rotations.get(page_uuid, 0)
            state.rotations[page_uuid] = (current + int(delta)) % 360
        return

    if kind == "page.delete":
        pages = payload.get("pages")
        if not isinstance(pages, list) or not pages:
            raise OpValidationError("OP_NOT_APPLICABLE", "page.delete requires non-empty pages list.")
        to_delete: set[str] = set()
        for page_uuid in pages:
            if page_uuid not in state.order:
                raise OpValidationError("PAGE_NOT_FOUND", f"Page not found: {page_uuid}")
            to_delete.add(page_uuid)
        state.order = [item for item in state.order if item not in to_delete]
        return

    if kind == "page.reorder":
        order = payload.get("order")
        if not isinstance(order, list) or not order:
            raise OpValidationError("OP_NOT_APPLICABLE", "page.reorder requires an order list.")
        if len(order) != len(state.order):
            raise OpValidationError("OP_NOT_APPLICABLE", "page.reorder order length mismatch.")
        if set(order) != set(state.order):
            raise OpValidationError("OP_NOT_APPLICABLE", "page.reorder must contain the same page UUIDs.")
        state.order = [str(item) for item in order]
        return

    if kind == "scan.apply":
        params = payload.get("params")
        if not isinstance(params, dict):
            raise OpValidationError("OP_NOT_APPLICABLE", "scan.apply requires params object.")
        return


def build_state(session: Session, document: DocumentModel, upto: int | None = None) -> EditorState:
    limit = document.cursor if upto is None else upto
    state = load_base_state(session, document.id)
    for op in load_ops(session, document.id):
        if op.seq > limit:
            break
        apply_state_op(state, op.kind, _parse_payload(op.payload))
    return state


def _calc_flags(cursor: int, max_seq: int) -> tuple[bool, bool]:
    return cursor > 0, cursor < max_seq


def latest_applied_op(session: Session, document: DocumentModel) -> OpModel | None:
    if document.cursor <= 0:
        return None
    return session.exec(
        select(OpModel)
        .where(OpModel.document_id == document.id)
        .where(OpModel.seq == document.cursor)
    ).first()


def list_ops_response(session: Session, document: DocumentModel) -> tuple[int, list[StoredOp]]:
    rows = load_ops(session, document.id)
    return (
        document.cursor,
        [StoredOp(seq=row.seq, kind=row.kind, payload=_parse_payload(row.payload), created_at=row.created_at) for row in rows],
    )


def append_op(session: Session, document: DocumentModel, request: OpRequest) -> OpResult:
    state = build_state(session, document)
    next_state = state.clone()
    apply_state_op(next_state, request.kind, request.payload)

    max_seq = session.exec(select(func.max(OpModel.seq)).where(OpModel.document_id == document.id)).one() or 0

    if document.cursor < max_seq:
        session.exec(
            delete(OpModel)
            .where(OpModel.document_id == document.id)
            .where(OpModel.seq > document.cursor)
        )
        max_seq = document.cursor

    new_seq = document.cursor + 1
    session.add(
        OpModel(
            document_id=document.id,
            seq=new_seq,
            kind=request.kind,
            payload=json.dumps(request.payload, sort_keys=True),
            created_at=_now(),
        )
    )

    document.cursor = new_seq
    document.version = new_seq
    document.page_count = len(next_state.order)
    document.updated_at = _now()
    session.add(document)
    session.commit()

    can_undo, can_redo = _calc_flags(document.cursor, new_seq)
    return OpResult(
        cursor=document.cursor,
        canUndo=can_undo,
        canRedo=can_redo,
        pageCount=document.page_count,
        version=document.version,
    )


def undo(session: Session, document: DocumentModel) -> OpResult:
    max_seq = session.exec(select(func.max(OpModel.seq)).where(OpModel.document_id == document.id)).one() or 0
    applied = latest_applied_op(session, document)
    if applied and applied.kind == "scan.apply":
        raise OpValidationError("OP_NOT_APPLICABLE", "Undo is disabled after in-place scan.")

    if document.cursor > 0:
        document.cursor -= 1
        document.version = document.cursor
        state = build_state(session, document)
        document.page_count = len(state.order)
        document.updated_at = _now()
        session.add(document)
        session.commit()

    can_undo, can_redo = _calc_flags(document.cursor, max_seq)
    return OpResult(
        cursor=document.cursor,
        canUndo=can_undo,
        canRedo=can_redo,
        pageCount=document.page_count,
        version=document.version,
    )


def redo(session: Session, document: DocumentModel) -> OpResult:
    max_seq = session.exec(select(func.max(OpModel.seq)).where(OpModel.document_id == document.id)).one() or 0
    applied = latest_applied_op(session, document)
    if applied and applied.kind == "scan.apply":
        raise OpValidationError("OP_NOT_APPLICABLE", "Redo is disabled after in-place scan.")

    if document.cursor < max_seq:
        next_op = session.exec(
            select(OpModel)
            .where(OpModel.document_id == document.id)
            .where(OpModel.seq == document.cursor + 1)
        ).first()
        if next_op and next_op.kind == "scan.apply":
            raise OpValidationError("OP_NOT_APPLICABLE", "Redo is disabled at scan boundary.")

        document.cursor += 1
        document.version = document.cursor
        state = build_state(session, document)
        document.page_count = len(state.order)
        document.updated_at = _now()
        session.add(document)
        session.commit()

    can_undo, can_redo = _calc_flags(document.cursor, max_seq)
    return OpResult(
        cursor=document.cursor,
        canUndo=can_undo,
        canRedo=can_redo,
        pageCount=document.page_count,
        version=document.version,
    )


def resolve_pdf_path(session: Session, document: DocumentModel, requested_version: int | None) -> Path:
    settings = get_settings()
    version = document.cursor if requested_version is None else requested_version
    if version < 0 or version > document.cursor:
        raise OpValidationError("OP_NOT_APPLICABLE", "Requested version is out of range.")

    if version == 0:
        return original_pdf_path(settings.store_root, document.id)

    path = derived_pdf_path(settings.store_root, document.id, version)
    if path.exists():
        return path

    return build_derived_pdf(session, document, version)


def apply_pdf_op(doc: pymupdf.Document, state: EditorState, kind: str, payload: dict[str, Any]) -> None:
    if kind == "page.rotate":
        for page_uuid in payload.get("pages", []):
            index = state.order.index(page_uuid)
            page = doc[index]
            delta = int(payload.get("delta", 0))
            page.set_rotation((page.rotation + delta) % 360)
        apply_state_op(state, kind, payload)
        return

    if kind == "page.delete":
        indices = sorted((state.order.index(page_uuid) for page_uuid in payload.get("pages", [])), reverse=True)
        for index in indices:
            doc.delete_page(index)
        apply_state_op(state, kind, payload)
        return

    if kind == "page.reorder":
        order = payload.get("order", [])
        indices = [state.order.index(page_uuid) for page_uuid in order]
        doc.select(indices)
        apply_state_op(state, kind, payload)
        return

    if kind == "scan.apply":
        apply_state_op(state, kind, payload)
        return

    apply_state_op(state, kind, payload)


def build_derived_pdf(session: Session, document: DocumentModel, version: int) -> Path:
    settings = get_settings()
    src_path = original_pdf_path(settings.store_root, document.id)
    output_path = derived_pdf_path(settings.store_root, document.id, version)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(src_path)
    state = load_base_state(session, document.id)
    for op in load_ops(session, document.id):
        if op.seq > version:
            break
        apply_pdf_op(doc, state, op.kind, _parse_payload(op.payload))

    doc.save(output_path, garbage=4, deflate=True, clean=True)
    doc.close()
    return output_path
