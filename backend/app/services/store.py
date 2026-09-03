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


def derived_dir(root: Path, doc_id: str) -> Path:
    return safe_path(root, "documents", doc_id, "derived")


def derived_pdf_path(root: Path, doc_id: str, version: int) -> Path:
    return safe_path(root, "documents", doc_id, "derived", f"{version}.pdf")


def thumbs_dir(root: Path, doc_id: str, version: int) -> Path:
    return safe_path(root, "documents", doc_id, "thumbs", str(version))


def thumb_path(root: Path, doc_id: str, version: int, page_uuid: str) -> Path:
    return safe_path(root, "documents", doc_id, "thumbs", str(version), f"{page_uuid}.webp")


def assets_dir(root: Path, doc_id: str) -> Path:
    return safe_path(root, "documents", doc_id, "assets")
