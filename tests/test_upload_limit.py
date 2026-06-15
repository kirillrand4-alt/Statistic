"""A big phrases paste (a large multipart *text* field) must not trip
Starlette's 1 MB per-part default once the app raises the limit.

Note: Starlette only size-checks non-file fields; file uploads stream to a temp
file. So the failure mode users hit ("Part exceeded maximum size of 1024KB") is
the textarea, not the file input.
"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.formlimit import apply_upload_limit


def test_large_form_field_allowed():
    apply_upload_limit(16 * 1024 * 1024)  # idempotent: no-op if app already patched higher
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        form = await request.form()
        return {"n": len(form.get("phrases", ""))}

    client = TestClient(app)
    big = "ремонт компрессора\n" * 100000  # ~1.9M chars, well over the 1 MB default
    # files=... forces multipart/form-data, so the text field becomes a checked part
    r = client.post("/echo", data={"phrases": big}, files={"_": ("x.txt", b"x")})
    assert r.status_code == 200
    assert r.json()["n"] == len(big)
