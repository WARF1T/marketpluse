// MarketPulse API на Cloudflare Workers (порт server.py).
// Один файл без импортов: работает и через wrangler/GitHub, и если вставить его в редактор в панели Cloudflare.
//
// Настройки (Settings → Variables and Secrets или wrangler.jsonc → vars):
//   GEMINI_API_KEY (секрет)        ключ для «Спросить ИИ»
//   GEMINI_MODEL                   по умолчанию gemini-3.8-flash
//   GEMINI_FALLBACK_MODEL          по умолчанию gemini-2.5-flash-lite, пустая строка отключает
//   GEMINI_THINKING_LEVEL          по умолчанию low
//   ALLOWED_ORIGIN                 адреса сайта через запятую (нужно, только если сайт на другом домене)
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERTS_KEY (секреты) + KV-привязка ALERTS_KV + Cron Trigger — алерты в Telegram

const API_HOST = 'https://api.warframe.market';
const IMG_BASE = 'https://warframe.market/static/assets/';
const UA = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36';
const PLATFORMS = ['pc', 'ps4', 'xbox', 'switch'];
const SLUG_RE = /^[a-z0-9_]{1,80}$/;
const START_TIME = Date.now();

const CACHE_MAX_SIZE = 1500;
const ERROR_CACHE_TTL = 15;
const STALE_MAX_AGE = 900;
const AI_CACHE_TTL = 300;
const AI_RATE_LIMIT_PER_MINUTE = 10;
const AI_RATE_LIMIT_PER_IP_PER_MINUTE = 5;
const UPSTREAM_GAP = 340;
const UPSTREAM_MAX_WAIT = 5000;
const UPSTREAM_LOW_WAIT = 4000;
const IP_BUDGET = 120;
const IP_WINDOW = 60000;
const TG_MAX_CHECKS_PER_RUN = 40; // бесплатный тариф: до 50 подзапросов за один запуск

// Память живёт, пока жив экземпляр Worker'а (минуты-часы). Это кэш, а не хранилище.
const CACHE = new Map();
const ERROR_CACHE = new Map();
const INFLIGHT = new Map();
const AI_CACHE = new Map();
const IP_HITS = new Map();
const AI_CALLS = [];
const AI_BY_IP = new Map();
let UPSTREAM_NEXT = 0;
let ITEMS_MEM = null; // {at, items, public}
let ITEMS_INFLIGHT = null;
let PROBE_STATE = { at: 0, aiAt: 0, result: null };

class HttpError extends Error {
  constructor(status, message, local = false) {
    super(message || `HTTP ${status}`);
    this.status = status;
    this.local = local;
  }
}

// ---------- утилиты ----------
const now = () => Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const norm = (s) => String(s || '').toLowerCase().replace(/ё/g, 'е').trim();
const words = (s) => String(s).split(/\s+/).filter(Boolean);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const round1 = (v) => Math.round(v * 10) / 10;

function number(value, def = 0) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return def;
  const r = Number(value);
  return Number.isFinite(r) ? r : def;
}
const int = (v, def = 0) => Math.trunc(number(v, def));

function pick(d, keys, def = '') {
  if (!isObj(d)) return def;
  for (const k of keys) if (d[k] !== null && d[k] !== undefined) return d[k];
  return def;
}

function lruSet(map, key, value, max = CACHE_MAX_SIZE) {
  map.delete(key);
  map.set(key, value);
  while (map.size > max) map.delete(map.keys().next().value);
}

function extractList(data, names) {
  if (Array.isArray(data)) return data;
  if (isObj(data)) {
    for (const c of [data, data.payload, data.data, data.result]) {
      if (isObj(c)) {
        for (const n of names) if (Array.isArray(c[n])) return c[n];
      } else if (Array.isArray(c)) return c;
    }
  }
  return [];
}

function extractOrders(raw) {
  if (Array.isArray(raw)) return raw;
  if (!isObj(raw)) return [];
  const data = raw.data;
  if (Array.isArray(data)) return data;
  const source = isObj(data) ? data : raw;
  for (const key of ['orders', 'results', 'list']) if (Array.isArray(source[key])) return source[key];
  if (Array.isArray(source.sell) && Array.isArray(source.buy)) {
    const out = [];
    for (const o of source.sell) if (isObj(o)) out.push({ order_type: 'sell', ...o });
    for (const o of source.buy) if (isObj(o)) out.push({ order_type: 'buy', ...o });
    return out;
  }
  return extractList(raw, ['orders', 'results', 'list']);
}

function extractSingle(raw) {
  if (!isObj(raw)) return {};
  for (const k of ['data', 'payload', 'result']) if (isObj(raw[k])) return raw[k];
  return raw;
}

function friendly(e) {
  if (e && e.status) return `API warframe.market недоступен (HTTP ${e.status}). Возможна блокировка или даунтайм.`;
  return `Нет соединения с warframe.market: ${e && e.message ? e.message : e}`;
}

