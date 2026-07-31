"""Отдельное приложение «Обзвон» для продажников.

Запускается СВОИМ процессом (см. deploy/seostat-obzvon.service) на своём порту
и проксируется nginx на подпуть OBZVON_ROOT_PATH (по умолчанию /obzvon).
Содержит ТОЛЬКО страницы обзвона — роутов основного сервиса статистики здесь
нет, поэтому продажники физически не могут на него попасть.

Доступ — HTTP Basic auth со своими парами логин:пароль из ``OBZVON_USERS``
(.env, формат «vasya:pass1,petya:pass2»). Пароли основного сервиса (nginx
basic auth на /stat) никак не связаны с этими.
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
from app.web import STATIC_DIR, templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def parse_users(raw: str) -> dict[str, str]:
    """«vasya:pass1,petya:pass2» -> {логин: пароль} (пробелы/; допускаются)."""
    users: dict[str, str] = {}
    for pair in (raw or "").replace(";", ",").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        login, pwd = pair.split(":", 1)
        if login.strip() and pwd:
            users[login.strip()] = pwd
    return users


class BasicAuthASGI:
    """Basic auth на всё приложение (включая статику). Без настроенных
    пользователей доступ закрыт совсем (fail closed) с подсказкой в теле."""

    def __init__(self, app, users: dict[str, str]):
        self.app = app
        self.users = users

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not self.users:
            return await self._deny(scope, receive, send, 503,
                                    "Обзвон не настроен: задайте OBZVON_USERS в .env "
                                    "(формат: логин:пароль,логин2:пароль2) и перезапустите сервис.")
        header = b""
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                header = v
                break
        if not self._ok(header):
            return await self._deny(scope, receive, send, 401, "Нужен логин и пароль обзвона.")
        return await self.app(scope, receive, send)

    def _ok(self, header: bytes) -> bool:
        try:
            scheme, _, cred = header.decode("latin-1").partition(" ")
            if scheme.lower() != "basic":
                return False
            login, _, pwd = base64.b64decode(cred.strip()).decode("utf-8").partition(":")
        except (UnicodeDecodeError, binascii.Error, ValueError):
            return False
        expected = self.users.get(login)
        # Сравниваем БАЙТЫ: secrets.compare_digest на не-ASCII str кидает TypeError
        # (кириллический пароль → 500 и никто не войдёт). Проверка None — первой,
        # чтобы не звать .encode() у неизвестного логина.
        return expected is not None and secrets.compare_digest(
            pwd.encode("utf-8"), expected.encode("utf-8"))

    async def _deny(self, scope, receive, send, status: int, text: str):
        body = text.encode("utf-8")
        headers = [(b"content-type", b"text/plain; charset=utf-8"),
                   (b"content-length", str(len(body)).encode())]
        if status == 401:
            headers.append((b"www-authenticate", b'Basic realm="Obzvon", charset="UTF-8"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # таблицы (в т.ч. call_company) создаются при старте
    from app.db.base import SessionLocal
    from app.services import callbase
    db = SessionLocal()
    try:  # добавить новые колонки на живой базе + region для старых строк
        callbase.ensure_schema(db)
    finally:
        db.close()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    obz = settings.obzvon_path  # "" или "/obzvon"

    app = FastAPI(title="Обзвон", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)

    # base_path в шаблонах обзвона приходит через контекст запроса (routes_obzvon),
    # а не через templates.env.globals — тот общий с основным приложением.
    app.mount(f"{obz}/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(routes_obzvon.router, prefix=obz)
    from app.api import routes_centro  # Центробежные 1/2
    routes_centro.include_centro(app, prefix=obz)

    if obz:  # корень процесса -> на страницу обзвона (удобно при заходе на порт)
        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse(url=f"{obz}/", status_code=307)

    users = parse_users(settings.obzvon_users)
    if not users:
        logging.getLogger(__name__).warning(
            "OBZVON_USERS пуст — доступ к обзвону закрыт, задайте пользователей в .env")
    return BasicAuthASGI(app, users)


app = create_app()
