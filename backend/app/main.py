from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import create_db_and_tables
from app.routers.compress import router as compress_router
from app.routers.documents import router as documents_router
from app.routers.features import router as features_router
from app.routers.health import router as health_router
from app.routers.ops import router as ops_router
from app.routers.scan import job_router, preset_router, scan_router
from app.services.store import ensure_store_layout


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    ensure_store_layout(settings.store_root)
    create_db_and_tables()
    yield


app = FastAPI(title="myOpenPDF API", version="0.1.0", lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(features_router, prefix="/api")
app.include_router(compress_router, prefix="/api")
app.include_router(ops_router, prefix="/api")
app.include_router(preset_router, prefix="/api")
app.include_router(scan_router, prefix="/api")
app.include_router(job_router, prefix="/api")
