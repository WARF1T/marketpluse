# Релей для обхода 403 из Cloudflare Workers

1. Зайди на https://dash.deno.com → New Playground, вставь `main.ts`, сохрани и задеплой.
2. В настройках проекта Deno добавь переменную `RELAY_KEY` (любой длинный пароль).
3. В Cloudflare → Worker → Settings → Variables and Secrets добавь:
   - `WFM_API_BASE` = `https://<твой-проект>.deno.dev` (без слеша в конце)
   - `WFM_RELAY_KEY` (Secret) = тот же пароль.
4. Проверь `/api/health?deep=1`: должно быть `"upstream":"relay"` и `"marketStatus":"catalog-available"`.
