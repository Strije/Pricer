# CLAUDE.md — карта проекта Pricer

Веб-версия десктопной «Проценки» (C:\Price, PyQt6) для автозапчастей: поиск цен у поставщиков,
заказы, статусы из кабинетов, подборы для клиентов с оплатой, прайс-листы. Цель — бесплатный
аналог QWEP для многих организаций. Подробный план и история решений — `docs/MIGRATION_PLAN.md`
(текущий план — раздел 15).

## ⚠️ Правила, которые нельзя нарушать

- **Репозиторий публичный.** Никогда не коммитить `settings.json`, ключи, пароли, сырые записи
  ответов поставщиков, `backend/var/`, `secret.key`, `.env`. Ключи вводятся только в веб-интерфейсе
  и хранятся в базе зашифрованными (Fernet, `app/security.py`). В тестах — только вымышленные данные
  и обезличенные записи (`app/redact.py`).
- **`backend/core/engine.py` не править руками.** Он генерируется `tools/gen_engine.py` из `main.py`
  десктопа (исходники десктопа в репозиторий не входят). Изменения поиска — в генераторе или
  в `app/search.py`.
- **`backend/core/` — копия модулей десктопа**: менять минимально и точечно, логику не переписывать.
- **Всё изолировано по `organization_id`.** Любой запрос к базе фильтруется по организации
  текущего пользователя; чужой объект — 404.
- **Роль `customer`** (покупатель витрины) не видит закупочных цен, поставщиков и наценок
  (`search.offer_view(..., customer=True)`, `quotes.public_view`).
- **CSRF:** изменяющие запросы требуют заголовок `X-Requested-With: pricer` (в тестах `H`);
  исключение — `/api/yookassa/webhook`.
- **Адрес HTTP-уведомлений ЮKassa не менять** — он занят сайтом ABCP; оплаты проверяются опросом.
- Почта (IMAP) — только чтение: `readonly=True`, `BODY.PEEK`.
- Схема базы: `db.add_missing_columns` только добавляет столбцы (Alembic ещё не введён).
  Не переименовывать и не удалять столбцы.
- Один процесс uvicorn (фоновые потоки и блокировки живут в процессе).
- Комментарии, сообщения и тексты интерфейса — на русском. Интерфейс должен работать на телефоне
  и планшете.
- Каждое изменение — с тестами; `python -m pytest` в `backend` должен быть зелёным (SQLite;
  CI гоняет ещё и PostgreSQL).

## Запуск

```bash
cd backend
pip install -e ".[dev]"            # или pip install -r requirements.txt
python -m pytest -q                # ~210 тестов, без сети
PRICER_REPLAY=1 uvicorn app.server:app --port 8000   # демо на записанных ответах
uvicorn app.server:app --port 8000                   # настоящие поставщики
python -m app.manage list | create-org | reset-password   # управление организациями
python tools/gen_engine.py <путь к main.py десктопа>       # пересобрать engine.py (из корня)
```

Сервер: `deploy/setup.sh` (`install [адрес] [email]`, `update`, `create-org`, `reset-password`,
`backup`, `restore`, `status`), подробно — `deploy/README.md`. Сейчас: VPS 109.73.199.217,
https://109-73-199-217.sslip.io, nginx → 127.0.0.1:8095, systemd `pricer.service`, PostgreSQL 16,
сторож с Telegram (`pricer-watchdog.timer`), копии в `/var/backups/pricer`.

### Переменные окружения