function imageUrl(value) {
  if (!value) return '';
  value = String(value).trim();
  if (!value) return '';
  if (/^https?:\/\//.test(value)) return value;
  if (value.startsWith('//')) return 'https:' + value;
  if (value.startsWith('/static/') || value.startsWith('/')) return 'https://warframe.market' + value;
  if (value.startsWith('static/')) return 'https://warframe.market/' + value;
  return IMG_BASE.replace(/\/+$/, '') + '/' + value.replace(/^\/+/, '');
}

function normalizeItem(x) {
  if (!isObj(x)) return ['', '', '', ''];
  const i18n = isObj(x.i18n) ? x.i18n : {};
  const en = isObj(i18n.en) ? i18n.en : {};
  const ru = isObj(i18n.ru) ? i18n.ru : {};
  const uk = isObj(i18n.uk) ? i18n.uk : {};
  const name = en.name || x.name || x.item_name || x.title || '';
  const nameRu = ru.name || uk.name || x.nameRu || name;
  const slug = x.slug || x.url_name || x.url || x.id || '';
  let thumb = '';
  for (const src of [en, ru, uk, x]) {
    thumb = src.thumb || src.thumbnail || src.icon || src.image || '';
    if (thumb) break;
  }
  return [String(name), String(nameRu), String(slug), imageUrl(thumb)];
}

// ---------- поиск с русскими синонимами ----------
const SYNONYMS = {
  glaive: ['глайв'], nikana: ['никана'], volt: ['вольт'], mirage: ['мираж'], nova: ['нова'], trinity: ['тринити'],
  excalibur: ['экскалибур'], rhino: ['рино'], mag: ['маг'], loki: ['локи'], ash: ['эш'], banshee: ['банши'],
  oberon: ['оберон'], hydroid: ['гидроид'], valkyr: ['валькир'], ember: ['эмбер'], frost: ['фрост'],
  prime: ['прайм'], blueprint: ['чертеж', 'бп'], set: ['сет', 'набор'], blade: ['клинок', 'диск'], sword: ['меч'],
  rifle: ['винтовка'], pistol: ['пистолет'], shotgun: ['дробовик'], bow: ['лук'], scythe: ['коса'], hammer: ['молот'],
  staff: ['посох'], systems: ['системы', 'система'], chassis: ['каркас'], neuroptics: ['нейрооптика', 'шлем'], helmet: ['шлем'],
};
const RU_TO_EN = new Map();
for (const [en, rus] of Object.entries(SYNONYMS)) {
  for (const ru of rus) {
    if (!RU_TO_EN.has(norm(ru))) RU_TO_EN.set(norm(ru), new Set());
    RU_TO_EN.get(norm(ru)).add(norm(en));
  }
}

function aliasesForText(text) {
  const ws = new Set(words(norm(text).replace(/[_-]/g, ' ')));
  const out = new Set();
  for (const [en, rus] of Object.entries(SYNONYMS)) if (ws.has(norm(en))) rus.forEach((r) => out.add(norm(r)));
  return out;
}

function expandQuery(q) {
  const qn = norm(q);
  if (!qn) return [];
  const terms = new Set([qn]);
  const parts = words(qn.replace(/[_-]/g, ' '));
  parts.forEach((p) => terms.add(p));
  for (const part of parts) {
    if (RU_TO_EN.has(part)) RU_TO_EN.get(part).forEach((t) => terms.add(t));
    for (const [en, rus] of Object.entries(SYNONYMS)) {
      const enN = norm(en);
      if (enN === part) { terms.add(enN); rus.forEach((r) => terms.add(norm(r))); }
      for (const ru of rus) if (norm(ru) === part) { terms.add(enN); terms.add(norm(ru)); }
    }
  }
  for (const [ru, ens] of RU_TO_EN) if (ru && qn.includes(ru)) ens.forEach((t) => terms.add(t));
  return [...terms].filter(Boolean);
}

function matchItem(item, q) {
  const qn = norm(q);
  if (!qn) return true;
  const hay = item.hay || '';
  if (hay.includes(qn)) return true;
  for (const term of expandQuery(q)) {
    if (term.length < 2) continue;
    if (term.includes(' ') ? hay.includes(term) : item.hayWords.has(term)) return true;
  }
  return false;
}

function searchItems(items, q) {
  const qn = norm(q);
  if (!qn) return [];
  const expanded = expandQuery(qn).filter((t) => t.length >= 3);
  const score = (item) => {
    const name = norm(item.name), ru = norm(item.nameRu);
    if (qn === name || qn === ru) return 0;
    if (name.startsWith(qn) || ru.startsWith(qn)) return 1;
    if (name.includes(qn) || ru.includes(qn)) return 2;
    if (expanded.some((t) => name.includes(t) || ru.includes(t))) return 3;
    return 4;
  };
  return items.filter((it) => matchItem(it, qn)).map((it) => [score(it), it]).sort((a, b) => a[0] - b[0]).map((x) => x[1]);
}

// ---------- запросы к warframe.market: очередь, бюджет на клиента, кэш, объединение одинаковых ----------
async function upstreamWait(ctx) {
  const t = now();
  if (ctx && ctx.ip) {
    const hits = (IP_HITS.get(ctx.ip) || []).filter((x) => t - x < IP_WINDOW);
    if (hits.length >= IP_BUDGET) { IP_HITS.set(ctx.ip, hits); throw new HttpError(429, 'Per-client upstream budget exceeded', true); }
    hits.push(t);
    IP_HITS.set(ctx.ip, hits);
    if (IP_HITS.size > 2000) for (const [k, v] of IP_HITS) if (!v.length || t - v[v.length - 1] > IP_WINDOW) IP_HITS.delete(k);
  }
  const start = Math.max(t, UPSTREAM_NEXT);
  const wait = start - t;
  if (wait > (ctx && ctx.low ? UPSTREAM_LOW_WAIT : UPSTREAM_MAX_WAIT)) throw new HttpError(429, 'Local queue is full', true);
  UPSTREAM_NEXT = start + UPSTREAM_GAP;
  if (wait > 0) await sleep(wait);
}

function fetchJson(url, ttl = 60, platform = 'pc', language = 'en', ctx = null) {
  const key = `${platform}:${language}:GET:${url}`;
  if (INFLIGHT.has(key)) return INFLIGHT.get(key);
  const p = fetchJsonInner(key, url, ttl, platform, language, ctx).finally(() => INFLIGHT.delete(key));
  INFLIGHT.set(key, p);
  return p;
}

async function fetchJsonInner(key, url, ttl, platform, language, ctx) {
  const t = now();
  const hit = CACHE.get(key);
  if (hit && t - hit.at < ttl * 1000) return hit.data;
  const errHit = ERROR_CACHE.get(key);
  if (errHit && t - errHit.at < ERROR_CACHE_TTL * 1000) {
    if (hit && t - hit.at < STALE_MAX_AGE * 1000) return hit.data;
    throw errHit.error;
  }
  // Не удаляем Platform/Language при ошибке: иначе консольный запрос может получить цены PC.
  const variants = [
    { 'User-Agent': UA, Accept: 'application/json', Platform: platform, Language: language },
  ];
  let last = null;
  for (const headers of variants) {
    try {
      await upstreamWait(ctx);
      const resp = await fetch(url, { headers, signal: AbortSignal.timeout(8000) });
      if (!resp.ok) {
        last = new HttpError(resp.status, `HTTP ${resp.status}`);
        // Повтор с другими заголовками не исправляет запрет доступа.
        break;
      }
      const data = await resp.json();
      lruSet(CACHE, key, { at: now(), data });
      ERROR_CACHE.delete(key);
      return data;
    } catch (e) {
      last = e;
      if (e instanceof HttpError && e.local) break;
    }
  }
  if (last instanceof HttpError && [403, 429].includes(last.status) && !last.local) lruSet(ERROR_CACHE, key, { at: now(), error: last });
  const stale = CACHE.get(key);
  if (stale && now() - stale.at < STALE_MAX_AGE * 1000) return stale.data;
  throw last || new Error('нет соединения');
}

const isRateLimited = (e) => !!e && e.status === 429;
const failCode = (e) => (isRateLimited(e) ? 429 : 502);

// ---------- каталог ----------
function publicItem(item) {
  const tags = Array.isArray(item.tags) ? item.tags : [];
  return {
    name: String(item.name || ''), nameRu: String(item.nameRu || ''), slug: String(item.slug || ''),
    thumb: String(item.thumb || ''), aliases: Array.isArray(item.aliases) ? item.aliases : [], tags,
    prime: tags.some((t) => String(t).toLowerCase() === 'prime'), ducats: number(item.ducats, 0),
  };
}

function buildCatalog(rawItems, ruBySlug) {
  const out = [];
  const seen = new Set();
  for (const x of rawItems) {
    let [name, nameRu, slug, thumb] = normalizeItem(x);
    if (!name || !slug || seen.has(slug)) continue;
    seen.add(slug);
    nameRu = (ruBySlug && ruBySlug.get(slug)) || nameRu;
    out.push({ name, nameRu, slug, thumb, aliases: [...aliasesForText(`${name} ${nameRu} ${slug}`)].sort(),
      tags: Array.isArray(x.tags) ? x.tags : [], ducats: x.ducats || 0 });
  }
  return out;
}

function withHay(items) {
  for (const it of items) {
    it.hay = norm([it.name, it.nameRu, it.slug.replace(/[_-]/g, ' '), it.aliases.join(' ')].join(' | '));
    it.hayWords = new Set(words(it.hay.replace(/\|/g, ' ')));
  }
  return items;
}

const CATALOG_CACHE_URL = 'https://marketpulse.internal/catalog/v1';

async function getItems(ctx) {
  if (ITEMS_MEM && now() - ITEMS_MEM.at < 600000) return ITEMS_MEM;
  if (ITEMS_INFLIGHT) return ITEMS_INFLIGHT;
  ITEMS_INFLIGHT = (async () => {
    // 1) общий кэш дата-центра: компактный каталог, его дешевле разобрать, чем полный ответ API
    let items = null;
    try {
      const cached = await caches.default.match(CATALOG_CACHE_URL);
      if (cached) items = await cached.json();
    } catch (e) { /* Cache API недоступен — не страшно */ }
    if (!Array.isArray(items) || !items.length) {
      let raw;
      try {
        raw = await fetchJson(`${API_HOST}/v2/items`, 600, 'pc', 'ru', ctx);
      } catch (e) {
        if (ITEMS_MEM) return ITEMS_MEM;
        const error = new Error(friendly(e));
        error.status = e.status || null;
        throw error;
      }
      const rawItems = extractList(raw, ['items']);
      if (!rawItems.length) throw new Error('каталог пуст');
      const head = rawItems.slice(0, 20).filter((x) => isObj(x) && isObj(x.i18n));
      const haveEn = head.some((x) => 'en' in x.i18n);
      const haveRu = head.some((x) => 'ru' in x.i18n);
      if (haveEn && haveRu) {
        items = buildCatalog(rawItems, null);
      } else {
        // ответ без английских или русских названий: берём английский каталог и подмешиваем русские имена
        const ruBySlug = new Map();
        for (const y of rawItems) {
          const s = pick(y, ['url_name', 'slug', 'url', 'id']);
          const n = (isObj(y.i18n) && isObj(y.i18n.ru) && y.i18n.ru.name) || pick(y, ['item_name', 'name', 'title']);
          if (s && n) ruBySlug.set(String(s), n);
        }
        const rawEn = await fetchJson(`${API_HOST}/v2/items`, 600, 'pc', 'en', ctx);
        items = buildCatalog(extractList(rawEn, ['items']), ruBySlug);
      }
      if (!items.length) throw new Error('каталог пуст');
      try {
        await caches.default.put(CATALOG_CACHE_URL, new Response(JSON.stringify(items), {
          headers: { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=600' } }));
      } catch (e) { /* не страшно */ }
    }
    withHay(items);
    const pub = [];
    const seen = new Set();
    for (const it of items) { const p = publicItem(it); if (p.slug && !seen.has(p.slug)) { seen.add(p.slug); pub.push(p); } }
    ITEMS_MEM = { at: now(), items, public: pub };
    return ITEMS_MEM;
  })().finally(() => { ITEMS_INFLIGHT = null; });
  return ITEMS_INFLIGHT;
}

// ---------- ордера и метрики ----------
function normalizeOrder(o) {
  if (!isObj(o)) return { order_type: '', platinum: 0, quantity: 0, user: { status: '' } };
  const user = o.user || o.player || o.owner || o.account;
  const status = isObj(user) ? (user.status || '') : pick(o, ['status', 'user_status'], '');
  return {
    order_type: String(pick(o, ['order_type', 'type', 'orderType', 'order'], '')).toLowerCase(),
    platinum: number(pick(o, ['platinum', 'price'], 0)),
    quantity: number(pick(o, ['quantity', 'qty', 'count'], 1)),
    user: { status: String(status).toLowerCase().replace(/[ -]/g, '') },
  };
}

async function fetchOrders(slug, platform, ctx) {
  const raw = await fetchJson(`${API_HOST}/v2/orders/item/${encodeURIComponent(slug)}`, 45, platform, 'en', ctx);
  if (extractOrders(raw).length) return raw;
  throw new Error(`нет активных ордеров для ${slug} на ${platform}`);
}

async function fetchItemDetail(slug, platform, ctx) {
  const raw = await fetchJson(`${API_HOST}/v2/item/${encodeURIComponent(slug)}`, 600, platform, 'en', ctx);
  let item = extractSingle(raw);
  if (isObj(item) && isObj(item.item)) item = item.item;
  const [name, nameRu, , thumb] = normalizeItem(item);
  return { name, nameRu, thumb };
}

const safeQuantity = (o) => Math.max(0, int(o.quantity || 0));

function stats(orders) {
  const online = (o) => isObj(o.user) && ['online', 'ingame'].includes(o.user.status);
  const sell = orders.filter((o) => o.order_type === 'sell');
  const buy = orders.filter((o) => o.order_type === 'buy');
  const osell = sell.filter(online), obuy = buy.filter(online);
  const sp = sell.map((o) => o.platinum || 0).filter((v) => v > 0).sort((a, b) => a - b);
  const bp = buy.map((o) => o.platinum || 0).filter((v) => v > 0).sort((a, b) => a - b);
  const med = (a) => { if (!a.length) return 0; const n = a.length; return n % 2 ? a[n >> 1] : round1((a[n / 2 - 1] + a[n / 2]) / 2); };
  const ms = med(sp), mb = med(bp);
  const rawSpread = ms && mb ? (ms - mb) / ms * 100 : 100;
  const spread = round1(Math.max(0, rawSpread));
  const crossed = Boolean(ms && mb && mb >= ms);
  const depth = osell.reduce((s, o) => s + safeQuantity(o), 0);
  const demand = obuy.reduce((s, o) => s + safeQuantity(o), 0);
  const demandScore = Math.min(100, demand / Math.max(depth, 1) * 100);
  const depthScore = Math.min(100, (osell.length + obuy.length) * 4);
  const spreadScore = crossed ? 0 : clamp(100 - Math.max(spread, 0) * 2.5, 0, 100);
  const onlineScore = Math.min(100, osell.length * 5 + obuy.length * 7);
  let score = Math.round(demandScore * 0.35 + depthScore * 0.25 + spreadScore * 0.25 + onlineScore * 0.15);
  if (crossed) score = Math.min(score, 35);
  const verdict = score >= 85 ? 'Очень ликвидный' : score >= 70 ? 'Хороший оборот' : score >= 50 ? 'Средний оборот' : 'Низкая ликвидность';
  const result = {
    source: 'live', sellCount: sell.length, buyCount: buy.length, onlineSell: osell.length, onlineBuy: obuy.length,
    sellDepth: depth, buyDepth: demand, medianSell: ms, medianBuy: mb,
    lowestSell: sp.length ? sp[0] : 0, highestBuy: bp.length ? bp[bp.length - 1] : 0,
    spread, rawSpread: round1(rawSpread), score, marketScore: score, scoreLabel: 'Общий рыночный score', verdict, crossedMarket: crossed,
    demandPct: Math.round(Math.min(100, demandScore)), supplyPct: Math.round(Math.min(100, depth / Math.max(demand, 1) * 50)),
    confidence: Math.min(100, Math.round((osell.length + obuy.length) * 3)),
    sellPrices: sp.slice(0, 80), buyPrices: bp.slice(-80),
  };
  return Object.assign(result, liquidityAnalysis(result));
}

function liquidityAnalysis(st) {
  const sellCount = int(st.sellCount), buyCount = int(st.buyCount), onlineSell = int(st.onlineSell), onlineBuy = int(st.onlineBuy);
  const sellDepth = int(st.sellDepth), buyDepth = int(st.buyDepth);
  const spread = st.spread !== null && st.spread !== undefined ? Number(st.spread) : 100;
  const crossed = Boolean(st.crossedMarket);
  const live = onlineSell + onlineBuy, market = sellCount + buyCount;
  const demandScore = Math.min(100, onlineBuy * 5 + Math.min(buyDepth, 100) * 0.35);
  const supplyScore = Math.min(100, onlineSell * 3 + Math.min(sellDepth, 100) * 0.2);
  const spreadScore = crossed ? 0 : clamp(100 - spread * 3, 0, 100);
  const presence = Math.min(100, market * 1.5);
  let liq = Math.round(demandScore * 0.45 + supplyScore * 0.3 + spreadScore * 0.2 + presence * 0.05);
  if (live < 4) liq = Math.min(liq, 45); else if (live < 8) liq = Math.min(liq, 65);
  if (onlineBuy === 0) liq = Math.min(liq, 35);
  if (crossed) liq = Math.min(liq, 35);
  let risk = Math.round(clamp(100 - liq + Math.max(0, spread - 10) * 2, 0, 100));
  let flip = Math.round(clamp(liq * 0.65 + spreadScore * 0.35, 0, 100));
  if (crossed) { risk = Math.max(risk, 70); flip = Math.min(flip, 25); }
  const payback = crossed ? 5 : Math.round(clamp(liq * 0.6 + (100 - Math.min(100, spread * 3)) * 0.4, 5, 99));
  const liqLabel = liq >= 80 ? 'Высокая ликвидность' : liq >= 60 ? 'Хорошая ликвидность' : liq >= 40 ? 'Средняя ликвидность' : 'Низкая ликвидность';
  const riskLabel = risk >= 70 ? 'Высокий риск зависания' : risk >= 40 ? 'Умеренный риск' : 'Низкий риск';
  let advice;
  if (crossed) advice = 'Buy-цена не ниже sell-цены. Проверь ордера: рынок может содержать устаревшие или пересекающиеся предложения.';
  else if (buyCount === 0) advice = 'Покупателей не найдено. Для быстрого флипа предмет сейчас не подходит.';
  else if (onlineBuy === 0) advice = 'Активных покупателей сейчас нет. Не закупай предмет большим объёмом.';
  else if (spread >= 25) advice = 'Спред слишком большой. Покупать стоит только под конкретный заказ.';
  else if (liq >= 80 && flip >= 70) advice = 'Предмет подходит для быстрого флипа: хороший спрос и приемлемый спред.';
  else if (liq >= 60) advice = 'Предмет можно продавать небольшими партиями. Перед покупкой проверь актуальные buy-ордера.';
  else advice = 'Ликвидность слабая. Не стоит закупать большой объём.';
  let vol = liq >= 85 && onlineBuy >= 10 ? 5 : liq >= 70 && onlineBuy >= 5 ? 3 : liq >= 55 && onlineBuy >= 2 ? 2 : 1;
  if (live < 4 && buyCount > 0 && !crossed) {
    advice = 'Недостаточно активных ордеров для уверенной оценки. Используй предмет осторожно и небольшим объёмом.';
    vol = 1;
  }
  if (crossed) vol = 1;
  return {
    liquidityScore: liq, liquidityLabel: liqLabel, riskScore: risk, riskLabel, flipScore: flip, recommendedVolume: vol, advice,
    analysisConfidence: live >= 20 ? 'high' : live >= 8 ? 'medium' : 'low', liveSampleSize: live, marketSampleSize: market,
    paybackScore: payback, crossedMarket: crossed,
  };
}

async function fetchLivePrice(slug, platform, ctx) {
  const orders = extractOrders(await fetchOrders(slug, platform, ctx)).map(normalizeOrder);
  return orders.length ? stats(orders) : null;
}

async function fetchLiveOffers(slug, platform, ctx, limit = 5) {
  const offers = { sell: [], buy: [] };
  for (const o of extractOrders(await fetchOrders(slug, platform, ctx))) {
    if (!isObj(o)) continue;
    const n = normalizeOrder(o);
    const user = o.user || o.player || o.owner || o.account;
    if (!isObj(user) || !(n.order_type in offers) || n.platinum <= 0) continue;
    const status = n.user.status;
    const name = user.ingameName || user.ingame_name || user.name || '';
    if (!['ingame', 'online'].includes(status) || !name) continue;
    offers[n.order_type].push({ name: String(name).slice(0, 40), platinum: n.platinum, quantity: int(n.quantity || 1) || 1, status });
  }
  const ig = (x) => (x.status !== 'ingame' ? 1 : 0);
  offers.sell.sort((a, b) => a.platinum - b.platinum || ig(a) - ig(b));
  offers.buy.sort((a, b) => b.platinum - a.platinum || ig(a) - ig(b));
  return { slug, platform, sell: offers.sell.slice(0, limit), buy: offers.buy.slice(0, limit) };
}

async function fetchItemHistory(slug, platform, ctx) {
  const raw = await fetchJson(`${API_HOST}/v1/items/${encodeURIComponent(slug)}/statistics`, 600, platform, 'en', ctx);
  const closed = (raw && raw.payload && raw.payload.statistics_closed) || {};
  const pack = (entries) => (Array.isArray(entries) ? entries : [])
    .filter((e) => isObj(e) && e.datetime)
    .map((e) => ({ t: e.datetime, median: number(e.median), avg: number(e.avg_price), min: number(e.min_price), max: number(e.max_price), volume: int(e.volume) }))
    .sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : 0));
  return { slug, platform, hours: pack(closed['48hours']).slice(-48), days: pack(closed['90days']).slice(-90) };
}

// ---------- ИИ (Gemini) ----------
function aiRateLimitOk(ip) {
  const t = now();
  while (AI_CALLS.length && t - AI_CALLS[0] > 60000) AI_CALLS.shift();
  if (AI_CALLS.length >= AI_RATE_LIMIT_PER_MINUTE) return false;
  const per = (AI_BY_IP.get(ip) || []).filter((x) => t - x <= 60000);
  if (per.length >= AI_RATE_LIMIT_PER_IP_PER_MINUTE) { AI_BY_IP.set(ip, per); return false; }
  AI_CALLS.push(t);
  per.push(t);
  AI_BY_IP.set(ip, per);
  if (AI_BY_IP.size > 2000) for (const [k, v] of AI_BY_IP) if (!v.length || t - v[v.length - 1] > 60000) AI_BY_IP.delete(k);
  return true;
}

function sanitizeAiPayload(raw) {
  if (!isObj(raw)) throw new Error('invalid payload');
  const slug = String(raw.slug || '').slice(0, 160).trim();
  if (!slug) throw new Error('slug required');
  let platform = String(raw.platform || 'pc').toLowerCase();
  if (!PLATFORMS.includes(platform)) platform = 'pc';
  return {
    slug, platform, name: String(raw.name || '').slice(0, 200), nameRu: String(raw.nameRu || '').slice(0, 200),
    medianSell: clamp(number(raw.medianSell), 0, 1e6), medianBuy: clamp(number(raw.medianBuy), 0, 1e6), spread: clamp(number(raw.spread), 0, 1000),
    sellCount: clamp(int(raw.sellCount), 0, 1e5), buyCount: clamp(int(raw.buyCount), 0, 1e5),
    onlineSell: clamp(int(raw.onlineSell), 0, 1e5), onlineBuy: clamp(int(raw.onlineBuy), 0, 1e5),
    liquidityScore: clamp(int(raw.liquidityScore), 0, 100), riskScore: clamp(int(raw.riskScore), 0, 100), flipScore: clamp(int(raw.flipScore), 0, 100),
    recommendedVolume: clamp(int(raw.recommendedVolume, 1), 1, 100), paybackScore: clamp(int(raw.paybackScore), 0, 100),
    crossedMarket: raw.crossedMarket === true,
  };
}

async function loadAiPayload(slug, platform, name, nameRu, ctx) {
  const p = await fetchLivePrice(slug, platform, ctx);
  if (!p) throw new Error('нет активных ордеров для этого предмета');
  return sanitizeAiPayload({ slug, platform, name, nameRu, ...p, spread: p.spread ?? 100, paybackScore: p.paybackScore ?? 5 });
}

function validateAiResult(result, payload) {
  if (!isObj(result)) throw new Error('AI returned an unexpected format');
  for (const k of ['verdict', 'summary', 'buyPrice', 'sellPrice', 'recommendedVolume', 'risks', 'action']) {
    if (!(k in result)) throw new Error(`AI response field is missing: ${k}`);
  }
  if (!Array.isArray(result.risks)) throw new Error('AI response risks must be an array');
  return {
    verdict: String(result.verdict).slice(0, 100), summary: String(result.summary).slice(0, 1000),
    buyPrice: payload.medianBuy, sellPrice: payload.medianSell, // цены всегда берём с сервера, не из ответа ИИ
    recommendedVolume: clamp(int(result.recommendedVolume, 1), 1, 100),
    risks: result.risks.slice(0, 3).map((x) => String(x).slice(0, 300)), action: String(result.action).slice(0, 1000),
  };
}

const AI_SYSTEM_PROMPT =
  'Ты — аналитик внутриигрового рынка Warframe Market. Тебе присылают уже посчитанные метрики по одному предмету в формате JSON, ' +
  'включая paybackScore (шанс окупа по шкале 0-100). Отвечай ТОЛЬКО валидным JSON, без markdown-разметки и пояснений вокруг, строго в такой форме:\n' +
  '{"verdict": "...", "summary": "...", "buyPrice": число, "sellPrice": число, "recommendedVolume": число, "risks": ["...", "..."], "action": "..."}\n' +
  'verdict — короткая фраза из 2-4 слов с общим выводом. summary — 1-2 предложения. risks — список из 1-3 коротких пунктов. ' +
  'action — одно предложение с конкретным советом. buyPrice и sellPrice — используй присланные medianBuy/medianSell напрямую, ' +
  'сервер всё равно подставит точные значения сам, важно только не написать в тексте советов цифру, которая противоречит входным данным. ' +
  'Все тексты пиши на русском. Не придумывай цифры и факты, которых нет во входных данных — используй только присланные значения.';

const AI_RESPONSE_SCHEMA = {
  type: 'OBJECT',
  properties: {
    verdict: { type: 'STRING' }, summary: { type: 'STRING' }, buyPrice: { type: 'NUMBER' }, sellPrice: { type: 'NUMBER' },
    recommendedVolume: { type: 'INTEGER' }, risks: { type: 'ARRAY', items: { type: 'STRING' } }, action: { type: 'STRING' },
  },
  required: ['verdict', 'summary', 'buyPrice', 'sellPrice', 'recommendedVolume', 'risks', 'action'],
};

function aiConfig(env) {
  const model = (env.GEMINI_MODEL || 'gemini-3.8-flash').trim();
  const fb = env.GEMINI_FALLBACK_MODEL === undefined ? 'gemini-2.5-flash-lite' : String(env.GEMINI_FALLBACK_MODEL).trim();
  return { key: String(env.GEMINI_API_KEY || '').trim(), model, fallback: fb, thinking: env.GEMINI_THINKING_LEVEL || 'low',
    models: fb && fb !== model ? [model, fb] : [model] };
}

async function geminiCall(key, model, body, timeoutMs) {
  return fetch(`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`, {
    method: 'POST', headers: { 'x-goog-api-key': key, 'content-type': 'application/json' },
    body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs),
  });
}

async function callAiAnalysis(payload, env) {
  const cfg = aiConfig(env);
  if (!cfg.key) throw new Error('AI service is not configured on the server (GEMINI_API_KEY missing)');
  const userContent = JSON.stringify({
    name: payload.nameRu || payload.name || payload.slug, medianSell: payload.medianSell, medianBuy: payload.medianBuy, spread: payload.spread,
    sellCount: payload.sellCount, buyCount: payload.buyCount, onlineSell: payload.onlineSell, onlineBuy: payload.onlineBuy,
    liquidityScore: payload.liquidityScore, riskScore: payload.riskScore, flipScore: payload.flipScore,
    recommendedVolume: payload.recommendedVolume, paybackScore: payload.paybackScore, crossedMarket: payload.crossedMarket,
  });
  let result = null;
  for (let i = 0; i < cfg.models.length; i++) {
    const model = cfg.models[i];
    const generationConfig = { responseMimeType: 'application/json', responseSchema: AI_RESPONSE_SCHEMA, maxOutputTokens: 2048 };
    if (model.startsWith('gemini-3')) generationConfig.thinkingConfig = { thinkingLevel: cfg.thinking };
    const resp = await geminiCall(cfg.key, model, {
      contents: [{ role: 'user', parts: [{ text: userContent }] }],
      systemInstruction: { parts: [{ text: AI_SYSTEM_PROMPT }] }, generationConfig,
    }, 20000);
    if (resp.ok) { result = await resp.json(); break; }
    const text = await resp.text().catch(() => '');
    if (i + 1 < cfg.models.length && [404, 429, 500, 503].includes(resp.status)) {
      console.log(`AI model ${model} failed with HTTP ${resp.status}, trying ${cfg.models[i + 1]}`);
      continue;
    }
    console.log(`AI HTTP ${resp.status}: ${text.slice(0, 300)}`);
    const err = new HttpError(resp.status, `Gemini HTTP ${resp.status}`);
    err.location = /location is not supported/i.test(text);
    throw err;
  }
  const candidates = (result && result.candidates) || [];
  if (!candidates.length) throw new Error(`AI returned no candidates (blockReason=${result && result.promptFeedback && result.promptFeedback.blockReason})`);
  const parts = (candidates[0].content && candidates[0].content.parts) || [];
  let text = parts.map((p) => p.text || '').join('').trim();
  if (text.startsWith('```')) text = text.replace(/^```(?:json)?/, '').replace(/```\s*$/, '').trim();
  return validateAiResult(JSON.parse(text), payload);
}

function stableStringify(v) {
  if (Array.isArray(v)) return '[' + v.map(stableStringify).join(',') + ']';
  if (isObj(v)) return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stableStringify(v[k])).join(',') + '}';
  return JSON.stringify(v);
}

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function safeEqual(a, b) {
  const [x, y] = await Promise.all([sha256Hex(String(a)), sha256Hex(String(b))]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

// ---------- Telegram ----------
function tgConfig(env) {
  const token = String(env.TELEGRAM_BOT_TOKEN || '').trim();
  const chatId = String(env.TELEGRAM_CHAT_ID || '').trim();
  const key = String(env.ALERTS_KEY || '').trim();
  return { token, chatId, key, kv: env.ALERTS_KV || null, enabled: Boolean(token && chatId && key.length >= 8 && env.ALERTS_KV) };
}

async function tgSend(cfg, text) {
  const resp = await fetch(`https://api.telegram.org/bot${cfg.token}/sendMessage`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: cfg.chatId, text, disable_web_page_preview: true }), signal: AbortSignal.timeout(10000),
  });
  if (!resp.ok) throw new Error(`Telegram HTTP ${resp.status}: ${(await resp.text().catch(() => '')).slice(0, 200)}`);
  return true;
}

