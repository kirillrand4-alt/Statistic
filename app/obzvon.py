"""Отдельное приложение «Обзвон» для продажников.

Запускается отдельным процессом на порту 8012 и проксируется Caddy/Nginx на
подпуть OBZVON_ROOT_PATH (обычно /obzvon). Старые базы kc/meyer используют
HTTP Basic, а объединённая Centro — собственную cookie-аутентификацию и роли.
"""
from __future__ import annotations

import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes_obzvon
from app.config import get_settings
from app.db.base import init_db
from app.web import STATIC_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def parse_users(raw: str) -> dict[str, str]:
    """«vasya:pass1,petya:pass2» -> {логин: пароль}."""
    users: dict[str, str] = {}
    for pair in (raw or "").replace(";", ",").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        login, password = pair.split(":", 1)
        if login.strip() and password:
            users[login.strip()] = password
    return users


class BasicAuthASGI:
    """HTTP Basic для старых страниц; Centro использует свою авторизацию."""

    def __init__(self, app, users: dict[str, str]):
        self.app = app
        self.users = users

    @staticmethod
    def _is_centro_path(path: str) -> bool:
        clean = path.rstrip("/")
        return (
            clean.endswith("/centro")
            or "/centro/" in clean
            or clean.endswith(("/centro1", "/centro2"))
            or clean.endswith("/static/css/centro.css")
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if self._is_centro_path(path):
            return await self.app(scope, receive, send)
        if not self.users:
            return await self._deny(
                scope,
                receive,
                send,
                503,
                "Обзвон не настроен: задайте OBZVON_USERS в .env "
                "(формат: логин:пароль,логин2:пароль2) и перезапустите сервис.",
            )
        header = b""
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                header = value
                break
        if not self._ok(header):
            return await self._deny(
                scope,
                receive,
                send,
                401,
                "Нужен логин и пароль обзвона.",
            )
        return await self.app(scope, receive, send)

    def _ok(self, header: bytes) -> bool:
        try:
            scheme, _, credentials = header.decode("latin-1").partition(" ")
            if scheme.lower() != "basic":
                return False
            login, _, password = (
                base64.b64decode(credentials.strip())
                .decode("utf-8")
                .partition(":")
            )
        except (UnicodeDecodeError, binascii.Error, ValueError):
            return False
        expected = self.users.get(login)
        return expected is not None and secrets.compare_digest(
            password.encode("utf-8"), expected.encode("utf-8")
        )

    async def _deny(self, scope, receive, send, status: int, text: str):
        body = text.encode("utf-8")
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ]
        if status == 401:
            headers.append(
                (b"www-authenticate", b'Basic realm="Obzvon", charset="UTF-8"')
            )
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.db.base import SessionLocal
    from app.services import callbase, centro_sales

    db = SessionLocal()
    try:
        callbase.ensure_schema(db)
    finally:
        db.close()
    centro_sales.init_schema()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    obz = settings.obzvon_path
    app = FastAPI(
        title="Обзвон",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount(
        f"{obz}/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    # Специфичные маршруты Centro должны идти перед catch-all /{base}.
    from app.api import routes_centro_sales

    app.include_router(routes_centro_sales.router, prefix=obz)
    app.include_router(routes_obzvon.router, prefix=obz)

    if obz:
        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse(url=f"{obz}/", status_code=307)

    users = parse_users(settings.obzvon_users)
    if not users:
        logging.getLogger(__name__).warning(
            "OBZVON_USERS пуст — старые базы обзвона закрыты"
        )
    return BasicAuthASGI(app, users)


app = create_app()
