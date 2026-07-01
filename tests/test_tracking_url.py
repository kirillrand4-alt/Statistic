"""is_tracking_url: detect ad/tracking-tagged URLs that pollute organic page stats."""
from __future__ import annotations

from app.utils import is_tracking_url


def test_ad_and_tracking_urls_flagged():
    assert is_tracking_url(
        "https://berg-kompressor.ru/?utm_source=yandex-berg&utm_medium=cpc"
        "&utm_content={gbid}|{ad_id}|{device_type}&roistat=direct10_{source_type}_709629496")
    assert is_tracking_url("https://berg-kompressor.ru/catalog?roistat=direct10_{PHRASE}")
    assert is_tracking_url("https://x.ru/?utm_campaign=sale")
    assert is_tracking_url("https://x.ru/?yclid=12345")
    assert is_tracking_url("https://x.ru/?gclid=abc")
    assert is_tracking_url("https://x.ru/land/{PHRASE}/")  # unfilled macro in path


def test_clean_and_benign_query_urls_not_flagged():
    assert not is_tracking_url("https://berg-kompressor.ru/")
    assert not is_tracking_url("https://berg-kompressor.ru/catalog/osushiteli/")
    assert not is_tracking_url("https://x.ru/catalog?page=2")       # benign pagination
    assert not is_tracking_url("https://x.ru/search?q=компрессор")  # benign query
    assert not is_tracking_url("")
    assert not is_tracking_url(None)
