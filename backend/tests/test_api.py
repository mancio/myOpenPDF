import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pymupdf


def _sample_pdf_bytes(page_count: int = 1) -> bytes:
    doc = pymupdf.open()
    for index in range(page_count):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 120), f"Hello PDF Studio #{index + 1}")
        page.insert_text((72, 150), "Searchable marker line")
    data = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return data


def _create_document(client, page_count: int = 1) -> dict:
    files = {"file": ("sample.pdf", _sample_pdf_bytes(page_count), "application/pdf")}
    response = client.post("/api/documents", files=files, data={"title": "Sample"})
    assert response.status_code == 200
    return response.json()


def _wait_for_job_terminal(client, job_id: str, timeout_seconds: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"done", "error", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach a terminal state in time")


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_document_crud_and_viewer_endpoints(client):
    doc = _create_document(client)
    doc_id = doc["id"]

    response = client.get("/api/documents")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1

    response = client.get(f"/api/documents/{doc_id}")
    assert response.status_code == 200
    assert response.json()["id"] == doc_id

    response = client.get(f"/api/documents/{doc_id}/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")

    response = client.get(f"/api/documents/{doc_id}/pages")
    assert response.status_code == 200
    pages = response.json()
    assert len(pages) == 1
    page_uuid = pages[0]["uuid"]

    response = client.get(f"/api/documents/{doc_id}/pages/{page_uuid}/thumb")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/webp")

    response = client.get(f"/api/documents/{doc_id}/pages/{page_uuid}/text")
    assert response.status_code == 200
    text_blocks = response.json()
    assert any("Hello PDF Studio" in item["text"] for item in text_blocks)

    response = client.post(f"/api/documents/{doc_id}/search", json={"query": "marker", "caseSensitive": False})
    assert response.status_code == 200
    hits = response.json()
    assert len(hits) >= 1

    response = client.patch(f"/api/documents/{doc_id}", json={"title": "Renamed"})
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"

    response = client.delete(f"/api/documents/{doc_id}")
    assert response.status_code == 204

    response = client.get(f"/api/documents/{doc_id}")
    assert response.status_code == 404


def test_oplog_rotate_undo_redo(client):
    doc = _create_document(client)
    doc_id = doc["id"]

    pages_response = client.get(f"/api/documents/{doc_id}/pages")
    page_uuid = pages_response.json()[0]["uuid"]

    op = {"kind": "page.rotate", "payload": {"pages": [page_uuid], "delta": 90}}
    response = client.post(f"/api/documents/{doc_id}/ops", json=op)
    assert response.status_code == 200
    result = response.json()
    assert result["cursor"] == 1

    response = client.get(f"/api/documents/{doc_id}/file?version=1")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")

    response = client.post(f"/api/documents/{doc_id}/undo")
    assert response.status_code == 200
    assert response.json()["cursor"] == 0

    response = client.post(f"/api/documents/{doc_id}/redo")
    assert response.status_code == 200
    assert response.json()["cursor"] == 1


def test_reject_delete_last_page(client):
    doc = _create_document(client, page_count=1)
    doc_id = doc["id"]

    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    only_page = pages[0]["uuid"]

    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={"kind": "page.delete", "payload": {"pages": [only_page]}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OP_NOT_APPLICABLE"

    pages_after = client.get(f"/api/documents/{doc_id}/pages")
    assert pages_after.status_code == 200
    assert len(pages_after.json()) == 1


def test_annotation_ink_and_signature_ops(client):
    doc = _create_document(client, page_count=1)
    doc_id = doc["id"]
    page_uuid = client.get(f"/api/documents/{doc_id}/pages").json()[0]["uuid"]

    ink_id = str(uuid4())
    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "annot.add",
            "payload": {
                "annot": {
                    "id": ink_id,
                    "page": page_uuid,
                    "kind": "ink",
                    "rect": [72, 120, 180, 150],
                    "points": [[72, 126], [108, 132], [140, 128], [178, 146]],
                    "color": [0.08, 0.33, 0.75],
                    "width": 2.4,
                }
            },
        },
    )
    assert response.status_code == 200

    signature_id = str(uuid4())
    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "annot.add",
            "payload": {
                "annot": {
                    "id": signature_id,
                    "page": page_uuid,
                    "kind": "signature",
                    "rect": [72, 200, 290, 250],
                    "text": "Andrea M.",
                    "color": [0.06, 0.08, 0.21],
                    "width": 0,
                }
            },
        },
    )
    assert response.status_code == 200

    annotations = client.get(f"/api/documents/{doc_id}/annotations")
    assert annotations.status_code == 200
    payload = annotations.json()
    by_id = {item["id"]: item for item in payload}
    assert by_id[ink_id]["kind"] == "ink"
    assert len(by_id[ink_id]["points"]) >= 2
    assert by_id[signature_id]["kind"] == "signature"

    file_response = client.get(f"/api/documents/{doc_id}/file?version=2")
    assert file_response.status_code == 200
    assert file_response.content.startswith(b"%PDF-")


