"""Raise Starlette's per-part multipart size limit.

Starlette's ``Request.form()`` caps each form field / uploaded file at 1 MB
(``max_part_size``) and FastAPI calls ``request.form()`` with no arguments, so
there is no per-route knob. Large uploads (full keyword lists pasted into a
textarea, URL lists, Metrika CSV exports) trip it with::

    {"detail":"Part exceeded maximum size of 1024KB."}

We patch the default once at startup so every form route gets the configured
limit. FastAPI still passes ``max_files`` / ``max_fields`` positionally as
keywords, which our wrapper forwards untouched.
"""
from __future__ import annotations

from starlette.requests import Request

_PATCHED = False


def apply_upload_limit(max_bytes: int) -> None:
    """Make ``Request.form()`` allow up to ``max_bytes`` per part by default."""
    global _PATCHED
    if _PATCHED:
        return
    _orig_form = Request.form

    def form(self, *, max_files=1000, max_fields=1000, max_part_size=max_bytes):
        return _orig_form(self, max_files=max_files, max_fields=max_fields,
                          max_part_size=max_part_size)

    Request.form = form  # type: ignore[method-assign]
    _PATCHED = True
