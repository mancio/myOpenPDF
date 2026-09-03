import os
from pathlib import Path


def _normalized_path(path: Path) -> str:
    text = os.path.normcase(str(path))
    if os.name == "nt" and text.startswith("\\\\?\\"):
        text = text[4:]
    return text.rstrip("\\/")


def safe_path(root: Path, *parts: str) -> Path:
    resolved_root = root.expanduser().resolve(strict=False)
    candidate = resolved_root.joinpath(*parts).resolve(strict=False)

    root_norm = _normalized_path(resolved_root)
    candidate_norm = _normalized_path(candidate)
    if candidate_norm != root_norm and not candidate_norm.startswith(f"{root_norm}{os.sep}"):
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