async function tgLoad(cfg) {
  try { const v = await cfg.kv.get('alerts', 'json'); return isObj(v) ? v : {}; } catch (e) { return {}; }
}

async function tgSync(cfg, platform, items) {
  const old = await tgLoad(cfg);
  const fresh = {};
  for (const item of items.slice(0, 100)) {
    if (!isObj(item)) continue;
    const slug = String(item.slug || '');
    const target = number(item.target);
    if (!SLUG_RE.test(slug) || !(target > 0 && target <= 1e6)) continue;
    const o = old[slug];
    const same = Boolean(o) && o.target === target && o.platform === platform;
    fresh[slug] = { target, platform, name: String(item.name || slug).slice(0, 60), fired: same ? (o.fired || null) : null };
  }
  if (stableStringify(fresh) !== stableStringify(old)) await cfg.kv.put('alerts', JSON.stringify(fresh)); // экономим лимит записей KV
  return { count: Object.keys(fresh).length, fired: Object.keys(fresh).filter((s) => fresh[s].fired) };
}

async function tgCheckOnce(env) {
  const cfg = tgConfig(env);
  if (!cfg.enabled) return;
  const alerts = await tgLoad(cfg);
  const pending = Object.entries(alerts).filter(([, a]) => !a.fired).slice(0, TG_MAX_CHECKS_PER_RUN);
  if (!pending.length) return;
  const ctx = { low: false };
  let changed = false;
  for (const [slug, alert] of pending) {
    let price;
    try { price = await fetchLivePrice(slug, alert.platform, ctx); } catch (e) { console.log(`Alert check failed for ${slug}: ${e.message}`); continue; }
    const value = price ? number(price.medianSell) : 0;
    if (value <= 0 || value > alert.target) continue;
    try {
      await tgSend(cfg, `🔔 ${alert.name}: цена ${value} p (цель ≤ ${alert.target} p, ${String(alert.platform).toUpperCase()})`);
    } catch (e) { console.log(`Telegram send failed: ${e.message}`); continue; }
    alert.fired = Date.now() / 1000;
    changed = true;
  }
  if (changed) {
    // перечитываем, чтобы не затереть изменения, которые сайт мог прислать за это время
    const current = await tgLoad(cfg);
    for (const [slug, a] of pending) if (a.fired && current[slug] && current[slug].target === a.target) current[slug].fired = a.fired;
    await cfg.kv.put('alerts', JSON.stringify(current));
  }
}

