from pathlib import Path


def safe_path(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    candidate = (root / Path(*parts)).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError("invalid path")
    return candidate


def ensure_store_layout(root: Path) -> None:
    safe_path(root).mkdir(parents=True, exist_ok=True)
    safe_path(root, "documents").mkdir(parents=True, exist_ok=True)
    safe_path(root, "tmp").mkdir(parents=True, exist_ok=True)


def document_dir(root: Path, doc_id: str) -> Path:
    return safe_path(root, "documents", doc_id)


def original_pdf_path(root: Path, doc_id: str) -> Path:
    return safe_path(root, "documents", doc_id, "original.pdf")
