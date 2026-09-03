# myOpenPDF

Local-first web PDF studio with a React frontend and FastAPI backend.

Current progress covers M0-M6 foundation:
- upload PDFs
- list documents in a local library
- render documents in a browser with pdf.js
- versioned file serving through op-log cursor
- page manager ops (rotate/delete/duplicate/insert blank/import/split/extract)
- persisted annotations via op-log
- scan preview/export + in-place scan apply
- export and compression as background jobs with progress polling

## Stack
- Backend: FastAPI, SQLModel, SQLite, PyMuPDF, Pillow, NumPy
- Frontend: React + TypeScript + Vite + pdf.js
- Environment: Python `venv`

## Quick Start

### 1. Backend (Windows PowerShell)
```powershell
cd backend
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 2. Frontend (new terminal)
```powershell
cd frontend
pnpm install
pnpm dev
```

Frontend runs on `http://127.0.0.1:5173` and proxies `/api` to backend `127.0.0.1:8000`.

### 3. Checks
```powershell
cd backend
. .venv/Scripts/Activate.ps1
ruff check .
pytest

cd ../frontend
pnpm build
```

## API Implemented
- `GET /api/health`
- `POST /api/documents` (multipart upload)
- `GET /api/documents`
- `GET /api/documents/{id}`
- `GET /api/documents/{id}/file`
- `GET /api/documents/{id}/pages`
- `GET /api/documents/{id}/pages/{pageUuid}/thumb`
- `GET /api/documents/{id}/pages/{pageUuid}/text`
- `POST /api/documents/{id}/search`
- `POST /api/documents/{id}/ops`
- `GET /api/documents/{id}/ops`
- `POST /api/documents/{id}/undo`
- `POST /api/documents/{id}/redo`
- `GET /api/documents/{id}/annotations`
- `POST /api/documents/{id}/assets`
- `GET /api/documents/{id}/forms`
- `POST /api/documents/{id}/export`
- `POST /api/documents/{id}/extract`
- `POST /api/documents/{id}/split`
- `PATCH /api/documents/{id}`
- `DELETE /api/documents/{id}`
- `GET /api/scan/presets`
- `POST /api/scan/presets`
- `DELETE /api/scan/presets/{id}`
- `POST /api/documents/{id}/scan/preview`
- `POST /api/documents/{id}/scan` (export + in_place)
- `POST /api/documents/{id}/compress`
- `POST /api/documents/{id}/compress/estimate`
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/events`
- `POST /api/jobs/{id}/cancel`
- `GET /api/jobs/{id}/result`

## Notes
- The repository is currently MIT-licensed as requested.
- PyMuPDF has AGPL/commercial terms; before broad redistribution, obtain a compatible commercial licence or replace the PDF mutation engine.