| Переменная | Назначение |
|---|---|
| `PRICER_DATABASE_URL` | база (по умолчанию SQLite `var/pricer.db`; на сервере `postgresql+psycopg://…`) |
| `PRICER_VAR_DIR` | папка данных (`backend/var`) |
| `PRICER_SECRET_KEY` | ключ Fernet для секретов; без него — `var/secret.key`. **Потеря = потеря паролей** |
| `PRICER_DATA_DIR` | справочник брендов (по умолчанию `var/brands`) |
| `PRICER_PUBLIC_URL` | внешний адрес (ссылки на подборы, возврат после оплаты) |
| `PRICER_SECURE_COOKIES` | cookie только по HTTPS |
| `PRICER_ALLOW_SIGNUP` | открытая регистрация организаций |
| `PRICER_STATUS_REFRESH_MINUTES` | статусы из кабинетов (30) |
| `PRICER_PAYMENT_CHECK_SECONDS` | опрос оплат ЮKassa (120) |
| `PRICER_PRICE_CHECK_SECONDS` | проверка расписания прайсов (60) |
| `PRICER_REPLAY` | демо-режим на записях `tests/fixtures` |
| `PRICER_DOCS` | `1` — включить `/docs` |

## Карта папок

```
backend/
  app/                      веб-приложение (FastAPI)
    server.py               create_app(): все эндпоинты, build_engine, фоновые потоки (~2300 строк)
    db.py                   модели SQLAlchemy 2 и add_missing_columns
    security.py             пароли (scrypt), SecretBox (Fernet), LoginLimiter
    search.py               выдача: offer_view, бренд голосованием (choose_brand), гарантии, подсветки
    orders.py               заказы: черновик → перепроверка → отправка, журнал
    supplier_lines.py       сопоставление позиций с кабинетами поставщиков, статусы, отказы
    supplier_catalog.py     описание разделов настроек поставщиков/сервисов (поля, секреты)
    quotes.py               «Подбор» для клиента: варианты, ссылка, выбор, оплата
    service_template.py     «ТО по машине»: шаблон работ → OEM-номера → варианты
    prices.py               прайс-листы: источники, разметка колонок, загрузка, PriceDbProvider
    clients.py              клиенты организации
    replay.py               демо-перехватчик HTTP (Replayer); 127.0.0.1 пропускает
    redact.py               обезличивание записей
    manage.py               CLI: create-org, reset-password, list
    static/                 интерфейс без сборщика
      index.html            вкладки, вёрстка, встроенный JS (заказы, корзина, поставщики, VIN, ТО)
      search.js             поиск и выдача (группировка, фильтры брендов, избранное)
      quotes.js             вкладка «Подборы»
      settings.js           «Настройки»: сервисы, почтовые ящики, прайсы
      quote.html            страница клиента по ссылке: печать/PDF, оплата
  core/                     модули десктопа без Qt (поставщики, нормализация, бренды, кроссы)
    engine.py               ГЕНЕРИРУЕТСЯ из main.py — не править
    url_csv_provider.py     чтение CSV/XLSX/ZIP прайсов (используется prices.py)
    armtek.py rossko.py favorit.py forum_auto.py mikado.py abcp_supplier.py … — адаптеры поставщиков
  integrations/
    supplier_orders.py      история заказов из кабинетов (Armtek, Rossko, Favorit, Forum, Avtoto,
                            ABSTD, ABCP, PR-LG, Mikado); FETCHERS, fetcher_for
    yookassa.py             клиент ЮKassa API v3 (платёж, чек 54-ФЗ, /me)
    mail_prices.py          IMAP: письмо по правилу → вложение-прайс
    laximo.py               подбор по VIN/FRAME/госномеру
    onec_odata.py, outbox.py  выгрузка в 1С через OData (заготовка, очередь)
  data/                     справочники брендов и гарантий (без секретов)
  tests/                    pytest; fixtures/ — обезличенные записи ответов (*.jsonl.gz)
  var/                      база, ключ, кэши — в .gitignore
tools/
  gen_engine.py             генератор core/engine.py из main.py десктопа
  record_provider_responses.py  запись ответов поставщиков для тестов (с обезличиванием)
  callgraph.py              граф вызовов main.py (для генератора)
deploy/                     setup.sh, pricer.service, nginx-pricer.conf, watchdog, pricer.env.example
docs/MIGRATION_PLAN.md      план, решения, статус по этапам
.github/workflows/          backend-tests.yml: pytest на SQLite и PostgreSQL
```

