# MarketPulse API

Сервер данных для сайта MarketPulse: прокси к warframe.market, кэш, история цен, ИИ-подсказки. Только стандартная библиотека Python.

## Запуск на Render (бесплатный тариф)

- Language: Python 3
- Build Command: `echo ok`
- Start Command: `python server.py`
- Instance Type: Free
- Environment: `GEMINI_API_KEY` (если нужен ИИ), позже `ALLOWED_ORIGIN` = адрес сайта, например `https://имя.pages.dev`

Проверка: откройте `/api/health` у адреса сервиса.

Локально: `python server.py` (порт 4173, переменная `PORT`).