// ---------- диагностика ----------
async function probeAi(env) {
  const cfg = aiConfig(env);
  if (!cfg.key) return { configured: false, hint: 'GEMINI_API_KEY is not set on the server' };
  const out = { configured: true, tries: [] };
  for (const model of cfg.models) {
    try {
      const resp = await geminiCall(cfg.key, model, { contents: [{ role: 'user', parts: [{ text: 'Reply with the word ok' }] }], generationConfig: { maxOutputTokens: 16 } }, 15000);
      if (resp.ok) out.tries.push({ model, ok: true, status: resp.status });
      else {
        let message = '';
        try { message = ((await resp.json()).error || {}).message || ''; } catch (e) { /* пусто */ }
        out.tries.push({ model, ok: false, status: resp.status, message: String(message).slice(0, 160) });
      }
    } catch (e) {
      out.tries.push({ model, ok: false, message: `${e.name}: ${String(e.message).slice(0, 100)}` });
    }
  }
  return out;
}

async function runProbes(env, request, withAi, ctx) {
  const t = now();
  const cached = PROBE_STATE.result;
  if (cached && t - PROBE_STATE.at < 15000 && (!withAi || 'ai' in cached)) return cached;
  PROBE_STATE.at = t;
  const runAi = withAi && t - PROBE_STATE.aiAt >= 30000;
  if (runAi) PROBE_STATE.aiAt = t;
  const result = { colo: (request.cf && request.cf.colo) || '', country: (request.cf && request.cf.country) || '' };
  let thumb = '';
  try {
    const { items } = await getItems(ctx);
    thumb = (items.find((i) => i.thumb) || {}).thumb || '';
    result.items = { ok: true, count: items.length, withThumb: items.filter((i) => i.thumb).length };
  } catch (e) {
    result.items = { ok: false, upstreamStatus: e.status || null, error: `${e.name}: ${String(e.message).slice(0, 160)}` };
  }
  if (thumb) {
    try {
      const resp = await fetch(thumb, { headers: { 'User-Agent': UA, Accept: 'image/avif,image/webp,image/*,*/*', Referer: 'https://warframe.market/' }, signal: AbortSignal.timeout(10000) });
      const body = await resp.arrayBuffer();
      result.image = { ok: resp.ok, status: resp.status, type: String(resp.headers.get('Content-Type') || '').slice(0, 40), bytes: body.byteLength, url: thumb };
    } catch (e) {
      result.image = { ok: false, url: thumb, error: `${e.name}: ${String(e.message).slice(0, 120)}` };
    }
  } else if (result.items.ok) result.image = { ok: false, error: 'catalog items have no thumb field' };
  if (runAi) result.ai = await probeAi(env);
  else if (withAi) result.ai = { cooldown: 'try again in 30 seconds' };
  PROBE_STATE.result = result;
  return result;
}

