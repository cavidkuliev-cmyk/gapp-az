# Gapp.az

Агрегатор новостей азербайджанских изданий на русском языке. Сборщик читает RSS-ленты, убирает повторы, объединяет новости об одном событии в сюжет, а сайт показывает ленту и ведёт читателя на сайт источника.

## Как это устроено

- `gapp/collector.py` опрашивает ленты из `sources.yaml` раз в 3 минуты, берёт только русскоязычные материалы, определяет рубрику и складывает всё в SQLite.
- `gapp/web.py` отдаёт готовый HTML без клиентского JavaScript: главная, рубрики, страница сюжета, поиск, `sitemap.xml`, `news-sitemap.xml`, `rss.xml`, `robots.txt`.
- Повторы отсекаются в три шага: канонический адрес, хеш заголовка внутри источника, сходство заголовков между источниками за 48 часов.
- Страница сюжета открыта для индексации, когда о событии написали минимум два издания. Остальные получают `noindex, follow`.

## Запуск на своём компьютере

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m gapp.check_feeds        # проверить, какие ленты отвечают
python -m gapp.collector --once   # один проход сборщика
uvicorn gapp.web:app --reload     # сайт на http://127.0.0.1:8000
pytest -q
```

## Источники

Список лежит в `sources.yaml`. Адреса лент нужно проверить командой `python -m gapp.check_feeds`: она покажет по каждой ленте статус и число записей. Неработающие адреса исправьте или поставьте источнику `enabled: false`.

## Запуск на сервере

Нужен сервер с Docker и домен, записи A для `gapp.az` и `www.gapp.az` указывают на сервер.

```bash
git clone https://github.com/cavidkuliev-cmyk/gapp-az && cd gapp-az
docker compose up -d --build
docker compose exec collector python -m gapp.check_feeds
```

Caddy сам получит сертификат HTTPS. Для другого домена задайте `GAPP_DOMAIN=example.com` в файле `.env`. База лежит в томе `data`.

## Настройки

Переменные окружения: `GAPP_DB`, `GAPP_SOURCES`, `GAPP_SITE_URL`, `GAPP_USER_AGENT`, `GAPP_POLL_INTERVAL`, `GAPP_STORY_WINDOW_HOURS`, `GAPP_STORY_SIMILARITY`, `GAPP_INDEX_MIN_SOURCES`. Значения по умолчанию смотрите в `gapp/config.py`.