def test_concurrent_derived_and_thumbnail_requests(client):
    doc = _create_document(client, page_count=2)
    doc_id = doc["id"]

    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    page_uuid = pages[0]["uuid"]

    rotate = client.post(
        f"/api/documents/{doc_id}/ops",
        json={"kind": "page.rotate", "payload": {"pages": [page_uuid], "delta": 90}},
    )
    assert rotate.status_code == 200
    version = rotate.json()["version"]

    def request_pdf() -> tuple[int, bytes]:
        response = client.get(f"/api/documents/{doc_id}/file?version={version}")
        return response.status_code, response.content[:5]

    def request_thumb() -> tuple[int, str]:
        response = client.get(f"/api/documents/{doc_id}/pages/{page_uuid}/thumb?version={version}&dpi=110")
        return response.status_code, response.headers.get("content-type", "")

    with ThreadPoolExecutor(max_workers=10) as executor:
        file_futures = [executor.submit(request_pdf) for _ in range(10)]
        thumb_futures = [executor.submit(request_thumb) for _ in range(10)]

    for future in file_futures:
        status, head = future.result()
        assert status == 200
        assert head == b"%PDF-"

    for future in thumb_futures:
        status, content_type = future.result()
        assert status == 200
        assert content_type.startswith("image/webp")


def test_scan_preview_and_export_job(client):
    doc = _create_document(client)
    doc_id = doc["id"]
    page_uuid = client.get(f"/api/documents/{doc_id}/pages").json()[0]["uuid"]

    preview_payload = {
        "pageUuid": page_uuid,
        "previewDpi": 110,
        "params": {
            "dpi": 200,
            "color_mode": "gray",
            "paper_tint": "#FFFFFF",
            "gamma": 1.0,
            "brightness": 1.0,
            "contrast": 1.1,
            "jitter": 0.03,
            "blur_sigma": 0.2,
            "noise_sigma": 4,
            "noise_mono": True,
            "bw_threshold": 128,
            "bw_dither": True,
            "jpeg_quality": 80,
            "downsample": 1.0,
        },
    }

    response = client.post(f"/api/documents/{doc_id}/scan/preview", json=preview_payload)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/webp")

    run_payload = {"mode": "export", "params": preview_payload["params"]}
    response = client.post(f"/api/documents/{doc_id}/scan", json=run_payload)
    assert response.status_code == 200
    created = response.json()
    assert created["status"] in {"queued", "running", "done"}
    job = _wait_for_job_terminal(client, created["id"])
    assert job["status"] in {"done", "error"}

    if job["status"] == "done":
        result = client.get(f"/api/jobs/{job['id']}/result")
        assert result.status_code == 200
        assert result.headers["content-type"] == "application/pdf"
        assert result.content.startswith(b"%PDF-")


def test_compress_endpoint(client):
    doc = _create_document(client)
    doc_id = doc["id"]

    payload = {
        "profile": "balanced",
        "stripMetadata": True,
        "imageDpi": 200,
    }
    response = client.post(f"/api/documents/{doc_id}/compress", json=payload)
    assert response.status_code == 200
    created = response.json()
    assert created["kind"] == "compress"
    assert created["status"] in {"queued", "running", "done"}
    job = _wait_for_job_terminal(client, created["id"])
    assert job["status"] in {"done", "error"}

    if job["status"] == "done":
        result = client.get(f"/api/jobs/{job['id']}/result")
        assert result.status_code == 200
        assert result.headers["content-type"] == "application/pdf"
        assert result.content.startswith(b"%PDF-")


def test_compress_endpoint_rejects_high_dpi(client):
    doc = _create_document(client)
    doc_id = doc["id"]

    payload = {
        "profile": "strong",
        "stripMetadata": True,
        "imageDpi": 999,
    }
    response = client.post(f"/api/documents/{doc_id}/compress", json=payload)
    assert response.status_code == 422


def test_compress_estimate_endpoint(client):
    doc = _create_document(client)
    doc_id = doc["id"]

    payload = {
        "profile": "balanced",
        "stripMetadata": True,
        "imageDpi": 200,
    }
    response = client.post(f"/api/documents/{doc_id}/compress/estimate", json=payload)
    assert response.status_code == 200

    estimate = response.json()
    assert estimate["profile"] == "balanced"
    assert estimate["sourceBytes"] > 0
    assert estimate["estimatedBytes"] > 0
    assert isinstance(estimate["estimatedReductionPercent"], float)