// ---------- HTTP ----------
const SECURITY_HEADERS = { 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'SAMEORIGIN', 'Referrer-Policy': 'strict-origin-when-cross-origin' };

function corsOrigin(request, env) {
  const allowed = String(env.ALLOWED_ORIGIN || '').split(',').map((o) => o.trim().replace(/\/+$/, '')).filter(Boolean);
  if (!allowed.length) return '*';
  const origin = (request.headers.get('Origin') || '').replace(/\/+$/, '');
  return allowed.includes(origin) ? origin : allowed[0];
}

function json(obj, status, request, env) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
      'Access-Control-Allow-Origin': corsOrigin(request, env), Vary: 'Origin', ...SECURITY_HEADERS },
  });
}

function getPlatform(url) {
  const v = (url.searchParams.get('platform') || 'pc').toLowerCase();
  return { pc: 'pc', ps4: 'ps4', playstation: 'ps4', psn: 'ps4', xbox: 'xbox', xb1: 'xbox', switch: 'switch' }[v] || 'pc';
}

async function readJsonBody(request, maxLen) {
  const text = await request.text();
  if (!text.length || text.length > maxLen) return { error: 'length' };
  try { return { data: JSON.parse(text) }; } catch (e) { return { error: 'json' }; }
}

function slugFrom(path, prefix) {
  let s;
  try { s = decodeURIComponent(path.slice(prefix.length)).trim(); } catch (e) { s = path.slice(prefix.length).trim(); }
  return s;
}

