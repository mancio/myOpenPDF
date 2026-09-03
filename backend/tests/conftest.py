import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    store_root = tmp_path_factory.mktemp("pdfstudio-data")
    os.environ["PDFSTUDIO_STORE_ROOT"] = str(store_root)
    os.environ["PDFSTUDIO_DB_PATH"] = str(store_root / "pdfstudio-test.sqlite3")

    from app.config import get_settings
    from app.db import get_engine

    get_settings.cache_clear()
    get_engine.cache_clear()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