def test_page_manager_ops_and_scan_in_place_boundary(client):
    doc = _create_document(client, page_count=2)
    doc_id = doc["id"]

    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    first = pages[0]["uuid"]
    second = pages[1]["uuid"]

    duplicate_uuid = str(uuid4())
    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "page.duplicate",
            "payload": {"page": first, "newUuid": duplicate_uuid, "after": first},
        },
    )
    assert response.status_code == 200

    insert_uuid = str(uuid4())
    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "page.insert_blank",
            "payload": {
                "newUuid": insert_uuid,
                "after": second,
                "width": 595,
                "height": 842,
            },
        },
    )
    assert response.status_code == 200

    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    assert len(pages) == 4

    reversed_order = [page["uuid"] for page in reversed(pages)]
    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={"kind": "page.reorder", "payload": {"order": reversed_order}},
    )
    assert response.status_code == 200

    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={"kind": "page.delete", "payload": {"pages": [insert_uuid]}},
    )
    assert response.status_code == 200

    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    assert len(pages) == 3

    scan_payload = {
        "mode": "in_place",
        "params": {
            "dpi": 200,
            "color_mode": "gray",
            "paper_tint": "#FFFFFF",
            "gamma": 1.0,
            "brightness": 1.0,
            "contrast": 1.0,
            "jitter": 0.03,
            "blur_sigma": 0.2,
            "noise_sigma": 5,
            "noise_mono": True,
            "bw_threshold": 128,
            "bw_dither": True,
            "jpeg_quality": 75,
            "downsample": 1.0,
        },
    }
    response = client.post(f"/api/documents/{doc_id}/scan", json=scan_payload)
    assert response.status_code == 200
    assert response.json()["status"] == "done"

    response = client.post(f"/api/documents/{doc_id}/undo")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OP_NOT_APPLICABLE"

    page_uuid = pages[0]["uuid"]
    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "annot.add",
            "payload": {
                "annot": {
                    "id": str(uuid4()),
                    "page": page_uuid,
                    "kind": "rect",
                    "rect": [40, 40, 140, 140],
                }
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OP_NOT_APPLICABLE"


def test_assets_import_extract_split_export_and_jobs(client):
    doc = _create_document(client, page_count=2)
    doc_id = doc["id"]
    pages = client.get(f"/api/documents/{doc_id}/pages").json()

    response = client.get(f"/api/documents/{doc_id}/forms")
    assert response.status_code == 200
    assert response.json() == []

    asset_pdf = _sample_pdf_bytes(page_count=1)
    upload = client.post(
        f"/api/documents/{doc_id}/assets",
        files={"file": ("import.pdf", asset_pdf, "application/pdf")},
    )
    assert upload.status_code == 200
    asset_id = upload.json()["assetId"]

    import_uuid = str(uuid4())
    response = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "page.import",
            "payload": {
                "assetId": asset_id,
                "pages": [0, 0],
                "after": None,
                "newUuids": [import_uuid],
            },
        },
    )
    assert response.status_code == 200

    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    assert len(pages) == 3

    extract = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"pages": [pages[0]["uuid"], pages[1]["uuid"]], "title": "Extracted"},
    )
    assert extract.status_code == 200
    assert extract.json()["page_count"] == 2

    split = client.post(
        f"/api/documents/{doc_id}/split",
        json={"splitAfterIndex": 1, "leftTitle": "Left", "rightTitle": "Right"},
    )
    assert split.status_code == 200
    split_docs = split.json()
    assert len(split_docs) == 2
    assert split_docs[0]["page_count"] == 2
    assert split_docs[1]["page_count"] == 1

    export_pdf = client.post(
        f"/api/documents/{doc_id}/export",
        json={"format": "pdf", "flatten": False, "pages": None, "dpi": 200},
    )
    assert export_pdf.status_code == 200
    pdf_job = _wait_for_job_terminal(client, export_pdf.json()["id"])
    assert pdf_job["status"] == "done"

    events = client.get(f"/api/jobs/{pdf_job['id']}/events")
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: progress" in events.text

    cancel = client.post(f"/api/jobs/{pdf_job['id']}/cancel")
    assert cancel.status_code == 202
    assert cancel.json()["accepted"] is True

    pdf_result = client.get(f"/api/jobs/{pdf_job['id']}/result")
    assert pdf_result.status_code == 200
    assert pdf_result.headers["content-type"] == "application/pdf"

    export_png = client.post(
        f"/api/documents/{doc_id}/export",
        json={"format": "png", "flatten": False, "pages": [pages[0]["uuid"]], "dpi": 144},
    )
    assert export_png.status_code == 200
    png_job = _wait_for_job_terminal(client, export_png.json()["id"])
    png_result = client.get(f"/api/jobs/{png_job['id']}/result")
    assert png_result.status_code == 200
    assert png_result.headers["content-type"] == "image/png"

    export_jpeg = client.post(
        f"/api/documents/{doc_id}/export",
        json={"format": "jpeg", "flatten": False, "pages": [p["uuid"] for p in pages], "dpi": 144},
    )
    assert export_jpeg.status_code == 200
    jpeg_job = _wait_for_job_terminal(client, export_jpeg.json()["id"])
    jpeg_result = client.get(f"/api/jobs/{jpeg_job['id']}/result")
    assert jpeg_result.status_code == 200
    assert jpeg_result.headers["content-type"] in {"application/zip", "application/x-zip-compressed"}