const CATEGORY_TAGS = {
  warframes: ['warframe'], weapons: ['weapon'], mods: ['mod'], relics: ['relic'],
  resources: ['resource', 'material'], arcanes: ['arcane', 'arcane_enhancement'],
};
const ALL_CATEGORY_TAGS = ['warframe', 'weapon', 'mod', 'relic', 'resource', 'material', 'arcane', 'arcane_enhancement'];

async function handleImage(url, request, env) {
  const src = (url.searchParams.get('src') || '').trim();
  let source;
  try { source = new URL(src); } catch (e) { return json({ error: 'invalid image source' }, 400, request, env); }
  if (source.protocol !== 'https:' || !['warframe.market', 'www.warframe.market', 'api.warframe.market'].includes(source.hostname)) {
    return json({ error: 'invalid image source' }, 400, request, env);
  }
  try {
    const resp = await fetch(source.toString(), {
      headers: { 'User-Agent': UA, Accept: 'image/avif,image/webp,image/*,*/*', Referer: 'https://warframe.market/' },
      cf: { cacheEverything: true, cacheTtl: 86400 }, signal: AbortSignal.timeout(10000),
    });
    if (!resp.ok) return json({ error: 'image unavailable' }, 502, request, env);
    const type = (resp.headers.get('Content-Type') || '').split(';')[0].toLowerCase();
    if (!type.startsWith('image/')) return json({ error: 'upstream response is not an image' }, 502, request, env);
    const len = Number(resp.headers.get('Content-Length') || 0);
    const max = 5 * 1024 * 1024;
    if (len > max) return json({ error: 'image is too large' }, 413, request, env);
    let body = resp.body;
    if (!len) {
      body = await resp.arrayBuffer();
      if (body.byteLength > max) return json({ error: 'image is too large' }, 413, request, env);
    }
    return new Response(body, { headers: { 'Content-Type': type, 'Cache-Control': 'public, max-age=86400',
      'Access-Control-Allow-Origin': corsOrigin(request, env), ...SECURITY_HEADERS } });
  } catch (e) {
    console.log(`Image proxy error: ${e.name}: ${e.message} (${src.slice(0, 100)})`);
    return json({ error: 'image unavailable' }, 502, request, env);
  }
}