## Основные потоки

- **Поиск.** `POST /api/find` → задача (`Job`) → SSE `/api/jobs/{id}/events` → `/api/find/{id}/results`.
  Строка распознаётся как артикул / госномер / VIN / FRAME. Бренд выбирается голосованием поставщиков
  (`engine.search_brands` → `search.choose_brand`, порог в настройках организации), затем
  `engine.search_offers`. Движок организации — `build_engine` (поставщики из `SupplierAccount`,
  `UrlCsvProvider` заменён на `PriceDbProvider`), кэшируется в `account_engine`.
- **Заказы.** Корзина → `orders.py` → отправка поставщикам (метка `ORD-…` в начале комментария,
  `engine.supplier_comment`) → журнал.
- **Статусы.** Фоновый поток каждые 30 мин: `supplier_lines.refresh_from_supplier` тянет историю
  через `integrations/supplier_orders.py`, сопоставляет позиции, создаёт `Notification` об отказах.
- **Подборы.** `quotes.py`: менеджер собирает варианты → ссылка `/q/{token}` → клиент выбирает →
  оплата ЮKassa (`/pay`) → поток `payment_watch` (раз в 120 с) проверяет платежи.
- **Прайсы.** `PriceSource` (url / ftp / email) с расписанием → `price_watch` (раз в 60 с) →
  `prices.load` → строки в `PriceRow` (индекс `organization_id, article_key`). Старше 2 дней —
  предупреждение, старше 7 — не в выдаче.

## Эндпоинты (кратко)

- вход: `/api/auth/*` (register, login, password, logout), `/health`
- поиск: `/api/find`, `/api/jobs/{id}/events` (SSE), `/api/find/{id}/results`, `/api/stats/popular`,
  `/api/me/favorites`, `/api/brands/warranty`; VIN/Laximo: `/api/vin/find|groups|details`
- поставщики и сервисы: `/api/suppliers*`, `/api/suppliers/{id}/check`, `/api/services/{section}/check`,
  `/api/services/mailbox/{id}/check`, `/api/import/settings`
- прайсы: `/api/prices` (CRUD), `/{id}/preview`, `/{id}/load`, `/api/prices/import-desktop`
- заказы: `/api/orders*` (recheck, submit-preview, submit, replace/skip/restore, log),
  `/api/order-file/parse|search`, корзина `/api/cart*` (`/checkout`)
- кабинеты поставщиков: `/api/supplier-lines*` (`/refresh`, `/{id}/status`), `/api/supplier-stats`
- клиенты и машины: `/api/clients*`, `/api/vehicles/{id}`; настройки организации `/api/org/settings`
- подборы: `/api/quotes*`, `/api/quotes/service`, `/q/{token}`, `/api/public/quote/{token}`
  (`/pay`, `/choose`), `/api/quotes/{id}/send|order|lines|variants`, `/api/yookassa/webhook`
- уведомления: `/api/notifications*`
- старый `/api/search` + `/api/search/{id}/events` — к удалению

## Тесты

- `tests/test_server.py` — фикстуры `app_factory`, `register`, заголовок `H`, `read_events` для SSE.
- Записи ответов поставщиков воспроизводятся `tests/replay.py` / `app/replay.py`; новые записи —
  только через `tools/record_provider_responses.py` (обезличивание обязательно, проверить глазами).
- Внешние сервисы (ЮKassa, IMAP, Laximo, 1С) в тестах подменяются.

## Известный техдолг

- `server.py` и `index.html` слишком большие — разбить на роутеры и JS-модули.
- Нет Alembic; связь части данных хранится в JSON-полях.
- Большие прайсы загружаются в память целиком (на сервере `MemoryMax=900M`) — нужна потоковая загрузка.
- XLS и RAR во вложениях не читаются (понятная ошибка).
- Ставка НДС 22% для чека ЮKassa не подтверждена (в интерфейсе пометка «сверьте»).
