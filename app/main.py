"""FastAPI 应用：网页 + JSON API。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import db, logic
from .errors import DomainError

STATIC_DIR = Path(__file__).parent / "static"


class VersionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    csv_text: str = Field(min_length=1)


class MarkerIn(BaseModel):
    version_id: int
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    note: str = Field(default="", max_length=500)


class LiftIn(BaseModel):
    version_id: int
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DB_PATH", os.path.join("data", "app.db"))
    db.init_db(path)
    app = FastAPI(title="口述史馆禁播消音标记管理", version="1.0.0")
    app.state.db_path = path

    @contextmanager
    def session():
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    # ------------------------------------------------------------- 网页

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    # ------------------------------------------------------------- 剪辑版

    @app.post("/api/versions", status_code=201)
    def api_create_version(body: VersionIn):
        with session() as conn:
            return logic.create_version(conn, body.name, body.csv_text)

    @app.get("/api/versions")
    def api_list_versions():
        with session() as conn:
            return logic.list_versions(conn)

    @app.get("/api/versions/{version_id}")
    def api_get_version(version_id: int):
        with session() as conn:
            return logic.get_version(conn, version_id)

    @app.get("/api/versions/{version_id}/muted")
    def api_muted(version_id: int):
        with session() as conn:
            return logic.muted_for_version(conn, version_id)

    # ------------------------------------------------------------- 标记

    @app.post("/api/markers", status_code=201)
    def api_create_marker(body: MarkerIn):
        with session() as conn:
            return logic.create_marker(
                conn, body.version_id, body.start_ms, body.end_ms, body.note
            )

    @app.get("/api/markers")
    def api_list_markers():
        with session() as conn:
            return logic.list_markers(conn)

    @app.get("/api/markers/{marker_id}")
    def api_get_marker(marker_id: int):
        with session() as conn:
            return logic.get_marker_detail(conn, marker_id)

    @app.post("/api/markers/{marker_id}/withdraw")
    def api_withdraw_marker(marker_id: int):
        with session() as conn:
            return logic.withdraw_marker(conn, marker_id)

    @app.post("/api/markers/{marker_id}/lifts", status_code=201)
    def api_create_lift(marker_id: int, body: LiftIn):
        with session() as conn:
            return logic.create_lift(
                conn, marker_id, body.version_id, body.start_ms, body.end_ms
            )

    @app.get("/api/markers/{marker_id}/impact")
    def api_marker_impact(marker_id: int):
        with session() as conn:
            return logic.marker_impact(conn, marker_id)

    return app


app = create_app()
