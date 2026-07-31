# Рабочая база «Центробежные»

## Архитектура

Интерфейс `/obzvon/centro` объединяет строки `centro1` и `centro2` по
нормализованному ИНН. Источник `centrifugal.db` остаётся заменяемым снимком.
Рабочие данные продажников хранятся отдельно в `data/centro_sales.db`, поэтому
обновление исходной базы не удаляет назначения, результаты звонков и
комментарии.

Старые адреса `/obzvon/centro1` и `/obzvon/centro2` перенаправляются в единую
очередь. Базы `kc` и `meyer` продолжают использовать HTTP Basic. Centro имеет
собственных пользователей `admin`/`sales` и подписанную HttpOnly cookie
(SameSite=Lax, Secure при HTTPS, срок восемь часов).

## Что хранится в `centro_sales.db`

- `users` — логин, bcrypt-хеш, роль и активность;
- `company_assignment` — постоянное назначение компании, score и признаки
  качества контактов;
- `company_state` — результат звонка, последний и следующий контакт;
- `company_comment` — комментарии до 5000 символов с авторством;
- `activity_log` — журнал сохранений, правок и переназначений.

Схема создаётся и обновляется идемпотентно. При переходе со старой версией
недостающие поля распределения добавляются автоматически.

## Распределение

Новые ИНН распределяются только между активными пользователями роли `sales`.
Уже назначенные компании не перераспределяются автоматически. Алгоритм
учитывает количество компаний, сумму score, наличие телефона, закупщика,
технического ЛПР и новостного повода. Ручное переназначение доступно admin;
текущий результат звонка переносится новому ответственному.

## Очередь и фильтры

После сохранения обработанная компания уходит ниже новых компаний, поэтому
кнопка «Сохранить и следующая» открывает следующую приоритетную карточку.
Доступны:

- полнотекстовый поиск, регион и ответственный сотрудник;
- результат звонка;
- наличие телефона, закупщика, технического ЛПР и новостного повода;
- ОКВЭД, тип оборудования, марка и статус ЕГРЮЛ;
- диапазон выручки и минимальный приоритет.

Фильтры сохраняются при переходе между карточками и страницами очереди.

## Первичная настройка Windows-сервера

Установить зависимости:

```powershell
C:\seostat\.venv\Scripts\python.exe -m pip install -r C:\seostat\requirements.txt
```

Добавить в `C:\seostat\.env`:

```dotenv
CENTRIFUGAL_DB=C:\seostat\data\centrifugal.db
CENTRO_SALES_DB=C:\seostat\data\centro_sales.db
CENTRO_SESSION_SECRET=<случайная строка минимум 32 байта>
```

Создать секрет локально:

```powershell
C:\seostat\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

Без корректного `CENTRO_SESSION_SECRET` вход блокируется ответом 503 — это
намеренный fail-closed режим.

Создать пользователей; пароль вводится через `getpass` и не попадает в историю
PowerShell:

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

## Проверка перед развёртыванием

В отдельной копии проекта:

```powershell
cd C:\seostat-centro-review
C:\seostat\.venv\Scripts\python.exe -m pip install -r requirements.txt
C:\seostat\.venv\Scripts\python.exe -m pytest tests\test_centro_sales.py -q
C:\seostat\.venv\Scripts\python.exe -m compileall app
```

После успешной проверки рабочий проект обновляется отдельно. Служба остаётся:

```text
python -m uvicorn app.obzvon:app --host 127.0.0.1 --port 8012
```

Caddy менять не требуется.
