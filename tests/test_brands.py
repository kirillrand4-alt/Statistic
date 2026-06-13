"""Per-domain URL->brand map import + brand filtering of a project."""
from __future__ import annotations

from datetime import date

from app.bootstrap import ensure_sources
from app.db.models import Page, PageMetricDaily, Project, ProjectUrl, Site
from app.providers.base import DateRange
from app.services import brands as B
from app.services import totals as T
from app.utils import normalize_url


def _site(db, host="prokompressor.ru"):
    src = ensure_sources(db)["gsc"]
    s = Site(source_id=src.id, property_uri=f"https://{host}/", display_name=host)
    db.add(s)
    db.commit()
    return s


def test_import_brand_csv_cp1251_and_listing(db):
    s = _site(db)
    csv = ("бренд\tссылка\n"
           "Abac\thttps://prokompressor.ru/catalog/abac-1/\n"
           "Abac\thttps://prokompressor.ru/catalog/abac-2/\n"
           "Berg\thttps://prokompressor.ru/catalog/berg-1/?utm=x\n").encode("cp1251")
    res = B.import_brand_csv(db, csv)
    assert res["rows"] == 3 and res["brands"] == 2

    brands = {b["brand"]: b["urls"] for b in B.list_brands(db, "prokompressor.ru")}
    assert brands == {"Abac": 2, "Berg": 1}
    # url_key is host+path (UTM/scheme/www ignored), so it matches clean URLs
    keys = B.brand_url_keys(db, "prokompressor.ru", ["Berg"])
    assert keys == {"prokompressor.ru/catalog/berg-1"}


def test_sync_project_urls_populates_empty_project(db):
    """Uploading brands fills the project's URL list, so a dedicated (empty)
    brand project gets per-URL stats/goals instead of nothing."""
    s = _site(db)
    B.import_brand_csv(db, (
        "Abac\thttps://prokompressor.ru/a/\n"
        "Berg\thttps://prokompressor.ru/b/?utm=x\n"
        "Berg\thttps://prokompressor.ru/b/\n").encode("utf-8"))  # dup key -> one
    proj = Project(name="бренды", site_id=s.id)
    db.add(proj)
    db.commit()

    added = B.sync_project_urls(db, proj, "prokompressor.ru")
    db.refresh(proj)
    assert added == 2 and len(proj.urls) == 2          # /a and /b (UTM dup collapsed)
    # idempotent: a second sync adds nothing
    assert B.sync_project_urls(db, proj, "prokompressor.ru") == 0


def test_brand_filter_restricts_project_subset(db):
    s = _site(db)
    B.import_brand_csv(db, (
        "Abac\thttps://prokompressor.ru/a/\n"
        "Berg\thttps://prokompressor.ru/b/\n").encode("utf-8"))

    d = date(2026, 6, 1)
    proj = Project(name="P", site_id=s.id)
    db.add(proj)
    db.commit()
    for path, clicks in (("a", 10), ("b", 3)):
        url = f"https://prokompressor.ru/{path}/"
        db.add(ProjectUrl(project_id=proj.id, url=url, normalized_url=normalize_url(url)))
        pg = Page(site_id=s.id, url=url, normalized_url=normalize_url(url))
        db.add(pg)
        db.flush()
        db.add(PageMetricDaily(site_id=s.id, page_id=pg.id, date=d,
                               clicks=clicks, impressions=clicks * 10, position=3.0))
    db.commit()
    db.refresh(proj)
    dr = DateRange(d, d)

    assert T.subset_totals(db, proj, dr)["clicks"] == 13          # whole project
    keys = B.brand_url_keys(db, "prokompressor.ru", ["Abac"])
    only = {u.normalized_url for u in proj.urls
            if __import__("app.services.goals", fromlist=["page_key"]).page_key(u.url) in keys}
    assert T.subset_totals(db, proj, dr, only_norms=only)["clicks"] == 10  # only Abac URL