async function handleGet(request, env, url, ctx) {
  const path = url.pathname;
  const plat = getPlatform(url);
  const J = (obj, status) => json(obj, status, request, env);

  if (path === '/' || path === '/index.html') return J({ ok: true, service: 'MarketPulse API', health: '/api/health' });
  if (path === '/config.js') {
    return new Response("window.MP_API_BASE=location.origin;\n", { headers: { 'Content-Type': 'application/javascript; charset=utf-8', 'Cache-Control': 'no-store' } });
  }
  if (path === '/api/tg/status') return J({ enabled: tgConfig(env).enabled });

  if (path === '/api/health') {
    const cfg = aiConfig(env);
    const health = { ok: true, runtime: 'cloudflare-workers', marketStatus: 'not-checked', cacheEntries: CACHE.size, imageCacheEntries: 0,
      uptimeSeconds: Math.round((now() - START_TIME) / 1000),
      ai: { configured: Boolean(cfg.key), model: cfg.model, fallback: cfg.fallback },
      telegram: { enabled: tgConfig(env).enabled, kvBound: Boolean(env.ALERTS_KV) } };
    if (url.searchParams.get('deep') === '1') {
      health.probe = await runProbes(env, request, url.searchParams.get('ai') === '1', ctx);
      health.marketStatus = health.probe.items && health.probe.items.ok ? 'catalog-available' : 'unavailable';
    }
    return J(health);
  }

  if (path === '/api/image') return handleImage(url, request, env);

  if (path === '/api/items') {
    const offset = Math.max(0, parseInt(url.searchParams.get('offset') || '0', 10) || 0);
    let limit = parseInt(url.searchParams.get('limit') || '60', 10);
    if (!Number.isFinite(limit)) limit = 60;
    limit = clamp(limit, 12, 100);
    let all;
    try { all = (await getItems(ctx)).public; } catch (e) { console.log(`Items failed: ${e.message}`); return J({ error: friendly(e), upstreamStatus: e.status || null, source: 'warframe.market' }, failCode(e)); }
    const cat = (url.searchParams.get('category') || 'all').toLowerCase() || 'all';
    const prime = (url.searchParams.get('prime') || '0') === '1';
    if (cat !== 'all' || prime) {
      all = all.filter((x) => {
        const tags = new Set((x.tags || []).map((t) => String(t).toLowerCase()));
        if (prime && !tags.has('prime')) return false;
        if (cat === 'other') return !ALL_CATEGORY_TAGS.some((t) => tags.has(t));
        if (CATEGORY_TAGS[cat]) return CATEGORY_TAGS[cat].some((t) => tags.has(t));
        return true;
      });
    }
    const page = all.slice(offset, offset + limit);
    return J({ items: page, offset, limit, total: all.length, hasMore: offset + page.length < all.length });
  }

  const slugRoutes = ['/api/item-detail/', '/api/history/', '/api/orders/', '/api/price/', '/api/item/'];
  const prefix = slugRoutes.find((p) => path.startsWith(p));
  if (prefix) {
    const slug = slugFrom(path, prefix);
    if (!slug) return J({ error: 'slug required' }, 400);
    if (slug.length > 160) return J({ error: 'slug too long' }, 414);

    if (prefix === '/api/item-detail/') {
      try { return J({ slug, ...(await fetchItemDetail(slug, plat, ctx)) }); } catch (e) {
        console.log(`Item detail failed for ${slug}: ${e.message}`);
        return J({ slug, name: '', nameRu: '', thumb: '' }, failCode(e));
      }
    }
    if (prefix === '/api/history/') {
      try { return J(await fetchItemHistory(slug, plat, ctx)); } catch (e) {
        console.log(`History failed for ${slug}: ${e.message}`);
        return J({ slug, hours: [], days: [] }, failCode(e));
      }
    }
    if (prefix === '/api/orders/') {
      try { return J(await fetchLiveOffers(slug, plat, ctx)); } catch (e) {
        console.log(`Orders failed for ${slug}: ${e.message}`);
        return J({ slug, sell: [], buy: [] }, failCode(e));
      }
    }
    if (prefix === '/api/price/') {
      try {
        const p = await fetchLivePrice(slug, plat, { ...ctx, low: true });
        if (!p || number(p.medianSell) <= 0) return J({ slug, available: false });
        return J({
          slug, available: true, price: number(p.medianSell), medianBuy: p.medianBuy, sellCount: p.sellCount, buyCount: p.buyCount,
          liquidityScore: p.liquidityScore, riskScore: p.riskScore, flipScore: p.flipScore, recommendedVolume: p.recommendedVolume,
          paybackScore: p.paybackScore, liquidityLabel: p.liquidityLabel, riskLabel: p.riskLabel, analysisConfidence: p.analysisConfidence,
          advice: p.advice, liveSampleSize: p.liveSampleSize, marketSampleSize: p.marketSampleSize, crossedMarket: p.crossedMarket,
          rawSpread: p.rawSpread, marketScore: p.marketScore, spread: p.spread, onlineSell: p.onlineSell, onlineBuy: p.onlineBuy,
        });
      } catch (e) {
        console.log(`Price failed for ${slug}: ${e.message}`);
        return J({ slug, available: false, error: friendly(e), upstreamStatus: e.status || null, source: 'warframe.market' }, failCode(e));
      }
    }
    // /api/item/
    let art = { name: '', nameRu: '', thumb: '' };
    try { art = await fetchItemDetail(slug, plat, ctx); } catch (e) { console.log(`Item detail failed for ${slug}: ${e.message}`); }
    let statsData = null, ordersError = null, rateLimited = false;
    try {
      const orders = extractOrders(await fetchOrders(slug, plat, ctx)).map(normalizeOrder);
      if (orders.length) statsData = stats(orders);
    } catch (e) {
      rateLimited = isRateLimited(e);
      ordersError = e.message;
      console.log(`Orders failed for ${slug}: ${e.message}`);
    }
    const out = { slug, name: art.name || '', nameRu: art.nameRu || '', thumb: art.thumb || '', stats: statsData,
      statsAvailable: statsData !== null, statsRateLimited: rateLimited };
    if (rateLimited) out.statsError = 'Лимит запросов к API';
    else if (ordersError) out.statsError = 'Активные ордера недоступны';
    return J(out);
  }

  if (path === '/api/search') {
    const q = (url.searchParams.get('q') || '').trim().slice(0, 80);
    let items;
    try { items = (await getItems(ctx)).items; } catch (e) { console.log(`Search failed: ${e.message}`); return J({ error: 'Поиск временно недоступен' }, 502); }
    return J({ items: searchItems(items, q).slice(0, 20).map(publicItem) });
  }

  return J({ error: 'not found' }, 404);
}

