import io

from pypdf import PdfWriter


def _sample_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_document_crud(client):
    files = {"file": ("sample.pdf", _sample_pdf_bytes(), "application/pdf")}
    response = client.post("/api/documents", files=files, data={"title": "Sample"})
    assert response.status_code == 200
    doc = response.json()

    doc_id = doc["id"]
    assert doc["title"] == "Sample"
    assert doc["page_count"] == 1

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

    response = client.patch(f"/api/documents/{doc_id}", json={"title": "Renamed"})
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"

    response = client.delete(f"/api/documents/{doc_id}")
    assert response.status_code == 204

    response = client.get(f"/api/documents/{doc_id}")
    assert response.status_code == 404
