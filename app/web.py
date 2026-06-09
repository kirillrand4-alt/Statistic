"""Shared Jinja2 templates environment."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _numfmt(v) -> str:
    try:
        return f"{int(round(float(v))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _ctr(v) -> str:
    try:
        return f"{float(v) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def _pos(v) -> str:
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "—"


templates.env.filters["numfmt"] = _numfmt
templates.env.filters["pct"] = _pct
templates.env.filters["ctr"] = _ctr
templates.env.filters["pos"] = _pos

# Overridden in app.main.create_app() with the configured base path ("" or "/stat").
templates.env.globals.setdefault("base_path", "")
