// Мини-релей для warframe.market API. Деплоить НЕ на Cloudflare (Deno Deploy, VPS, Render и т.п.),
// чтобы запросы шли не с IP Cloudflare Workers, которые warframe.market блокирует (HTTP 403).
// Переменная окружения RELAY_KEY — тот же пароль, что WFM_RELAY_KEY в Worker'е.
const UPSTREAM = "https://api.warframe.market";
const KEY = Deno.env.get("RELAY_KEY") ?? "";
const PASS = ["accept", "platform", "language", "user-agent"];

Deno.serve(async (req) => {
  const url = new URL(req.url);
  if (req.method !== "GET") return new Response("method not allowed", { status: 405 });
  if (!KEY || req.headers.get("x-relay-key") !== KEY) return new Response("forbidden", { status: 403 });
  if (!/^\/v[12]\//.test(url.pathname)) return new Response("not found", { status: 404 });
  const headers = new Headers();
  for (const h of PASS) { const v = req.headers.get(h); if (v) headers.set(h, v); }
  const r = await fetch(UPSTREAM + url.pathname + url.search, { headers });
  return new Response(r.body, { status: r.status, headers: { "content-type": r.headers.get("content-type") ?? "application/json" } });
});
