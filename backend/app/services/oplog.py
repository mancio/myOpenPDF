import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymupdf
from sqlmodel import Session, delete, func, select

from app.config import get_settings
from app.models import DocumentModel, OpModel, PageRefModel
from app.scan.pipeline import export_document_bytes
from app.schemas import AnnotationPayload, OpRequest, OpResult, ScanParams, StoredOp
from app.services.locks import keyed_lock
from app.services.store import assets_dir, derived_pdf_path, original_pdf_path

SUPPORTED_OPS = {
    "page.rotate",
    "page.delete",
    "page.reorder",
    "page.duplicate",
    "page.insert_blank",
    "page.import",
    "annot.add",
    "annot.update",
    "annot.delete",
    "form.set",
    "redact.apply",
    "text.replace",
    "scan.apply",
    "doc.flatten",
}

TEXT_SURFACE_OPS = {
    "annot.add",
    "annot.update",
    "annot.delete",
    "form.set",
    "redact.apply",
    "text.replace",
}

TERMINAL_OPS = {"scan.apply", "doc.flatten"}


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


def _after_position(order: list[str], after_page: str | None) -> int:
    if after_page is None:
        return len(order)
    if after_page not in order:
        raise OpValidationError("PAGE_NOT_FOUND", f"Page not found: {after_page}")
    return order.index(after_page) + 1


def _require_page(state: EditorState, page_uuid: str) -> None:
    if page_uuid not in state.order:
        raise OpValidationError("PAGE_NOT_FOUND", f"Page not found: {page_uuid}")


def _annotation_marker(annotation_id: str) -> str:
    return f"aid:{annotation_id}"


def _find_annotation(doc: pymupdf.Document, annotation_id: str) -> tuple[pymupdf.Page, pymupdf.Annot] | None:
    marker = _annotation_marker(annotation_id)
    for page_index in range(doc.page_count):
        page = doc[page_index]
        annots = page.annots()
        if not annots:
            continue
        for annot in annots:
            info = annot.info or {}
            if info.get("content") == marker:
                return page, annot
    return None


def load_ops(session: Session, document_id: str) -> list[OpModel]:
    return session.exec(
        select(OpModel)
        .where(OpModel.document_id == document_id)
        .order_by(OpModel.seq.asc())
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
        raise OpValidationError(
            "UNSUPPORTED_FEATURE",
            f"Operation kind '{kind}' is not implemented yet.",
        )

    if kind == "page.rotate":
        pages = payload.get("pages")
        delta = payload.get("delta")
        if not isinstance(pages, list) or not pages:
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.rotate requires non-empty pages list.",
            )
        if delta not in (90, 180, 270):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.rotate delta must be 90, 180 or 270.",
            )
        for page_uuid in pages:
            _require_page(state, page_uuid)
            current = state.rotations.get(page_uuid, 0)
            state.rotations[page_uuid] = (current + int(delta)) % 360
        return

    if kind == "page.delete":
        pages = payload.get("pages")
        if not isinstance(pages, list) or not pages:
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.delete requires non-empty pages list.",
            )
        for page_uuid in pages:
            _require_page(state, page_uuid)
        to_delete = set(pages)
        if len(state.order) - len(to_delete) < 1:
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "Cannot delete all pages. At least one page must remain.",
            )
        state.order = [item for item in state.order if item not in to_delete]
        return

    if kind == "page.reorder":
        order = payload.get("order")
        if not isinstance(order, list) or not order:
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.reorder requires an order list.",
            )
        if len(order) != len(state.order):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.reorder order length mismatch.",
            )
        if set(order) != set(state.order):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.reorder must contain the same page UUIDs.",
            )
        state.order = [str(item) for item in order]
        return

    if kind == "page.duplicate":
        src = payload.get("page")
        new_uuid = payload.get("newUuid")
        after_page = payload.get("after")
        if not isinstance(src, str) or not isinstance(new_uuid, str):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.duplicate requires page and newUuid.",
            )
        _require_page(state, src)
        if new_uuid in state.order:
            raise OpValidationError("OP_NOT_APPLICABLE", "newUuid already exists.")
        position = _after_position(state.order, after_page if isinstance(after_page, str) else src)
        state.order.insert(position, new_uuid)
        return

    if kind == "page.insert_blank":
        new_uuid = payload.get("newUuid")
        after_page = payload.get("after")
        if not isinstance(new_uuid, str):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.insert_blank requires newUuid.",
            )
        if new_uuid in state.order:
            raise OpValidationError("OP_NOT_APPLICABLE", "newUuid already exists.")
        position = _after_position(state.order, after_page if isinstance(after_page, str) else None)
        state.order.insert(position, new_uuid)
        return

    if kind == "page.import":
        after_page = payload.get("after")
        new_uuids = payload.get("newUuids")
        if not isinstance(new_uuids, list) or not new_uuids:
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "page.import requires non-empty newUuids.",
            )
        if any(item in state.order for item in new_uuids):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "At least one newUuids value already exists.",
            )
        position = _after_position(state.order, after_page if isinstance(after_page, str) else None)
        for idx, new_uuid in enumerate(new_uuids):
            state.order.insert(position + idx, str(new_uuid))
        return

    if kind in {"annot.add", "annot.update"}:
        annot = payload.get("annot")
        if not isinstance(annot, dict):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                f"{kind} requires annot object.",
            )
        page_uuid = annot.get("page")
        if not isinstance(page_uuid, str):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                f"{kind} requires annot.page.",
            )
        _require_page(state, page_uuid)
        return

    if kind == "annot.delete":
        if not isinstance(payload.get("id"), str):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "annot.delete requires id.",
            )
        return

    if kind in {"form.set", "redact.apply", "text.replace"}:
        page_uuid = payload.get("page")
        if not isinstance(page_uuid, str):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                f"{kind} requires page.",
            )
        _require_page(state, page_uuid)
        return

    if kind == "scan.apply":
        params = payload.get("params")
        if not isinstance(params, dict):
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "scan.apply requires params object.",
            )
        return

    if kind == "doc.flatten":
        return