def test_text_replace_and_redaction_remove_text(client):
    doc = _create_document(client, page_count=1)
    doc_id = doc["id"]

    page_uuid = client.get(f"/api/documents/{doc_id}/pages").json()[0]["uuid"]

    replace = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "text.replace",
            "payload": {
                "page": page_uuid,
                "old": "Hello PDF Studio #1",
                "new": "Replaced heading",
            },
        },
    )
    assert replace.status_code == 200

    text_after_replace = client.get(f"/api/documents/{doc_id}/pages/{page_uuid}/text")
    assert text_after_replace.status_code == 200
    merged = "\n".join(item["text"] for item in text_after_replace.json())
    assert "Replaced heading" in merged
    assert "Hello PDF Studio #1" not in merged

    marker_blocks = [
        item
        for item in text_after_replace.json()
        if "Searchable marker line" in item["text"]
    ]
    assert marker_blocks
    marker_rect = marker_blocks[0]["rect"]

    redact = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "redact.apply",
            "payload": {
                "page": page_uuid,
                "rects": [marker_rect],
                "fill": [0, 0, 0],
            },
        },
    )
    assert redact.status_code == 200

    text_after_redact = client.get(f"/api/documents/{doc_id}/pages/{page_uuid}/text")
    assert text_after_redact.status_code == 200
    merged_redacted = "\n".join(item["text"] for item in text_after_redact.json())
    assert "Searchable marker line" not in merged_redacted


def test_annotations_endpoint_tracks_add_update_delete(client):
    doc = _create_document(client, page_count=1)
    doc_id = doc["id"]
    page_uuid = client.get(f"/api/documents/{doc_id}/pages").json()[0]["uuid"]

    annotation_id = str(uuid4())
    add = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "annot.add",
            "payload": {
                "annot": {
                    "id": annotation_id,
                    "page": page_uuid,
                    "kind": "rect",
                    "rect": [40, 40, 160, 140],
                    "text": "hello",
                }
            },
        },
    )
    assert add.status_code == 200

    update = client.post(
        f"/api/documents/{doc_id}/ops",
        json={
            "kind": "annot.update",
            "payload": {
                "annot": {
                    "id": annotation_id,
                    "page": page_uuid,
                    "kind": "rect",
                    "rect": [50, 50, 170, 150],
                    "text": "updated",
                }
            },
        },
    )
    assert update.status_code == 200

    listed = client.get(f"/api/documents/{doc_id}/annotations")
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["id"] == annotation_id
    assert items[0]["text"] == "updated"

    delete = client.post(
        f"/api/documents/{doc_id}/ops",
        json={"kind": "annot.delete", "payload": {"id": annotation_id}},
    )
    assert delete.status_code == 200

    listed = client.get(f"/api/documents/{doc_id}/annotations")
    assert listed.status_code == 200
    assert listed.json() == []


def test_job_cancel_endpoint_accepts_request(client):
    doc = _create_document(client, page_count=8)
    doc_id = doc["id"]

    response = client.post(
        f"/api/documents/{doc_id}/scan",
        json={
            "mode": "export",
            "params": {
                "dpi": 220,
                "color_mode": "gray",
                "paper_tint": "#FFFFFF",
                "gamma": 1.0,
                "brightness": 1.0,
                "contrast": 1.15,
                "jitter": 0.03,
                "blur_sigma": 0.35,
                "noise_sigma": 8,
                "noise_mono": True,
                "bw_threshold": 128,
                "bw_dither": True,
                "jpeg_quality": 75,
                "downsample": 1.0,
            },
        },
    )
    assert response.status_code == 200
    job_id = response.json()["id"]

    cancel = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel.status_code == 202
    assert cancel.json()["accepted"] is True

    terminal = _wait_for_job_terminal(client, job_id)
    assert terminal["status"] in {"cancelled", "done", "error"}
