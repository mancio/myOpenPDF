# myOpenPDF

Local-first web PDF studio with a React frontend and FastAPI backend.

This repository starts milestone M0 from the plan/spec:
- upload PDFs
- list documents in a local library
- serve PDF bytes back for viewer integration

## Stack
- Backend: FastAPI, SQLModel, SQLite
- Frontend: React + TypeScript + Vite (starter shell)
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

## API Implemented (M0 start)
- `GET /api/health`
- `POST /api/documents` (multipart upload)
- `GET /api/documents`
- `GET /api/documents/{id}`
- `GET /api/documents/{id}/file`
- `PATCH /api/documents/{id}`
- `DELETE /api/documents/{id}`

## Notes
- The repository is currently MIT-licensed as requested.
- If AGPL-only dependencies are introduced later (for example PyMuPDF), review overall licensing compatibility before release.
