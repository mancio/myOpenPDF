from fastapi.responses import JSONResponse


def api_error(status_code: int, code: str, message: str, detail: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "detail": detail or {}}},
    )
