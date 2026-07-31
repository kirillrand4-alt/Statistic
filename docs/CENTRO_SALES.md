# Рабочая база «Центробежные»

## Архитектура

Интерфейс `/obzvon/centro` объединяет строки источников `centro1` и `centro2`
по нормализованному ИНН. `centrifugal.db` открывается только в режиме чтения и
остаётся заменяемым снимком. Контакты загружаются отдельным индексируемым
запросом только для открытой карточки и дедуплицируются по нормализованному
номеру. Рабочие данные находятся в отдельной `data/centro_sales.db`.

Старые адреса `/obzvon/centro1` и `/obzvon/centro2` перенаправляют в общую
очередь. Старые базы `kc` и `meyer` продолжают использовать Basic Auth, тогда
как Centro использует собственных пользователей, роли и подписанную HttpOnly
cookie (SameSite=Lax, Secure при HTTPS, восемь часов).

## Схема `centro_sales.db`

* `users` — уникальный логин, bcrypt-хеш, роль `admin`/`sales`, активность и даты;
* `company_assignment` — единственное неизменяемое назначение на ИНН, score,
  версия источника, дата и автор назначения;
* `company_state` — пользовательский результат, статус, последний и следующий
  контакт; первичный ключ `(inn, username)`;
* `company_comment` — комментарии до 5000 символов и их авторы;
* `activity_log` — неизменяемый журнал операций с JSON payload.

Внешние ключи ведут на `users.username`; индексы покрывают очередь пользователя,
статус, следующий контакт, историю комментариев и журнал. Схема создаётся
идемпотентно при запуске приложения или CLI.

## Первичное распределение

1. Компании дедуплицируются по ИНН; уже назначенные ИНН исключаются.
2. Основа score — `moy_prioritet`, при отсутствии `rank_metric`. Добавляются
   важность покупки, доступность контакта, телефон, закупщик, технический ЛПР,
   новость и факты. Ликвидированные/банкротные получают штраф очередности.
3. Записи делятся на десятибалльные диапазоны и воспроизводимо перемешиваются
   с фиксированным seed и хешем ИНН.
4. Жадный балансировщик сравнивает количество, сумму score и четыре контактных
   признака. Разница количества не превышает единицу.
5. Назначения записываются один раз. Повторный запуск/импорт ничего не меняет;
   распределяются исключительно новые ИНН. Ручное назначение доступно admin.

## Первичная настройка

Установить зависимости:

```powershell
C:\seostat\.venv\Scripts\python.exe -m pip install -r C:\seostat\requirements.txt
```

В `.env` (значения не публиковать):

```dotenv
CENTRIFUGAL_DB=C:\seostat\data\centrifugal.db
CENTRO_SALES_DB=C:\seostat\data\centro_sales.db
CENTRO_SESSION_SECRET=<случайная строка минимум 48 байт>
```

Секрет можно создать локально командой
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Создание пользователей (пароль дважды запрашивается через `getpass` и не
попадает в историю PowerShell):

```powershell
cd C:\seostat
C:\seostat\.venv\Scripts\python.exe -m app.tools.centro_users create --username admin --role admin
C:\seostat\.venv\Scripts\python.exe -m app.tools.centro_users create --username user1 --role sales
C:\seostat\.venv\Scripts\python.exe -m app.tools.centro_users create --username user2 --role sales
```

Смена пароля:

```powershell
C:\seostat\.venv\Scripts\python.exe -m app.tools.centro_users set-password --username user1
```

## Тестирование и обновление Windows-службы

```powershell
cd C:\seostat
C:\seostat\.venv\Scripts\python.exe -m pytest tests\test_centro_sales.py
C:\seostat\.venv\Scripts\python.exe -m pytest
```

Обновление выполняет администратор сервера (данный репозиторий сам деплой не
выполняет):

```powershell
cd C:\seostat
git fetch origin
git checkout <ветка-или-тег-релиза>
git pull --ff-only
C:\seostat\.venv\Scripts\python.exe -m pip install -r requirements.txt
C:\seostat\.venv\Scripts\python.exe -m pytest tests\test_centro_sales.py
Restart-Service obzvon
Get-Service obzvon
```

Команда службы остаётся
`python -m uvicorn app.obzvon:app --host 127.0.0.1 --port 8012`. Caddy менять
не требуется.
