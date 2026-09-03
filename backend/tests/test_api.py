import pymupdf


def _sample_pdf_bytes() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 120), "Hello PDF Studio")
    page.insert_text((72, 150), "Searchable marker line")
    data = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return data


def _create_document(client) -> dict:
    files = {"file": ("sample.pdf", _sample_pdf_bytes(), "application/pdf")}
    response = client.post("/api/documents", files=files, data={"title": "Sample"})
    assert response.status_code == 200
    return response.json()


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
    job = response.json()
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
    job = response.json()
    assert job["kind"] == "compress"
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
