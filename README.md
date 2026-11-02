# MarketPulse на Cloudflare Workers

Один Worker отдаёт и сайт (`public/`), и API (`src/worker.js`). Render больше не нужен: Worker не засыпает и не требует пингов.

## Структура

| Путь | Что это |
|---|---|
| `wrangler.jsonc` | настройки Worker'а |
| `src/worker.js` | API: прокси к warframe.market, кэш, ИИ, алерты Telegram |
| `public/` | сайт (`index.html`, `config.js`, иконка, манифест) |

## Секреты (Settings → Variables and Secrets → Add → тип Secret)

| Имя | Зачем |
|---|---|
| `GEMINI_API_KEY` | «Спросить ИИ» (ключ из aistudio.google.com/apikey) |
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | твой chat id |
| `ALERTS_KEY` | любой пароль от 8 символов, его вводишь на сайте |

Telegram дополнительно требует KV-привязку `ALERTS_KV` (см. `wrangler.jsonc`).

## Проверка

- `/api/health`: сервер жив
- `/api/health?deep=1&ai=1`: проверка warframe.market, картинок и ключа Google

## Локально (на ПК, нужен Node.js)

```
npm install
echo GEMINI_API_KEY=твой_ключ > .dev.vars
npx wrangler dev
```