def build_state(
    session: Session,
    document: DocumentModel,
    upto: int | None = None,
) -> EditorState:
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
        [
            StoredOp(
                seq=row.seq,
                kind=row.kind,
                payload=_parse_payload(row.payload),
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


def list_annotations(session: Session, document: DocumentModel) -> list[AnnotationPayload]:
    active: dict[str, AnnotationPayload] = {}

    for row in load_ops(session, document.id):
        if row.seq > document.cursor:
            break
        payload = _parse_payload(row.payload)

        if row.kind in {"annot.add", "annot.update"}:
            annot_data = payload.get("annot")
            if not isinstance(annot_data, dict):
                continue
            try:
                annotation = AnnotationPayload.model_validate(annot_data)
            except ValueError:
                continue
            active[annotation.id] = annotation
        elif row.kind == "annot.delete":
            annotation_id = payload.get("id")
            if isinstance(annotation_id, str):
                active.pop(annotation_id, None)

    return list(active.values())


def _has_terminal_before_cursor(session: Session, document: DocumentModel) -> bool:
    rows = load_ops(session, document.id)
    for row in rows:
        if row.seq > document.cursor:
            break
        if row.kind in TERMINAL_OPS:
            return True
    return False


def append_op(session: Session, document: DocumentModel, request: OpRequest) -> OpResult:
    if _has_terminal_before_cursor(session, document) and request.kind in TEXT_SURFACE_OPS:
        raise OpValidationError(
            "OP_NOT_APPLICABLE",
            "Text or annotation edits are disabled after scan/flatten ops.",
        )

    payload = dict(request.payload)
    if request.kind == "scan.apply":
        params = payload.get("params")
        if not isinstance(params, dict):
            raise OpValidationError("OP_NOT_APPLICABLE", "scan.apply requires params.")
        if params.get("seed") is None:
            params = dict(params)
            params["seed"] = int(uuid4().int & 0x7FFFFFFF)
            payload["params"] = params

    state = build_state(session, document)
    next_state = state.clone()
    apply_state_op(next_state, request.kind, payload)

    max_seq = session.exec(
        select(func.max(OpModel.seq)).where(OpModel.document_id == document.id)
    ).one() or 0

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
            payload=json.dumps(payload, sort_keys=True),
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
    max_seq = session.exec(
        select(func.max(OpModel.seq)).where(OpModel.document_id == document.id)
    ).one() or 0
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
    max_seq = session.exec(
        select(func.max(OpModel.seq)).where(OpModel.document_id == document.id)
    ).one() or 0
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


def resolve_pdf_path(
    session: Session,
    document: DocumentModel,
    requested_version: int | None,
) -> Path:
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


def _delete_annotation(doc: pymupdf.Document, annotation_id: str) -> None:
    found = _find_annotation(doc, annotation_id)
    if not found:
        return
    page, annot = found
    page.delete_annot(annot)


def _add_or_update_annotation(
    doc: pymupdf.Document,
    state: EditorState,
    payload: dict[str, Any],
    document_id: str,
) -> None:
    annot = payload.get("annot", {})
    annotation_id = str(annot.get("id"))
    if not annotation_id:
        raise OpValidationError("OP_NOT_APPLICABLE", "Annotation id is required.")

    _delete_annotation(doc, annotation_id)

    page_uuid = str(annot.get("page"))
    _require_page(state, page_uuid)
    page = doc[state.order.index(page_uuid)]
    kind = str(annot.get("kind"))
    rect_value = annot.get("rect")
    color = annot.get("color") or (0, 0, 0)
    fill = annot.get("fill")
    opacity = float(annot.get("opacity", 1.0))
    width = float(annot.get("width", 1.0))
    text = str(annot.get("text") or "")
    points_value = annot.get("points")

    rect = None
    if isinstance(rect_value, (list, tuple)) and len(rect_value) == 4:
        rect = pymupdf.Rect(
            float(rect_value[0]),
            float(rect_value[1]),
            float(rect_value[2]),
            float(rect_value[3]),
        )

    created = None
    if kind == "note":
        if rect is None:
            rect = pymupdf.Rect(40, 40, 200, 90)
        created = page.add_text_annot(rect.tl, text or "Note")
    elif kind == "freetext":
        if rect is None:
            rect = pymupdf.Rect(40, 40, 260, 120)
        created = page.add_freetext_annot(rect, text or "Text", fontsize=11, fontname="helv")
    elif kind == "signature":
        if rect is None:
            rect = pymupdf.Rect(40, 40, 260, 105)
        created = page.add_freetext_annot(rect, text or "Signature", fontsize=18, fontname="helv")
        width = 0.0
    elif kind == "highlight":
        if rect is None:
            raise OpValidationError("OP_NOT_APPLICABLE", "highlight requires rect")
        created = page.add_highlight_annot(rect)
    elif kind == "underline":
        if rect is None:
            raise OpValidationError("OP_NOT_APPLICABLE", "underline requires rect")
        created = page.add_underline_annot(rect)
    elif kind == "strikeout":
        if rect is None:
            raise OpValidationError("OP_NOT_APPLICABLE", "strikeout requires rect")
        created = page.add_strikeout_annot(rect)
    elif kind == "ellipse":
        if rect is None:
            raise OpValidationError("OP_NOT_APPLICABLE", "ellipse requires rect")
        created = page.add_circle_annot(rect)
    elif kind == "line":
        if rect is None:
            raise OpValidationError("OP_NOT_APPLICABLE", "line requires rect")
        created = page.add_line_annot(rect.tl, rect.br)
    elif kind == "ink":
        if not isinstance(points_value, list) or len(points_value) < 2:
            raise OpValidationError("OP_NOT_APPLICABLE", "ink requires at least two points")
        stroke: list[tuple[float, float]] = []
        for pair in points_value:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise OpValidationError("OP_NOT_APPLICABLE", "ink points must be [x, y]")
            stroke.append((float(pair[0]), float(pair[1])))
        created = page.add_ink_annot([stroke])
    elif kind in {"image", "stamp"}:
        asset_id = annot.get("asset_id")
        if not isinstance(asset_id, str):
            raise OpValidationError("OP_NOT_APPLICABLE", "image/stamp requires asset_id")
        if rect is None:
            rect = pymupdf.Rect(40, 40, 220, 140)
        image_path = assets_dir(get_settings().store_root, document_id) / asset_id
        if not image_path.exists():
            raise OpValidationError("NOT_FOUND", "Asset not found for annotation image.")
        page.insert_image(rect, filename=str(image_path), keep_proportion=True, overlay=True)
        return
    else:
        if rect is None:
            raise OpValidationError("OP_NOT_APPLICABLE", "rect-based annotation requires rect")
        created = page.add_rect_annot(rect)

    if created is not None:
        can_set_colors = kind not in {"freetext", "signature"}
        if can_set_colors and isinstance(color, (list, tuple)) and len(color) == 3:
            created.set_colors(stroke=tuple(float(x) for x in color))
        if isinstance(fill, (list, tuple)) and len(fill) == 3:
            created.set_colors(fill=tuple(float(x) for x in fill))
        created.set_opacity(opacity)
        created.set_border(width=width)
        created.set_info(content=_annotation_marker(annotation_id))
        created.update()


def _set_form_value(doc: pymupdf.Document, state: EditorState, payload: dict[str, Any]) -> None:
    page_uuid = str(payload.get("page"))
    field_name = str(payload.get("field"))
    value = payload.get("value")

    _require_page(state, page_uuid)
    page = doc[state.order.index(page_uuid)]
    widgets = page.widgets()
    if not widgets:
        raise OpValidationError("FIELD_NOT_FOUND", "No form fields on target page.")

    found = False
    for widget in widgets:
        if widget.field_name == field_name:
            widget.field_value = value
            widget.update()
            found = True
            break

    if not found:
        raise OpValidationError("FIELD_NOT_FOUND", f"Field not found: {field_name}")


def _apply_redaction(doc: pymupdf.Document, state: EditorState, payload: dict[str, Any]) -> None:
    page_uuid = str(payload.get("page"))
    rects = payload.get("rects") or []
    fill = payload.get("fill")

    _require_page(state, page_uuid)
    page = doc[state.order.index(page_uuid)]

    if not rects:
        raise OpValidationError("OP_NOT_APPLICABLE", "redact.apply requires rects.")

    for rect_values in rects:
        rect = pymupdf.Rect(
            float(rect_values[0]),
            float(rect_values[1]),
            float(rect_values[2]),
            float(rect_values[3]),
        )
        if isinstance(fill, (list, tuple)) and len(fill) == 3:
            page.add_redact_annot(rect, fill=tuple(float(x) for x in fill))
        else:
            page.add_redact_annot(rect)

    page.apply_redactions(
        images=getattr(pymupdf, "PDF_REDACT_IMAGE_PIXELS", 2),
        graphics=getattr(pymupdf, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1),
        text=getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0),
    )


def _apply_text_replace(doc: pymupdf.Document, state: EditorState, payload: dict[str, Any]) -> None:
    page_uuid = str(payload.get("page"))
    old_text = str(payload.get("old") or "")
    new_text = str(payload.get("new") or "")
    rect_filter = payload.get("rect")

    _require_page(state, page_uuid)
    page = doc[state.order.index(page_uuid)]

    hits = page.search_for(old_text)
    if not hits:
        raise OpValidationError("TEXT_NOT_FOUND", "No text span matched for replace.")

    target = hits[0]
    if isinstance(rect_filter, (list, tuple)) and len(rect_filter) == 4:
        wanted = pymupdf.Rect(
            float(rect_filter[0]),
            float(rect_filter[1]),
            float(rect_filter[2]),
            float(rect_filter[3]),
        )
        matching = [hit for hit in hits if hit.intersects(wanted)]
        if not matching:
            raise OpValidationError("TEXT_NOT_FOUND", "Target rectangle has no matching text span.")
        target = matching[0]

    page.add_redact_annot(target, fill=None)
    page.apply_redactions(
        images=getattr(pymupdf, "PDF_REDACT_IMAGE_PIXELS", 2),
        graphics=getattr(pymupdf, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1),
        text=getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0),
    )

    if page.insert_textbox(target, new_text, fontname="helv", fontsize=11, color=(0, 0, 0)) < 0:
        if page.insert_textbox(
            target,
            new_text,
            fontname="helv",
            fontsize=10.45,
            color=(0, 0, 0),
        ) < 0:
            raise OpValidationError("UNSUPPORTED_FEATURE", "Replacement text does not fit target span.")


