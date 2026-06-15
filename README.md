# SEO Статистика

Персональный self-hosted сервис SEO-аналитики. Агрегирует данные из **Google
Search Console**, **Яндекс.Вебмастера** и **Яндекс.Метрики** в собственную БД и
отвечает на 5 задач:

1. **ТОП-1 ключевой запрос** для каждого URL из загруженного списка.
2. **CTR** страницы (клики/показы в выдаче).
3. **Клики и показы**: по странице, по подмножеству и по всему сайту.
4. **Экспорт** детальной статистики за период (Excel/CSV).
5. **Рост/падение** метрик за период относительно периода сравнения.

> Статус: **Фаза 1 — Google Search Console** (полный вертикальный срез). Яндекс.Вебмастер
> и Метрика подключаются через ту же абстракцию провайдеров (Фазы 2–3).

📖 **Подробное руководство по всем страницам, источникам, скриптам и API —
[docs/GUIDE.md](docs/GUIDE.md).** Деплой на сервер — [DEPLOY.md](DEPLOY.md).

## Технологии

Python 3.11+ · FastAPI · SQLAlchemy + SQLite (переключается на PostgreSQL через
`DATABASE_URL`) · APScheduler (автосбор) · pandas + openpyxl (расчёты/экспорт) ·
Jinja2 + Chart.js (UI).

## Быстрый старт

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # заполните секреты
```

### Демо без ключей

```bash
python scripts/seed_demo.py     # наполняет БД синтетическими данными
uvicorn app.main:app --reload   # http://localhost:8000
```

### Реальные данные (GSC)

1. Создайте **сервисный аккаунт** в Google Cloud, включите *Search Console API*,
   скачайте JSON-ключ в `secrets/gsc-sa.json`.
2. ⚠️ В Search Console добавьте email сервисного аккаунта как **пользователя**
   ресурса (Настройки → Пользователи и разрешения). Иначе API вернёт 403.
3. В `.env` задайте `GSC_SERVICE_ACCOUNT_FILE`, `GSC_SITE_URL`
   (`sc-domain:example.com` или `https://example.com/`).
4. Запустите сервис и нажмите «Собрать статистику» на дашборде, либо:
   ```bash
   python scripts/run_collect_once.py --days 30
   ```
   Для длинной истории — кнопка backfill / `POST /api/admin/backfill`.

Альтернатива сервисному аккаунту — OAuth: `GSC_AUTH_MODE=oauth` +
`GSC_OAUTH_CLIENT_FILE` + `GSC_OAUTH_REFRESH_TOKEN`.

## Использование

- **Дашборд** (`/`) — итоги по сайту, график динамики, запуск сбора.
- **Проекты** (`/upload`) — создайте проект и загрузите список URL (вставкой или
  файлом). На странице проекта — ТОП-1 запросы, CTR по страницам, итоги, экспорт.
- **Сравнение** (`/compare`) — рост/падение по метрике между двумя периодами с
  группировкой (сайт / подмножество / страницы / запросы).

Помимо этого есть страницы: Ключевые слова, Страницы (десктоп/мобайл + динамика),
ТОП-10 выдачи (arsenkin), Каннибализация, Доноры (Miralinks), Антинакрутка,
Индексация, Метрика, Ошибки 404. Все они и их фильтры/выгрузки описаны в
[docs/GUIDE.md](docs/GUIDE.md).

## API (основное)

`POST /api/projects` · `POST /api/projects/{id}/urls` ·
`GET /api/projects/{id}/top-keywords` · `GET /api/projects/{id}/ctr` ·
`GET /api/totals` · `GET /api/compare` · `GET /api/export` ·
`POST /api/admin/collect/run` · `POST /api/admin/backfill` ·
`GET /api/admin/status` · `POST /api/admin/sites`. Документация: `/docs`.

## Автосбор

APScheduler запускает ежедневный инкрементальный сбор (час задаётся
`COLLECT_CRON_HOUR`), перезабирая последние `COLLECT_REFETCH_DAYS` дней (GSC
правит свежие данные). Управляется флагом `ENABLE_SCHEDULER`.

## Тесты

```bash
pytest
```
Тесты используют изолированную SQLite-БД и `MockProvider` — ключи не нужны.

## Docker

```bash
docker compose up --build       # монтирует ./data и ./secrets
```

## Деплой на сервер

Пошаговая инструкция (Ubuntu/VPS, systemd + nginx, обслуживание под `/stat` или
на поддомене) — в [DEPLOY.md](DEPLOY.md). Готовые файлы — в `deploy/`
(`seostat.service`, `nginx-subpath.conf`, `nginx-subdomain.conf`). Для подпути
задайте `ROOT_PATH=/stat` в `.env`.

## Архитектура

```
app/
  providers/   абстракция источников (base) + gsc (+ yandex_* в Фазах 2–3)
  db/          модели (снапшоты по дням) + движок
  services/    ingest, top_keyword, ctr, totals, growth, export
  scheduler/   APScheduler: инкрементальный сбор + backfill
  api/         REST-роуты + серверные HTML-страницы
  templates/, static/
```
Подробности — в `app/` (модули документированы docstring-ами).
