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

### Метрика — визиты и хиты (Logs API)

Закачка по всем доменам сразу: на каждый домен висит один запрос Logs API,
**от новых дат к старым** (от `--to`, по умолчанию вчера). Размер окна
подбирается по объёму данных — Яндекс через `evaluate` сообщает, сколько дней
влезет в один запрос (потолок `--max-chunk`, по умолчанию 30), так что
нагруженные домены качаются мелкими окнами, а тихие — крупными; период
обрезается датой создания счётчика. Фикс. размер — `--chunk N`. Готовность
проверяется раз в 3 минуты (создаёт → ждёт → качает → чистит у Яндекса; окно,
не готовое за 40 мин, отменяется и пропускается — доберётся позже). 429/сбои
API не роняют закачку — запрос повторяется. Нужен тот же токен Яндекса со
scope `metrika:read`.
```bash
cd /opt/seostat && .venv/bin/python scripts/metrika_logs.py --list       # счётчики
# докачка в фоне (переживёт закрытие консоли); уже скачанные окна пропускаются,
# поэтому после сбоя просто запустите снова — продолжит с места остановки:
bash deploy/metrika_sync.sh start --from 2025-06-01
# перекачать всё заново (upsert, без дублей): добавьте --force
bash deploy/metrika_sync.sh log        # следить (Ctrl-C — выйти, закачка не прервётся)
bash deploy/metrika_sync.sh status     # запущено? PID? путь к логу
bash deploy/metrika_sync.sh stop       # остановить
.venv/bin/python scripts/metrika_logs.py --site 7 --coverage             # покрытие/дыры
```
Счётчик к домену подбирается сам; если не угадал — задайте `--targets "7:12345,8:67890"`.
Дубли домена (`sc-domain:` и `https://`-свойство) качаются **один раз**; уже
скачанные под дублями визиты/хиты при старте переносятся на один сайт
(можно и отдельно: `--merge-dupes`).
Пропущенные окна добираются точечно: тот же запуск с `--chunk 1` без `--force`.

#### Автозапуск по расписанию (докачка после сброса квоты)

У Яндекса есть суточная квота на число API-запросов (ошибка 429
`quota_requests_by_uid`). С флагом `--stop-on-quota` прогон при исчерпании
квоты аккуратно завершается, а systemd-таймер запускает его каждое утро —
квота к тому времени сбрасывается, и докачка (gap-fill) продолжается с того
места, где остановилась. За несколько дней так добирается весь архив, а потом
таймер ежедневно подтягивает свежие дни.
```bash
cp deploy/seostat-metrika.service /etc/systemd/system/
cp deploy/seostat-metrika.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now seostat-metrika.timer     # включить и поставить на 05:00
systemctl start  seostat-metrika.service         # (необязательно) запустить сейчас же
systemctl list-timers seostat-metrika.timer      # когда следующий запуск
journalctl -u seostat-metrika -f                 # лог закачки
```
Перед включением таймера остановите ручную закачку (`metrika_sync.sh stop`),
чтобы не качать в два процесса. Период правится в `seostat-metrika.service`
(`--from`), время — в `.timer` (`OnCalendar`).

### ARSENKIN ТОП-10 (парсинг выдачи по ключевым словам)

Прогон всех собранных ключевых слов через инструмент «Выгрузка ТОП-10» arsenkin
(Яндекс + Google), результат пишется в таблицу `serp_result`. Токен — в
Настройках (`/stat/admin`, поле ARSENKIN) или `--token`. Соблюдаются лимиты API
(≤5 задач разом, ≤30 запросов/мин, ретрай 429).
```bash
cd /opt/seostat
# смета (без запуска): сколько фраз и лимитов уйдёт
.venv/bin/python scripts/arsenkin_top.py --domain prokompressor.ru --min-clicks 1
# запуск (в фоне, переживёт консоль); --se "type:region,..." настраивает ПС/регионы
nohup .venv/bin/python scripts/arsenkin_top.py --domain prokompressor.ru \
  --min-clicks 1 --apply > /tmp/arsenkin.log 2>&1 &
tail -f /tmp/arsenkin.log
```
`--domain all` — по всем сайтам; `--file kw.txt` — взять фразы из файла; `--depth`,
`--batch`, `--parallel` (≤5), `--rpm` (≤30) — тонкая настройка.


## Обзвон для продажников (отдельный сервис)

Страницы обзвона (`/obzvon/kc`, `/obzvon/meyer`) живут в **отдельном процессе**
`app.obzvon` со **своими паролями** — продажники не могут попасть в основной
`/stat` (другой процесс, других роутов там просто нет), а пароли обзвона никак
не связаны с паролем статистики.

1. Пароли в `/opt/seostat/.env` (пары `логин:пароль` через запятую):
```
OBZVON_USERS=vasya:пароль1,petya:пароль2
```
2. Сервис:
```bash
cp deploy/seostat-obzvon.service /etc/systemd/system/seostat-obzvon.service
systemctl daemon-reload
systemctl enable --now seostat-obzvon
systemctl status seostat-obzvon --no-pager     # active (running), порт 8012
```
3. nginx: добавьте блоки из `deploy/nginx-obzvon.conf` в тот же `server { … }`,
   где уже есть `/stat`, затем:
```bash
nginx -t && systemctl reload nginx
```
4. Проверка: `https://parsercompressor.online/obzvon/kc` — браузер спросит
   логин/пароль из `OBZVON_USERS`. Загрузите xlsx с приоритетами — очередь готова.

Смена/добавление паролей: правите `OBZVON_USERS` в `.env` →
`systemctl restart seostat-obzvon` (основной сервис перезапускать не нужно).

## Обновление версии
```bash
cd /opt/seostat && git pull && .venv/bin/pip install -r requirements.txt
systemctl restart seostat
systemctl restart seostat-obzvon   # если настроен обзвон
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
