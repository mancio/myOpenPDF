from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.db import get_session
from app.errors import api_error
from app.models import DocumentModel
from app.schemas import OpLogResponse, OpRequest, OpResult
from app.services.oplog import (
    OpValidationError,
    append_op,
    list_ops_response,
    redo,
    undo,
)

router = APIRouter(prefix="/documents", tags=["ops"])


@router.post("/{document_id}/ops", response_model=OpResult)
def create_op(document_id: str, request: OpRequest, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        return append_op(session, document, request)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)


@router.get("/{document_id}/ops", response_model=OpLogResponse)
def get_ops(document_id: str, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    cursor, ops = list_ops_response(session, document)
    return OpLogResponse(cursor=cursor, ops=ops)


@router.post("/{document_id}/undo", response_model=OpResult)
def undo_document(document_id: str, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        return undo(session, document)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)


@router.post("/{document_id}/redo", response_model=OpResult)
def redo_document(document_id: str, session: Session = Depends(get_session)):
    document = session.get(DocumentModel, document_id)
    if not document:
        return api_error(404, "NOT_FOUND", "Document not found.")

    try:
        return redo(session, document)
    except OpValidationError as error:
        return api_error(409, error.code, error.message)
