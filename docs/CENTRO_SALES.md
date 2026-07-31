# Объединённая база «Центробежные»

## Назначение

Приложение `/obzvon/centro` — персональная очередь двух продавцов по предприятиям с центробежным компрессорным оборудованием.

Данные разделены на два файла:

- `centrifugal.db` — заменяемый снимок исходных данных, собранный из CSV;
- `centro_sales.db` — пользователи, назначения, результаты звонков, комментарии и журнал действий.

Замена исходного снимка не удаляет работу продавцов.

## CSV — источник истины

Снимок собирается из трёх файлов:

1. `SVOD375OBEDINENNYY.csv` — одна сводная строка на предприятие;
2. `POLNYY375vsyainformaciya.csv` — подробные факты, новости, люди, номера и карточки;
3. `BAZA-CENTROBEZHNIKI-OBSHCHAYA.csv` — дополнительная база людей и контактов.

Сборщик записывает в `import_info` имена файлов, SHA-256, время сборки и количество загруженных сущностей. Администратор видит эти сведения внизу карточки и может проверить, какая именно база подключена.

## Что хранится в `centro_sales.db`

- `users` — логин, bcrypt-хеш, роль и активность;
- `company_assignment` — постоянное назначение компании, score и признаки качества контактов;
- `company_state` — результат звонка, последний и следующий контакт;
- `company_comment` — комментарии до 5000 символов с авторством;
- `activity_log` — журнал сохранений, правок и переназначений.

## Сборка тестовой базы

```powershell
Set-Location C:\seostat-centro-review

C:\seostat\.venv\Scripts\python.exe -m app.tools.build_centro_db `
  --summary "C:\seostat-centro-review\input\SVOD375OBEDINENNYY.csv" `
  --details "C:\seostat-centro-review\input\POLNYY375vsyainformaciya.csv" `
  --contacts "C:\seostat-centro-review\input\BAZA-CENTROBEZHNIKI-OBSHCHAYA.csv" `
  --output "C:\seostat-centro-review\data\centrifugal-test.db"
```

Ожидаемые показатели для версии от 31.07.2026:

- 375 компаний;
- 25 861 факт;
- 28 новостей;
- 370 строк людей;
- контакты формируются из сводной базы, полной детализации и дополнительного CSV.

## Переменные тестового запуска

```powershell
$env:PYTHONPATH = "C:\seostat-centro-review"
$env:CENTRIFUGAL_DB = "C:\seostat-centro-review\data\centrifugal-test.db"
$env:CENTRO_SALES_DB = "C:\seostat-centro-review\data\centro_sales-test.db"
$env:CENTRO_SESSION_SECRET = C:\seostat\.venv\Scripts\python.exe -c `
  "import secrets; print(secrets.token_urlsafe(48))"
```

## Проверка происхождения базы

```powershell
C:\seostat\.venv\Scripts\python.exe -c `
  "from app.services import centro_catalog as c; print(c.database_info())"
```

В выводе должны быть `summary_file`, `details_file`, `contacts_file`, их SHA-256 и `source_kind: csv-import-v2`.

## Интерфейс

В карточке показываются:

- контакты с установленными ролями — сразу;
- контакты без роли или без установленного владельца — в свёрнутом блоке;
- люди и технические ЛПР;
- реквизиты, руководство, деньги и ОКВЭД;
- оборудование по ОКВЭД;
- причина звонка;
- новости с датой и ссылкой;
- подробные факты по оборудованию: модель, тип, среда, состояние, дата, доказательство, цитата и первоисточник;
- источники проверки компании;
- результаты звонков, комментарии и личная очередь.

Расширенные фильтры прокручиваются и включают регион, ОКВЭД, статус ЕГРЮЛ, тип оборудования, модель/марку, рабочую среду, состояние, выручку, приоритет, наличие телефонов, закупщиков, технических ЛПР, новостей и моделей.

## Распределение

Новые ИНН распределяются только между активными пользователями роли `sales`. Уже назначенные компании не перераспределяются автоматически. Ручное переназначение доступно администратору; текущий результат звонка сохраняется.

## Проверки перед запуском

```powershell
C:\seostat\.venv\Scripts\python.exe -m compileall `
  C:\seostat-centro-review\app `
  C:\seostat-centro-review\tests

C:\seostat\.venv\Scripts\python.exe -m pytest `
  C:\seostat-centro-review\tests\test_centro_sales.py `
  C:\seostat-centro-review\tests\test_centro_import.py `
  -q
```

После этого приложение запускается на отдельном тестовом порту, например `8014`. Производственный сервис и файл `C:\seostat\data\centrifugal.db` до отдельного согласования не менять.
