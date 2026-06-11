# Деплой на сервер (Ubuntu 22.04, например Selectel VPS)

Сервис ставится как **отдельный** systemd-процесс и проксируется вашим nginx на
путь `/stat`. Ваш существующий сайт (`/opt/parser`) при этом не затрагивается.

Все команды — под `root` (как в консоли Selectel).

## 1. Зависимости

```bash
apt update
apt install -y python3-venv python3-pip git nginx
```

## 2. Код

```bash
git clone -b claude/jolly-brahmagupta-oZUpC https://github.com/kirillrand4-alt/Statistic.git /opt/seostat
cd /opt/seostat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
> Если репозиторий приватный — используйте тот же способ доступа, что и для
> `/opt/parser` (токен/SSH-ключ).

## 3. Настройка `.env`

```bash
cp .env.example .env
nano .env
```
Заполните как минимум:
```
SECRET_KEY=<длинная случайная строка>
ROOT_PATH=/stat                         # обслуживание под /stat
GSC_AUTH_MODE=service_account
GSC_SERVICE_ACCOUNT_FILE=./secrets/gsc-sa.json
GSC_SITE_URL=sc-domain:parsercompressor.online   # или https://parsercompressor.online/
```
Положите ключ сервисного аккаунта:
```bash
mkdir -p /opt/seostat/secrets
# скопируйте gsc-sa.json в /opt/seostat/secrets/gsc-sa.json
```
> ⚠️ Email сервисного аккаунта добавьте **пользователем** ресурса в Google
> Search Console (Настройки → Пользователи и разрешения), иначе API вернёт 403.

(Опционально проверить без ключей: `.venv/bin/python scripts/seed_demo.py`.)

> Альтернатива ручной настройке ключа: откройте `…/stat/admin` и вставьте
> JSON-ключ в форму «Подключить Google Search Console» — сервис сам найдёт
> сайты и загрузит данные. ⚠️ Сначала закройте `/stat` паролем (см. ниже), так
> как страница доступна публично.

## 4. systemd-сервис

```bash
cp deploy/seostat.service /etc/systemd/system/seostat.service
systemctl daemon-reload
systemctl enable --now seostat
systemctl status seostat --no-pager        # должно быть active (running)
curl -s http://127.0.0.1:8011/stat/health  # {"status":"ok"}
```
> Порт по умолчанию 8011. Если занят (проверьте `ss -tlnp | grep 8011`),
> поменяйте его в `deploy/seostat.service` и в nginx-конфиге.

## 5. nginx → /stat

Найдите конфиг, обслуживающий `parsercompressor.online`:
```bash
grep -Rl "parsercompressor" /etc/nginx/
```
В его блок `server { ... }` вставьте содержимое `deploy/nginx-subpath.conf`
(два `location`). Затем:
```bash
nginx -t && systemctl reload nginx
```
Откройте `https://parsercompressor.online/stat/`.

> Загрузка визитов — это большие файлы. Снипеты уже содержат
> `client_max_body_size 1024M` (без него nginx отдаёт **413 Request Entity Too
> Large** на архивах). Если конфиг уже подключён со старым лимитом, поднимите его
> на живом файле одной командой (сам найдёт нужный конфиг, сделает бэкап,
> проверит `nginx -t` и перезагрузит, при ошибке откатит):
> ```bash
> bash deploy/raise_upload_limit.sh
> ```
> Совсем большие архивы (сотни МБ) надёжнее заливать по SFTP в
> `/opt/seostat/uploads` и разбирать через `scripts/metrika_logs.py --import-dir`.

### Альтернатива — поддомен (проще и безопаснее)
Вместо правки существующего конфига можно поднять `stat.parsercompressor.online`
(см. `deploy/nginx-subdomain.conf`): добавьте A-запись DNS на `161.104.48.72`,
положите конфиг в `sites-available`, при этом оставьте `ROOT_PATH=` пустым.

## 6. Первый сбор данных

В браузере: `/stat` → дашборд → «Собрать статистику», либо Настройки → Backfill.
Или из консоли:
```bash
cd /opt/seostat && .venv/bin/python scripts/run_collect_once.py --days 30
```
Дальше ежедневный автосбор делает APScheduler (час — `COLLECT_CRON_HOUR`).

## Обновление версии
```bash
cd /opt/seostat && git pull && .venv/bin/pip install -r requirements.txt
systemctl restart seostat
```

## Про вашу `parser.service`
В консоли `systemctl restart parser` падает: «Unit parser.service not found» —
значит юнит называется иначе или сервис запущен не через systemd. Найти, как
именно работает текущий сайт:
```bash
systemctl list-units --type=service | grep -iE 'parser|webui|gunicorn|uvicorn'
ss -tlnp | grep -E ':80|:443|:8000|:5000'
```
Это к нашему сервису не относится — наш юнит называется `seostat`.