def apply_pdf_op(
    doc: pymupdf.Document,
    state: EditorState,
    kind: str,
    payload: dict[str, Any],
    document_id: str,
) -> pymupdf.Document:
    if kind == "page.rotate":
        for page_uuid in payload.get("pages", []):
            index = state.order.index(page_uuid)
            page = doc[index]
            delta = int(payload.get("delta", 0))
            page.set_rotation((page.rotation + delta) % 360)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "page.delete":
        indices = sorted({state.order.index(page_uuid) for page_uuid in payload.get("pages", [])}, reverse=True)
        if len(indices) >= doc.page_count:
            raise OpValidationError(
                "OP_NOT_APPLICABLE",
                "Cannot delete all pages. At least one page must remain.",
            )
        for index in indices:
            doc.delete_page(index)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "page.reorder":
        order = payload.get("order", [])
        indices = [state.order.index(page_uuid) for page_uuid in order]
        doc.select(indices)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "page.duplicate":
        src_uuid = str(payload.get("page"))
        source_index = state.order.index(src_uuid)
        after_page = payload.get("after")
        insertion_point = _after_position(state.order, after_page if isinstance(after_page, str) else src_uuid)

        temp = pymupdf.open()
        temp.insert_pdf(doc, from_page=source_index, to_page=source_index)
        doc.insert_pdf(temp, from_page=0, to_page=0, start_at=insertion_point)
        temp.close()

        apply_state_op(state, kind, payload)
        return doc

    if kind == "page.insert_blank":
        after_page = payload.get("after")
        insertion_point = _after_position(state.order, after_page if isinstance(after_page, str) else None)
        width = float(payload.get("width", 595))
        height = float(payload.get("height", 842))
        doc.new_page(pno=insertion_point, width=width, height=height)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "page.import":
        asset_id = payload.get("assetId")
        if not isinstance(asset_id, str):
            raise OpValidationError("OP_NOT_APPLICABLE", "page.import requires assetId.")

        asset_path = assets_dir(get_settings().store_root, document_id) / asset_id
        if not asset_path.exists():
            raise OpValidationError("NOT_FOUND", "Import asset not found.")

        source = pymupdf.open(asset_path)
        try:
            pages = payload.get("pages")
            from_page = 0
            to_page = source.page_count - 1
            if isinstance(pages, (list, tuple)) and len(pages) == 2:
                from_page = max(0, int(pages[0]))
                to_page = min(source.page_count - 1, int(pages[1]))
            insert_after = payload.get("after")
            insertion_point = _after_position(state.order, insert_after if isinstance(insert_after, str) else None)
            doc.insert_pdf(source, from_page=from_page, to_page=to_page, start_at=insertion_point)
        finally:
            source.close()

        apply_state_op(state, kind, payload)
        return doc

    if kind in {"annot.add", "annot.update"}:
        _add_or_update_annotation(doc, state, payload, document_id)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "annot.delete":
        annotation_id = str(payload.get("id"))
        _delete_annotation(doc, annotation_id)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "form.set":
        _set_form_value(doc, state, payload)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "redact.apply":
        _apply_redaction(doc, state, payload)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "text.replace":
        _apply_text_replace(doc, state, payload)
        apply_state_op(state, kind, payload)
        return doc

    if kind == "doc.flatten":
        doc.bake(
            annots=bool(payload.get("annots", True)),
            widgets=bool(payload.get("widgets", True)),
        )
        apply_state_op(state, kind, payload)
        return doc

    apply_state_op(state, kind, payload)
    return doc