async function handlePost(request, env, url, ctx) {
  const path = url.pathname;
  const J = (obj, status) => json(obj, status, request, env);

  if (path === '/api/ai/analyze') {
    const { data: raw, error } = await readJsonBody(request, 20000);
    if (error === 'length') return J({ ok: false, error: 'некорректный запрос' }, 400);
    if (error) return J({ ok: false, error: 'некорректный JSON' }, 400);
    if (!isObj(raw)) return J({ ok: false, error: 'invalid payload' }, 400);
    const slug = String(raw.slug || '').slice(0, 160).trim();
    if (!slug) return J({ ok: false, error: 'slug required' }, 400);
    let platform = String(raw.platform || 'pc').toLowerCase();
    if (!PLATFORMS.includes(platform)) platform = 'pc';

    let payload;
    try {
      payload = await loadAiPayload(slug, platform, String(raw.name || '').slice(0, 200), String(raw.nameRu || '').slice(0, 200), ctx);
    } catch (e) {
      console.log(`AI payload fetch failed for ${slug}: ${e.message}`);
      if (isRateLimited(e)) return J({ ok: false, error: 'Лимит запросов к API рынка, попробуй позже', rateLimited: true }, 429);
      if (e instanceof HttpError) return J({ ok: false, error: 'Не удалось получить данные рынка' }, 502);
      return J({ ok: false, error: 'Недостаточно live-данных для анализа этого предмета' }, 502);
    }

    const cacheKey = `${payload.slug}:${payload.platform}:${(await sha256Hex(stableStringify(payload))).slice(0, 16)}`;
    const hit = AI_CACHE.get(cacheKey);
    if (hit && now() - hit.at < AI_CACHE_TTL * 1000) return J({ ...hit.result, ok: true, cached: true });

    if (!aiRateLimitOk(ctx.ip)) return J({ ok: false, error: 'Слишком много запросов к ИИ, попробуй через минуту', rateLimited: true }, 429);

    let result;
    try {
      result = await callAiAnalysis(payload, env);
    } catch (e) {
      console.log(`AI analyze failed for ${payload.slug}: ${e.message}`);
      if (isRateLimited(e)) return J({ ok: false, error: 'ИИ временно перегружен, попробуй позже', rateLimited: true }, 429);
      if (e.location) return J({ ok: false, error: 'Google не принимает запросы из региона, где запустился сервер. Проверь placement в wrangler.jsonc' }, 502);
      if (e instanceof HttpError) return J({ ok: false, error: 'ИИ-сервис недоступен' }, 502);
      return J({ ok: false, error: 'Не удалось получить ответ от ИИ' }, 502);
    }
    lruSet(AI_CACHE, cacheKey, { at: now(), result });
    return J({ ...result, ok: true, cached: false });
  }

  if (path === '/api/tg/sync' || path === '/api/tg/test') {
    const cfg = tgConfig(env);
    if (!cfg.enabled) return J({ error: 'telegram disabled' }, 404);
    const { data: raw, error } = await readJsonBody(request, 30000);
    if (error === 'length') return J({ error: 'bad length' }, 400);
    if (error) return J({ error: 'bad json' }, 400);
    if (!isObj(raw) || !(await safeEqual(raw.key || '', cfg.key))) {
      await sleep(1000);
      return J({ error: 'bad key' }, 403);
    }
    if (path === '/api/tg/test') {
      try { await tgSend(cfg, '✅ MarketPulse подключён. Алерты цен будут приходить сюда.'); } catch (e) {
        console.log(`Telegram test failed: ${e.message}`);
        return J({ error: 'telegram error' }, 502);
      }
      return J({ ok: true });
    }
    let platform = String(raw.platform || 'pc').toLowerCase();
    if (!PLATFORMS.includes(platform)) platform = 'pc';
    const res = await tgSync(cfg, platform, Array.isArray(raw.alerts) ? raw.alerts : []);
    return J({ ok: true, ...res });
  }

  return J({ error: 'not found' }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const ctx = { ip: (request.headers.get('CF-Connecting-IP') || 'unknown').slice(0, 64), low: false };
    try {
      if (request.method === 'OPTIONS') {
        return new Response(null, { status: 204, headers: { 'Access-Control-Allow-Origin': corsOrigin(request, env), Vary: 'Origin',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type, Platform, Language', ...SECURITY_HEADERS } });
      }
      if (request.method === 'GET' || request.method === 'HEAD') {
        // Сайт лежит рядом (папка public). Сюда попадают только запросы, для которых файла нет.
        if (env.ASSETS && !url.pathname.startsWith('/api/') && url.pathname !== '/config.js') return env.ASSETS.fetch(request);
        return await handleGet(request, env, url, ctx);
      }
      if (request.method === 'POST') return await handlePost(request, env, url, ctx);
      return json({ error: 'method not allowed' }, 405, request, env);
    } catch (e) {
      console.log(`Unhandled request error: ${e.name}: ${e.message}`);
      return json({ error: 'Внутренняя ошибка сервера' }, 500, request, env);
    }
  },

  // Cron Trigger (раз в 3 минуты): проверка алертов Telegram
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tgCheckOnce(env).catch((e) => console.log(`Alert loop error: ${e.message}`)));
  },
};

