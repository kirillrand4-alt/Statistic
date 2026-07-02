"""Собрать частотности Wordstat с операторами через ARSENKIN API (инструмент
«Парсинг Wordstat»): base / "фраза" / "!фраза" / "[!фраза]".

Новый веб-Wordstat отдаёт операторы только на вкладках «Топы/Регионы», а не в
«Динамике», поэтому помесячной истории с операторами не существует — это снимок
за ~30 дней. Арсенкин снимает такой срез; мы его сохраняем и копим помесячно.

Токен: сохранённый ``arsenkin_token`` (Настройки) или --token.

Проба (1 фраза × 4 типа — стоит копейки, печатает сырой ответ):
    python scripts/arsenkin_freq.py --probe "винтовой компрессор"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.credentials import get_cred  # noqa: E402
from app.providers.arsenkin import (  # noqa: E402
    WS_TO_MATCH, Arsenkin, check_done, is_done, parse_wordstat_result,
)

ALL_WS = ("base", "quoted", "overal", "exact")


def _wait_result(client: Arsenkin, task_id, timeout=180) -> dict:
    """Поллить /check до готовности, затем /get. Возвращает payload /get."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = client.check(task_id)
        if check_done(st):
            break
        print(f"  … задача {task_id}: {st.get('status') or st.get('code')} "
              f"{st.get('progress', '')}", flush=True)
        time.sleep(5)
    return client.get(task_id)


def probe(client: Arsenkin, phrase: str, regions, device: str) -> None:
    print(f"Проба wordstat для «{phrase}» (ws={ALL_WS}, regions={regions}, device='{device}')…",
          flush=True)
    started = client.set_wordstat([phrase], regions=regions, device=device, ws=ALL_WS)
    print("\n--- ответ /set ---")
    print(json.dumps(started, ensure_ascii=False, indent=2)[:1500])
    task_id = started.get("task_id") or (started.get("result") or {}).get("task_id")
    if not task_id:
        print("\n⚠ Нет task_id — вероятно, инструмент называется иначе или недоступен на тарифе.\n"
              "  Проверь в справке точное tools_name/поля: help.arsenkin.ru/api/api-wordstat")
        return
    payload = _wait_result(client, task_id)
    print("\n--- ответ /get (сырой, обрезан) ---")
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    print(raw[:4000])
    os.makedirs("data/arsenkin", exist_ok=True)
    with open("data/arsenkin/freq_probe.json", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
    print("\nПолный ответ сохранён: data/arsenkin/freq_probe.json")
    rows = parse_wordstat_result(payload)
    print(f"\n--- разобрано строк: {len(rows)} ---")
    for r in rows:
        print(f"  ws={r['ws']:<7} → match={WS_TO_MATCH.get(r['ws'], '?'):<6} "
              f"частота={r['value']} регион={r['region']}")
    if not rows:
        print("  (парсер ничего не извлёк — пришли сырой JSON выше, поправлю parse_wordstat_result)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="токен arsenkin (иначе из Настроек)")
    ap.add_argument("--probe", metavar="ФРАЗА", help="проба: 1 фраза × 4 типа, печать сырого ответа")
    ap.add_argument("--regions", default="0", help="id регионов через запятую (0=все)")
    ap.add_argument("--device", default="", help="устройство: '' все | desktop | mobile")
    a = ap.parse_args()

    token = a.token or get_cred("arsenkin_token")
    if not token:
        sys.exit("Нет токена arsenkin (--token или Настройки → arsenkin_token).")
    regions = [int(x) for x in str(a.regions).split(",") if x.strip().lstrip("-").isdigit()]
    client = Arsenkin(token)

    if a.probe:
        probe(client, a.probe, regions or [0], a.device)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