def build_derived_pdf(session: Session, document: DocumentModel, version: int) -> Path:
    settings = get_settings()
    output_path = derived_pdf_path(settings.store_root, document.id, version)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with keyed_lock(f"derived:{document.id}:{version}"):
        if output_path.exists():
            return output_path

        src_path = original_pdf_path(settings.store_root, document.id)
        temp_path = output_path.with_suffix(f"{output_path.suffix}.{uuid4().hex}.tmp")
        doc = pymupdf.open(src_path)
        state = load_base_state(session, document.id)

        try:
            for op in load_ops(session, document.id):
                if op.seq > version:
                    break
                payload = _parse_payload(op.payload)
                if op.kind == "scan.apply":
                    params = ScanParams.model_validate(payload.get("params", {}))
                    seed = params.seed if params.seed is not None else int(uuid4().int & 0x7FFFFFFF)
                    scanned_bytes = export_document_bytes(doc, params, seed)
                    doc.close()
                    doc = pymupdf.open(stream=scanned_bytes, filetype="pdf")
                    apply_state_op(state, op.kind, payload)
                else:
                    doc = apply_pdf_op(doc, state, op.kind, payload, document.id)

            try:
                doc.save(temp_path, garbage=4, deflate=True, clean=True)
            except ValueError as error:
                if "zero pages" in str(error).lower():
                    raise OpValidationError(
                        "OP_NOT_APPLICABLE",
                        "Document has no pages after applied operations.",
                    ) from error
                raise
            temp_path.replace(output_path)
        finally:
            doc.close()
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    return output_path
