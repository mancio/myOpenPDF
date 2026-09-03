# myOpenPDF

Local-first web PDF studio with a React frontend and FastAPI backend.

Current progress follows M0 + M1 foundation + M2 scan skeleton:
- upload PDFs
- list documents in a local library
- render documents in a browser with pdf.js
- versioned file serving through op-log cursor
- scan preview and scanned-PDF export job endpoint

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
- `PATCH /api/documents/{id}`
- `DELETE /api/documents/{id}`
- `GET /api/scan/presets`
- `POST /api/scan/presets`
- `DELETE /api/scan/presets/{id}`
- `POST /api/documents/{id}/scan/preview`
- `POST /api/documents/{id}/scan` (export mode ready, in_place intentionally blocked)
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/result`

## Notes
- The repository is currently MIT-licensed as requested.
- The code now uses PyMuPDF for M1/M2 foundations; verify licence compatibility strategy as features mature.
