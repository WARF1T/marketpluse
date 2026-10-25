"""MarketPulse backend: frontend + cached warframe.market proxy (API v2)."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
from urllib.error import HTTPError
from collections import OrderedDict
import os
import base64
import json
import math
import hashlib
import hmac
import re
import time
import threading
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, 'public', 'index.html')
API_HOST = 'https://api.warframe.market'
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '')
API_VERSIONS = ['v2']
IMG_BASE = 'https://warframe.market/static/assets/'
START_TIME = time.time()

CACHE = OrderedDict()
CACHE_MAX_SIZE = 1500
ERROR_CACHE = OrderedDict()
ERROR_CACHE_TTL = 15
AI_CACHE = OrderedDict()
AI_CACHE_TTL = 300
AI_RATE_LIMIT_PER_MINUTE = 10
AI_RATE_LIMIT_PER_IP_PER_MINUTE = 5
AI_CALL_TIMESTAMPS = []
AI_RATE_LIMITS_BY_IP = {}
LOCK = threading.Lock()
ITEMS_CACHE = {}
ITEMS_LOCK = threading.Lock()
IMAGE_CACHE = OrderedDict()
IMAGE_CACHE_MAX_SIZE = 300
IMAGE_LOCK = threading.Lock()

UA = ('Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36')

NAME_KEYS = ('item_name', 'name', 'title')
SLUG_KEYS = ('url_name', 'slug', 'url', 'id')
THUMB_KEYS = ('thumb', 'thumbnail', 'icon', 'image')


def norm(s):
    return (s or '').casefold().replace('ё', 'е').strip()


def pick(d, keys, default=''):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def number(value, default=0):
    try:
        result = float(value)
        if not math.isfinite(result):
            return default
        if result.is_integer():
            return int(result)
        return result
    except (TypeError, ValueError):
        return default


def extract_list(data, names):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for c in (data, data.get('payload'), data.get('data'), data.get('result')):
            if isinstance(c, dict):
                for n in names:
                    v = c.get(n)
                    if isinstance(v, list):
                        return v
            elif isinstance(c, list):
                return c
    return []


def extract_orders(raw):
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    data = raw.get('data')
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        source = data
    else:
        source = raw
    for key in ('orders', 'results', 'list'):
        value = source.get(key)
        if isinstance(value, list):
            return value
    sell = source.get('sell')
    buy = source.get('buy')
    if isinstance(sell, list) and isinstance(buy, list):
        result = []
        for order in sell:
            if isinstance(order, dict):
                item = dict(order)
                item.setdefault('order_type', 'sell')
                result.append(item)
        for order in buy:
            if isinstance(order, dict):
                item = dict(order)
                item.setdefault('order_type', 'buy')
                result.append(item)
        return result
    return extract_list(raw, ('orders', 'results', 'list'))


def extract_single(raw):
    if not isinstance(raw, dict):
        return {}
    data = raw.get('data')
    if isinstance(data, dict):
        return data
    payload = raw.get('payload')
    if isinstance(payload, dict):
        return payload
    result = raw.get('result')
    if isinstance(result, dict):
        return result
    return raw


def friendly(e):
    code = getattr(e, 'code', None)
    if code:
        return (f'API warframe.market недоступен (HTTP {code}). '
                'Возможна блокировка провайдером или даунтайм: попробуй VPN/другую сеть.')
    return f'Нет соединения с warframe.market: {e}'


def dedup_items(items):
    seen = set()
    out = []
    for it in items:
        s = it.get('slug')
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(it)
    return out


def image_url(value):
    if not value:
        return ''
    value = str(value).strip()
    if not value:
        return ''
    if value.startswith(('http://', 'https://')):
        return value
    if value.startswith('//'):
        return 'https:' + value
    if value.startswith('/static/'):
        return 'https://warframe.market' + value
    if value.startswith('static/'):
        return 'https://warframe.market/' + value
    if value.startswith('/'):
        return 'https://warframe.market' + value
    return IMG_BASE.rstrip('/') + '/' + value.lstrip('/')


def normalize_item(x):
    if not isinstance(x, dict):
        return '', '', '', ''
    i18n = x.get('i18n') or {}
    if not isinstance(i18n, dict):
        i18n = {}
    en = i18n.get('en') or {}
    ru = i18n.get('ru') or {}
    uk = i18n.get('uk') or {}
    if not isinstance(en, dict):
        en = {}
    if not isinstance(ru, dict):
        ru = {}
    if not isinstance(uk, dict):
        uk = {}
    name = (en.get('name') or x.get('name') or x.get('item_name') or x.get('title') or '')
    name_ru = (ru.get('name') or uk.get('name') or x.get('nameRu') or name)
    slug = (x.get('slug') or x.get('url_name') or x.get('url') or x.get('id') or '')
    thumb = (en.get('thumb') or en.get('thumbnail') or en.get('icon') or en.get('image')
             or ru.get('thumb') or ru.get('thumbnail') or ru.get('icon') or ru.get('image')
             or uk.get('thumb') or uk.get('thumbnail') or uk.get('icon') or uk.get('image')
             or x.get('thumb') or x.get('thumbnail') or x.get('icon') or x.get('image') or '')
    return str(name), str(name_ru), str(slug), image_url(thumb)


def ru_name_of(x):
    i18n = x.get('i18n')
    if isinstance(i18n, dict):
        ru = i18n.get('ru') or {}
        if ru.get('name'):
            return ru['name']
    return pick(x, NAME_KEYS)


SYNONYMS = {
    'glaive': ['глайв'], 'nikana': ['никана'], 'volt': ['вольт'],
    'mirage': ['мираж'], 'nova': ['нова'], 'trinity': ['тринити'],
    'excalibur': ['экскалибур'], 'rhino': ['рино'], 'mag': ['маг'],
    'loki': ['локи'], 'ash': ['эш'], 'banshee': ['банши'],
    'oberon': ['оберон'], 'hydroid': ['гидроид'], 'valkyr': ['валькир'],
    'ember': ['эмбер'], 'frost': ['фрост'],
    'prime': ['прайм'], 'blueprint': ['чертеж', 'бп'], 'set': ['сет', 'набор'],
    'blade': ['клинок', 'диск'], 'sword': ['меч'], 'rifle': ['винтовка'],
    'pistol': ['пистолет'], 'shotgun': ['дробовик'], 'bow': ['лук'],
    'scythe': ['коса'], 'hammer': ['молот'], 'staff': ['посох'],
    'systems': ['системы', 'система'], 'chassis': ['каркас'],
    'neuroptics': ['нейрооптика', 'шлем'], 'helmet': ['шлем'],
}
RU_TO_EN = {}
for en, rus in SYNONYMS.items():
    for ru in rus:
        RU_TO_EN.setdefault(norm(ru), set()).add(norm(en))


def aliases_for_text(text):
    words = set(norm(text).replace('_', ' ').replace('-', ' ').split())
    out = set()
    for en, rus in SYNONYMS.items():
        if norm(en) in words:
            out.update(norm(r) for r in rus)
    return out


def expand_query(q):
    qn = norm(q)
    if not qn:
        return []
    terms = {qn}
    parts = qn.replace('_', ' ').replace('-', ' ').split()
    terms.update(parts)
    for part in parts:
        if part in RU_TO_EN:
            terms.update(RU_TO_EN[part])
        for en, rus in SYNONYMS.items():
            en_n = norm(en)
            if en_n == part:
                terms.add(en_n)
                terms.update(norm(r) for r in rus)
            for ru in rus:
                ru_n = norm(ru)
                if ru_n == part:
                    terms.add(en_n)
                    terms.add(ru_n)
    for ru, ens in RU_TO_EN.items():
        if ru and ru in qn:
            terms.update(ens)
    return [x for x in terms if x]


UPSTREAM_LOCK = threading.Lock()
UPSTREAM_NEXT = 0.0
UPSTREAM_GAP = 0.34       # ~3 requests/second, the warframe.market limit
UPSTREAM_MAX_WAIT = 5.0   # longer queue: fail fast with a local 429 instead of hanging visitors
UPSTREAM_LOW_WAIT = 4.0   # background price prefetch gives up earlier than interactive requests
LOCAL = threading.local()
STALE_MAX_AGE = 900       # serve cached data up to 15 minutes old when upstream fails
INFLIGHT = {}


def upstream_wait(url):
    """Space upstream requests apart so many visitors cannot trip the shared rate limit."""
    global UPSTREAM_NEXT
    with UPSTREAM_LOCK:
        now = time.time()
        start = max(now, UPSTREAM_NEXT)
        wait = start - now
        if wait > (UPSTREAM_LOW_WAIT if getattr(LOCAL, 'low', False) else UPSTREAM_MAX_WAIT):
            error = HTTPError(url, 429, 'Local queue is full', {}, None)
            error.local = True
            raise error
        UPSTREAM_NEXT = start + UPSTREAM_GAP
    if wait > 0:
        time.sleep(wait)


def fetch_json(url, ttl=60, platform='pc', language='en', method='GET', body=None):
    """Single-flight wrapper: concurrent identical requests share one upstream call."""
    key = f'{platform}:{language}:{method}:{url}'
    with LOCK:
        gate = INFLIGHT.setdefault(key, threading.Lock())
    try:
        with gate:
            return _fetch_json(url, ttl, platform, language, method, body)
    finally:
        with LOCK:
            if INFLIGHT.get(key) is gate and not gate.locked():
                INFLIGHT.pop(key, None)


def _fetch_json(url, ttl=60, platform='pc', language='en', method='GET', body=None):
    key = f'{platform}:{language}:{method}:{url}'
    now = time.time()
    with LOCK:
        hit = CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        err_hit = ERROR_CACHE.get(key)
        if err_hit and now - err_hit[0] < ERROR_CACHE_TTL:
            if hit and now - hit[0] < STALE_MAX_AGE:
                return hit[1]
            raise err_hit[1]
    variants = [
        {'User-Agent': UA, 'Accept': 'application/json', 'Platform': platform, 'Language': language},
        {'User-Agent': UA, 'Accept': 'application/json'},
        {'User-Agent': UA},
    ]
    last = None
    for headers in variants:
        h = dict(headers)
        data_bytes = None
        if body is not None:
            data_bytes = json.dumps(body).encode('utf-8')
            h['Content-Type'] = 'application/json'
        try:
            upstream_wait(url)
            req = urllib.request.Request(url, data=data_bytes, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            with LOCK:
                CACHE[key] = (time.time(), data)
                CACHE.move_to_end(key)
                ERROR_CACHE.pop(key, None)
                while len(CACHE) > CACHE_MAX_SIZE:
                    CACHE.popitem(last=False)
            return data
        except HTTPError as e:
            last = e
            if e.code not in (400, 403, 404, 405, 406):
                break
        except Exception as e:
            last = e
    if isinstance(last, HTTPError) and last.code == 429 and not getattr(last, 'local', False):
        with LOCK:
            ERROR_CACHE[key] = (time.time(), last)
            ERROR_CACHE.move_to_end(key)
            while len(ERROR_CACHE) > CACHE_MAX_SIZE:
                ERROR_CACHE.popitem(last=False)
    with LOCK:
        stale = CACHE.get(key)
    if stale and time.time() - stale[0] < STALE_MAX_AGE:
        return stale[1]
    raise last if last else Exception('нет соединения')


def is_rate_limited(error):
    return isinstance(error, HTTPError) and error.code == 429


def public_item(item):
    tags = item.get('tags', [])
    if not isinstance(tags, list):
        tags = []
    aliases = item.get('aliases', [])
    if not isinstance(aliases, list):
        aliases = []
    return {
        'name': str(item.get('name', '') or ''),
        'nameRu': str(item.get('nameRu', '') or ''),
        'slug': str(item.get('slug', '') or ''),
        'thumb': str(item.get('thumb', '') or ''),
        'aliases': aliases,
        'tags': tags,
        'prime': any(str(tag).lower() == 'prime' for tag in tags),
        'ducats': number(item.get('ducats', 0)),
    }


def get_items(platform='pc'):
    key = f'items:{platform}'
    now = time.time()
    with ITEMS_LOCK:
        hit = ITEMS_CACHE.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    out = []
    last_err = None
    for ver in API_VERSIONS:
        try:
            raw = fetch_json(f'{API_HOST}/{ver}/items', 600, platform, 'en')
        except Exception as e:
            last_err = e
            continue
        raw_items = extract_list(raw, ('items',))
        if not raw_items:
            continue
        have_ru = any(
            isinstance(x, dict) and isinstance(x.get('i18n'), dict) and ('ru' in x['i18n'])
            for x in raw_items[:20]
        )
        ru_by_slug = {}
        if not have_ru:
            try:
                raw_ru = fetch_json(f'{API_HOST}/{ver}/items', 600, platform, 'ru')
                for y in extract_list(raw_ru, ('items',)):
                    s = pick(y, SLUG_KEYS)
                    n = ru_name_of(y)
                    if s and n:
                        ru_by_slug[str(s)] = n
            except Exception:
                pass
        tmp = []
        seen = set()
        for x in raw_items:
            name, name_ru, slug, thumb = normalize_item(x)
            if not name or not slug or slug in seen:
                continue
            seen.add(slug)
            name_ru = ru_by_slug.get(slug) or name_ru
            aliases = sorted(aliases_for_text(f'{name} {name_ru} {slug}'))
            hay = norm(' | '.join([
                name, name_ru,
                slug.replace('_', ' ').replace('-', ' '),
                ' '.join(aliases),
            ]))
            hay_words = set(hay.replace('|', ' ').split())
            tags = x.get('tags') if isinstance(x.get('tags'), list) else []
            tmp.append({
                'name': name, 'nameRu': name_ru, 'slug': slug,
                'thumb': thumb, 'aliases': aliases, 'tags': tags,
                'ducats': x.get('ducats', 0),
                'hay': hay, 'hay_words': hay_words,
            })
        if tmp:
            out = tmp
            break
    if not out:
        raise Exception(friendly(last_err))
    with ITEMS_LOCK:
        ITEMS_CACHE[key] = (time.time(), out)
    return out


def match_item(item, q):
    qn = norm(q)
    if not qn:
        return True
    hay = item.get('hay') or ''
    words = item.get('hay_words') or set(hay.replace('|', ' ').split())
    if qn in hay:
        return True
    for term in expand_query(q):
        if len(term) < 2:
            continue
        if ' ' in term:
            if term in hay:
                return True
        else:
            if term in words:
                return True
    return False


def search_items(items, q):
    qn = norm(q)
    if not qn:
        return []
    res = [item for item in items if match_item(item, qn)]

    def score(item):
        name = norm(item.get('name'))
        ru = norm(item.get('nameRu'))
        if qn == name or qn == ru:
            return 0
        if name.startswith(qn) or ru.startswith(qn):
            return 1
        if qn in name or qn in ru:
            return 2
        expanded = [t for t in expand_query(qn) if len(t) >= 3]
        if any(t in name or t in ru for t in expanded):
            return 3
        return 4

    return sorted(res, key=score)


def normalize_order(o):
    if not isinstance(o, dict):
        return {'order_type': '', 'platinum': 0, 'quantity': 0, 'user': {'status': ''}}
    user = o.get('user') or o.get('player') or o.get('owner') or o.get('account')
    if isinstance(user, dict):
        status = user.get('status') or ''
    else:
        status = pick(o, ('status', 'user_status'), '')
    order_type = pick(o, ('order_type', 'type', 'orderType', 'order'), '')
    return {
        'order_type': str(order_type).lower(),
        'platinum': number(pick(o, ('platinum', 'price'), 0)),
        'quantity': number(pick(o, ('quantity', 'qty', 'count'), 1)),
        'user': {'status': str(status).lower().replace(' ', '').replace('-', '')},
    }


def fetch_orders(slug, platform):
    # Official v2 OpenAPI: GET /v2/orders/item/{slug} (Platform header required).
    qslug = quote(slug, safe='')
    url = f'{API_HOST}/v2/orders/item/{qslug}'
    raw = fetch_json(url, 45, platform, 'en')
    if extract_orders(raw):
        return raw
    raise Exception(f'нет активных ордеров для {slug} на {platform}')


def fetch_item_detail(slug, platform):
    qslug = quote(slug, safe='')
    raw = fetch_json(f'{API_HOST}/v2/item/{qslug}', 600, platform, 'en')
    item = extract_single(raw)
    if isinstance(item, dict):
        nested = item.get('item')
        if isinstance(nested, dict):
            item = nested
    name, name_ru, real_slug, thumb = normalize_item(item)
    return {'name': name, 'nameRu': name_ru, 'thumb': thumb}


def safe_quantity(order):
    try:
        return max(0, int(float(order.get('quantity') or 0)))
    except (TypeError, ValueError):
        return 0


def stats(orders):
    def is_online_order(order):
        user = order.get('user') or {}
        if not isinstance(user, dict):
            return False
        return user.get('status') in ('online', 'ingame')
    online = is_online_order
    sell = [o for o in orders if o.get('order_type') == 'sell']
    buy = [o for o in orders if o.get('order_type') == 'buy']
    osell = [o for o in sell if online(o)]
    obuy = [o for o in buy if online(o)]
    sp = sorted(o.get('platinum', 0) for o in sell if o.get('platinum', 0) > 0)
    bp = sorted(o.get('platinum', 0) for o in buy if o.get('platinum', 0) > 0)

    def med(a):
        if not a:
            return 0
        n = len(a)
        return a[n // 2] if n % 2 else round((a[n // 2 - 1] + a[n // 2]) / 2, 1)

    ms, mb = med(sp), med(bp)
    raw_spread = (ms - mb) / ms * 100 if ms and mb else 100
    spread = round(max(0, raw_spread), 1)
    crossed_market = bool(ms and mb and mb >= ms)
    depth = sum(safe_quantity(o) for o in osell)
    demand = sum(safe_quantity(o) for o in obuy)
    demand_score = min(100, demand / max(depth, 1) * 100)
    depth_score = min(100, (len(osell) + len(obuy)) * 4)
    spread_score = 0 if crossed_market else max(0, min(100, 100 - max(spread, 0) * 2.5))
    online_score = min(100, len(osell) * 5 + len(obuy) * 7)
    score = round(
        demand_score * 0.35 + depth_score * 0.25 +
        spread_score * 0.25 + online_score * 0.15
    )
    if crossed_market:
        score = min(score, 35)
    if score >= 85:
        verdict = 'Очень ликвидный'
    elif score >= 70:
        verdict = 'Хороший оборот'
    elif score >= 50:
        verdict = 'Средний оборот'
    else:
        verdict = 'Низкая ликвидность'
    result = {
        'source': 'live',
        'sellCount': len(sell), 'buyCount': len(buy),
        'onlineSell': len(osell), 'onlineBuy': len(obuy),
        'sellDepth': depth, 'buyDepth': demand,
        'medianSell': ms, 'medianBuy': mb,
        'lowestSell': sp[0] if sp else 0, 'highestBuy': bp[-1] if bp else 0,
        'spread': spread, 'rawSpread': round(raw_spread, 1), 'score': score, 'marketScore': score, 'scoreLabel': 'Общий рыночный score', 'verdict': verdict, 'crossedMarket': crossed_market,
        'demandPct': round(min(100, demand_score)),
        'supplyPct': round(min(100, depth / max(demand, 1) * 50)),
        'confidence': min(100, round((len(osell) + len(obuy)) * 3)),
        'sellPrices': sp[:80], 'buyPrices': bp[-80:],
    }
    result.update(liquidity_analysis(result))
    return result


def liquidity_analysis(st):
    sell_count = int(st.get('sellCount') or 0)
    buy_count = int(st.get('buyCount') or 0)
    online_sell = int(st.get('onlineSell') or 0)
    online_buy = int(st.get('onlineBuy') or 0)
    sell_depth = int(st.get('sellDepth') or 0)
    buy_depth = int(st.get('buyDepth') or 0)
    spread = float(st['spread']) if st.get('spread') is not None else 100.0
    crossed_market = bool(st.get('crossedMarket'))
    live_sample_size = online_sell + online_buy
    market_sample_size = sell_count + buy_count
    demand_score = min(100, online_buy * 5 + min(buy_depth, 100) * 0.35)
    supply_score = min(100, online_sell * 3 + min(sell_depth, 100) * 0.20)
    spread_score = 0 if crossed_market else max(0, min(100, 100 - spread * 3))
    market_presence = min(100, (sell_count + buy_count) * 1.5)
    liquidity_score = round(demand_score * 0.45 + supply_score * 0.30 + spread_score * 0.20 + market_presence * 0.05)
    if live_sample_size < 4:
        liquidity_score = min(liquidity_score, 45)
    elif live_sample_size < 8:
        liquidity_score = min(liquidity_score, 65)
    if online_buy == 0:
        liquidity_score = min(liquidity_score, 35)
    if crossed_market:
        liquidity_score = min(liquidity_score, 35)
    risk_score = round(max(0, min(100, 100 - liquidity_score + max(0, spread - 10) * 2)))
    flip_score = round(max(0, min(100, liquidity_score * 0.65 + spread_score * 0.35)))
    if crossed_market:
        risk_score = max(risk_score, 70)
        flip_score = min(flip_score, 25)
    payback_score = 5 if crossed_market else round(max(5, min(99, liquidity_score * 0.6 + (100 - min(100, spread * 3)) * 0.4)))
    if liquidity_score >= 80:
        liquidity_label = 'Высокая ликвидность'
    elif liquidity_score >= 60:
        liquidity_label = 'Хорошая ликвидность'
    elif liquidity_score >= 40:
        liquidity_label = 'Средняя ликвидность'
    else:
        liquidity_label = 'Низкая ликвидность'
    if risk_score >= 70:
        risk_label = 'Высокий риск зависания'
    elif risk_score >= 40:
        risk_label = 'Умеренный риск'
    else:
        risk_label = 'Низкий риск'
    if crossed_market:
        advice = 'Buy-цена не ниже sell-цены. Проверь ордера: рынок может содержать устаревшие или пересекающиеся предложения.'
    elif buy_count == 0:
        advice = 'Покупателей не найдено. Для быстрого флипа предмет сейчас не подходит.'
    elif online_buy == 0:
        advice = 'Активных покупателей сейчас нет. Не закупай предмет большим объёмом.'
    elif spread >= 25:
        advice = 'Спред слишком большой. Покупать стоит только под конкретный заказ.'
    elif liquidity_score >= 80 and flip_score >= 70:
        advice = 'Предмет подходит для быстрого флипа: хороший спрос и приемлемый спред.'
    elif liquidity_score >= 60:
        advice = 'Предмет можно продавать небольшими партиями. Перед покупкой проверь актуальные buy-ордера.'
    else:
        advice = 'Ликвидность слабая. Не стоит закупать большой объём.'
    if liquidity_score >= 85 and online_buy >= 10:
        recommended_volume = 5
    elif liquidity_score >= 70 and online_buy >= 5:
        recommended_volume = 3
    elif liquidity_score >= 55 and online_buy >= 2:
        recommended_volume = 2
    else:
        recommended_volume = 1
    if live_sample_size < 4 and buy_count > 0 and not crossed_market:
        advice = 'Недостаточно активных ордеров для уверенной оценки. Используй предмет осторожно и небольшим объёмом.'
        recommended_volume = 1
    analysis_confidence = 'high' if live_sample_size >= 20 else 'medium' if live_sample_size >= 8 else 'low'
    if crossed_market:
        recommended_volume = 1
    return {
        'liquidityScore': liquidity_score,
        'liquidityLabel': liquidity_label,
        'riskScore': risk_score,
        'riskLabel': risk_label,
        'flipScore': flip_score,
        'recommendedVolume': recommended_volume,
        'advice': advice,
        'analysisConfidence': analysis_confidence,
        'liveSampleSize': live_sample_size,
        'marketSampleSize': market_sample_size,
        'paybackScore': payback_score,
        'crossedMarket': crossed_market,
    }


AI_RATE_LIMITS_LAST_CLEANUP = 0.0


def get_client_ip(handler):
    """Real client IP: proxy headers are trusted only when the TCP peer is a local proxy (cloudflared, nginx)."""
    peer = handler.client_address[0]
    if peer in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        for header in ('CF-Connecting-IP', 'X-Forwarded-For'):
            value = handler.headers.get(header)
            if value:
                return value.split(',')[0].strip()[:64] or peer
    return peer


def ai_rate_limit_ok(client_ip):
    global AI_RATE_LIMITS_LAST_CLEANUP
    now = time.time()
    with LOCK:
        while AI_CALL_TIMESTAMPS and now - AI_CALL_TIMESTAMPS[0] > 60:
            AI_CALL_TIMESTAMPS.pop(0)
        if len(AI_CALL_TIMESTAMPS) >= AI_RATE_LIMIT_PER_MINUTE:
            return False
        if now - AI_RATE_LIMITS_LAST_CLEANUP > 60:
            AI_RATE_LIMITS_LAST_CLEANUP = now
            for ip in list(AI_RATE_LIMITS_BY_IP.keys()):
                timestamps = AI_RATE_LIMITS_BY_IP[ip]
                timestamps[:] = [t for t in timestamps if now - t <= 60]
                if not timestamps:
                    del AI_RATE_LIMITS_BY_IP[ip]
        per_ip = AI_RATE_LIMITS_BY_IP.setdefault(client_ip, [])
        while per_ip and now - per_ip[0] > 60:
            per_ip.pop(0)
        if len(per_ip) >= AI_RATE_LIMIT_PER_IP_PER_MINUTE:
            return False
        AI_CALL_TIMESTAMPS.append(now)
        per_ip.append(now)
        return True


def clamp(value, low, high):
    return max(low, min(high, value))


def sanitize_ai_payload(raw):
    if not isinstance(raw, dict):
        raise Exception('invalid payload')
    slug = str(raw.get('slug', '') or '')[:160].strip()
    if not slug:
        raise Exception('slug required')
    platform = str(raw.get('platform', 'pc') or 'pc').lower()
    if platform not in ('pc', 'ps4', 'xbox', 'switch'):
        platform = 'pc'
    return {
        'slug': slug,
        'platform': platform,
        'name': str(raw.get('name', '') or '')[:200],
        'nameRu': str(raw.get('nameRu', '') or '')[:200],
        'medianSell': clamp(number(raw.get('medianSell', 0)), 0, 1000000),
        'medianBuy': clamp(number(raw.get('medianBuy', 0)), 0, 1000000),
        'spread': clamp(number(raw.get('spread', 0)), 0, 1000),
        'sellCount': clamp(int(number(raw.get('sellCount', 0))), 0, 100000),
        'buyCount': clamp(int(number(raw.get('buyCount', 0))), 0, 100000),
        'onlineSell': clamp(int(number(raw.get('onlineSell', 0))), 0, 100000),
        'onlineBuy': clamp(int(number(raw.get('onlineBuy', 0))), 0, 100000),
        'liquidityScore': clamp(int(number(raw.get('liquidityScore', 0))), 0, 100),
        'riskScore': clamp(int(number(raw.get('riskScore', 0))), 0, 100),
        'flipScore': clamp(int(number(raw.get('flipScore', 0))), 0, 100),
        'recommendedVolume': clamp(int(number(raw.get('recommendedVolume', 1))), 1, 100),
        'paybackScore': clamp(int(number(raw.get('paybackScore', 0))), 0, 100),
        'crossedMarket': raw.get('crossedMarket') is True,
    }


def load_ai_payload(slug, platform, name='', name_ru=''):
    price_data = fetch_live_price(slug, platform)
    if not price_data:
        raise Exception('нет активных ордеров для этого предмета')
    return sanitize_ai_payload({
        'slug': slug,
        'platform': platform,
        'name': name,
        'nameRu': name_ru,
        'medianSell': price_data.get('medianSell', 0),
        'medianBuy': price_data.get('medianBuy', 0),
        'spread': price_data.get('spread', 100),
        'sellCount': price_data.get('sellCount', 0),
        'buyCount': price_data.get('buyCount', 0),
        'onlineSell': price_data.get('onlineSell', 0),
        'onlineBuy': price_data.get('onlineBuy', 0),
        'liquidityScore': price_data.get('liquidityScore', 0),
        'riskScore': price_data.get('riskScore', 0),
        'flipScore': price_data.get('flipScore', 0),
        'recommendedVolume': price_data.get('recommendedVolume', 1),
        'paybackScore': price_data.get('paybackScore', 5),
        'crossedMarket': price_data.get('crossedMarket', False),
    })


def validate_ai_result(result, payload=None):
    if not isinstance(result, dict):
        raise Exception('AI returned an unexpected format')
    required = ('verdict', 'summary', 'buyPrice', 'sellPrice', 'recommendedVolume', 'risks', 'action')
    for key in required:
        if key not in result:
            raise Exception(f'AI response field is missing: {key}')
    if not isinstance(result['risks'], list):
        raise Exception('AI response risks must be an array')
    result['risks'] = [str(item)[:300] for item in result['risks'][:3]]
    result['verdict'] = str(result['verdict'])[:100]
    result['summary'] = str(result['summary'])[:1000]
    result['action'] = str(result['action'])[:1000]
    if payload:
        # Prices stay a deterministic backend fact, not an AI-derived value — otherwise
        # the AI's narrative could reference a number the rest of the UI disagrees with.
        result['buyPrice'] = payload['medianBuy']
        result['sellPrice'] = payload['medianSell']
    else:
        result['buyPrice'] = clamp(number(result['buyPrice']), 0, 1000000)
        result['sellPrice'] = clamp(number(result['sellPrice']), 0, 1000000)
    result['recommendedVolume'] = clamp(int(number(result['recommendedVolume'], 1)), 1, 100)
    return result


AI_SYSTEM_PROMPT = (
    'Ты — аналитик внутриигрового рынка Warframe Market. Тебе присылают уже '
    'посчитанные метрики по одному предмету в формате JSON, включая paybackScore '
    '(шанс окупа по шкале 0-100). Отвечай ТОЛЬКО валидным JSON, без markdown-разметки '
    'и пояснений вокруг, строго в такой форме:\n'
    '{"verdict": "...", "summary": "...", "buyPrice": число, "sellPrice": число, '
    '"recommendedVolume": число, "risks": ["...", "..."], "action": "..."}\n'
    'verdict — короткая фраза из 2-4 слов с общим выводом. summary — 1-2 предложения. '
    'risks — список из 1-3 коротких пунктов. action — одно предложение с конкретным '
    'советом. buyPrice и sellPrice — используй присланные medianBuy/medianSell напрямую, '
    'сервер всё равно подставит точные значения сам, важно только не написать в тексте '
    'советов цифру, которая противоречит входным данным. Все тексты пиши на русском. Не '
    'придумывай цифры и факты, которых нет во входных данных — используй только '
    'присланные значения.'
)

# Model is overridable without editing code: GEMINI_MODEL=gemini-2.5-flash python server.py
# Free-tier quotas differ per model and change without notice (gemini-2.5-flash had 250 req/day;
# gemini-3.8-flash is reported at ~20 req/day on the free tier). Check https://aistudio.google.com
# for current limits and switch via the env var if the AI button starts answering with 429.
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')
# Gemini 3.x counts thinking tokens against maxOutputTokens; 'low' keeps the JSON from being cut off.
GEMINI_THINKING_LEVEL = os.environ.get('GEMINI_THINKING_LEVEL', 'low')
# Tried once when the main model answers 429/404/5xx (e.g. the free daily quota is spent). Empty string disables it.
GEMINI_FALLBACK_MODEL = os.environ.get('GEMINI_FALLBACK_MODEL', 'gemini-2.5-flash-lite')

AI_RESPONSE_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'verdict': {'type': 'STRING'},
        'summary': {'type': 'STRING'},
        'buyPrice': {'type': 'NUMBER'},
        'sellPrice': {'type': 'NUMBER'},
        'recommendedVolume': {'type': 'INTEGER'},
        'risks': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'action': {'type': 'STRING'},
    },
    'required': ['verdict', 'summary', 'buyPrice', 'sellPrice', 'recommendedVolume', 'risks', 'action'],
}


def call_ai_analysis(payload):
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        raise Exception('AI service is not configured on the server (GEMINI_API_KEY missing)')
    user_content = json.dumps({
        'name': payload['nameRu'] or payload['name'] or payload['slug'],
        'medianSell': payload['medianSell'],
        'medianBuy': payload['medianBuy'],
        'spread': payload['spread'],
        'sellCount': payload['sellCount'],
        'buyCount': payload['buyCount'],
        'onlineSell': payload['onlineSell'],
        'onlineBuy': payload['onlineBuy'],
        'liquidityScore': payload['liquidityScore'],
        'riskScore': payload['riskScore'],
        'flipScore': payload['flipScore'],
        'recommendedVolume': payload['recommendedVolume'],
        'paybackScore': payload['paybackScore'],
        'crossedMarket': payload['crossedMarket'],
    }, ensure_ascii=False)
    generation_config = {
        'responseMimeType': 'application/json',
        'responseSchema': AI_RESPONSE_SCHEMA,
        'maxOutputTokens': 2048,
    }
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        models.append(GEMINI_FALLBACK_MODEL)
    result = None
    for index, model in enumerate(models):
        config = dict(generation_config)
        if model.startswith('gemini-3'):
            config['thinkingConfig'] = {'thinkingLevel': GEMINI_THINKING_LEVEL}
        body = json.dumps({
            'contents': [{'role': 'user', 'parts': [{'text': user_content}]}],
            'systemInstruction': {'parts': [{'text': AI_SYSTEM_PROMPT}]},
            'generationConfig': config,
        }).encode('utf-8')
        req = urllib.request.Request(
            f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
            data=body,
            headers={'x-goog-api-key': api_key, 'content-type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            break
        except HTTPError as error:
            if index + 1 < len(models) and error.code in (404, 429, 500, 503):
                print(f'AI model {model} failed with HTTP {error.code}, trying {models[index + 1]}')
                continue
            raise
    candidates = result.get('candidates') or []
    if not candidates:
        block_reason = (result.get('promptFeedback') or {}).get('blockReason')
        raise Exception(f'AI returned no candidates (blockReason={block_reason})')
    parts = (candidates[0].get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts).strip()
    if text.startswith('```'):
        text = text.split('```', 2)[1]
        if text.startswith('json'):
            text = text[4:]
        text = text.strip()
    parsed = json.loads(text)
    return validate_ai_result(parsed, payload)


def fetch_live_offers(slug, platform, limit=5):
    """Top in-game/online offers with player names for the one-click /w whisper in the item modal."""
    offers = {'sell': [], 'buy': []}
    for o in extract_orders(fetch_orders(slug, platform)):
        if not isinstance(o, dict):
            continue
        n = normalize_order(o)
        user = o.get('user') or o.get('player') or o.get('owner') or o.get('account')
        if not isinstance(user, dict) or n['order_type'] not in offers or n['platinum'] <= 0:
            continue
        status = n['user']['status']
        name = user.get('ingameName') or user.get('ingame_name') or user.get('name') or ''
        if status not in ('ingame', 'online') or not name:
            continue
        offers[n['order_type']].append({
            'name': str(name)[:40],
            'platinum': n['platinum'],
            'quantity': int(n['quantity'] or 1),
            'status': status,
        })
    offers['sell'].sort(key=lambda x: (x['platinum'], x['status'] != 'ingame'))
    offers['buy'].sort(key=lambda x: (-x['platinum'], x['status'] != 'ingame'))
    return {'slug': slug, 'platform': platform, 'sell': offers['sell'][:limit], 'buy': offers['buy'][:limit]}


def fetch_item_history(slug, platform):
    """Real closed-trade history: hourly points for 48h and daily points for 90d (v1 statistics endpoint)."""
    raw = fetch_json(f'{API_HOST}/v1/items/{quote(slug, safe="")}/statistics', 600, platform)
    closed = ((raw or {}).get('payload') or {}).get('statistics_closed') or {}

    def pack(entries):
        out = []
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict) or not e.get('datetime'):
                continue
            out.append({
                't': e['datetime'],
                'median': number(e.get('median')),
                'avg': number(e.get('avg_price')),
                'min': number(e.get('min_price')),
                'max': number(e.get('max_price')),
                'volume': int(number(e.get('volume'))),
            })
        out.sort(key=lambda x: x['t'])
        return out

    return {'slug': slug, 'platform': platform, 'hours': pack(closed.get('48hours'))[-48:], 'days': pack(closed.get('90days'))[-90:]}


def fetch_live_price(slug, platform):
    raw = fetch_orders(slug, platform)
    orders = [normalize_order(o) for o in extract_orders(raw)]
    if not orders:
        return None
    return stats(orders)


# ---------- Telegram price alerts (optional: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERTS_KEY >= 8 chars) ----------
TG_TOKEN = os.environ.get('8877254805:AAGnIbs5njsGp7qzrfnMQXAucCWrk3ykILk', '').strip()
TG_CHAT_ID = os.environ.get('8773319339', '').strip()
TG_KEY = os.environ.get('kavtrufol', '').strip()
TG_ENABLED = bool(TG_TOKEN and TG_CHAT_ID and len(TG_KEY) >= 8)
TG_STATE_FILE = os.path.join(BASE_DIR, 'alerts_state.json')
TG_INTERVAL = 180
TG_LOCK = threading.Lock()
TG_ALERTS = {}  # slug -> {'target', 'platform', 'name', 'fired'}
SLUG_RE = re.compile(r'^[a-z0-9_]{1,80}$')


def tg_load():
    try:
        with open(TG_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            TG_ALERTS.update({k: v for k, v in data.items() if isinstance(v, dict) and SLUG_RE.match(k)})
    except (OSError, ValueError):
        pass


def tg_save():
    tmp = TG_STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(TG_ALERTS, f, ensure_ascii=False)
        os.replace(tmp, TG_STATE_FILE)
    except OSError as error:
        print(f'Alert state not saved: {error}')


def tg_send(text):
    body = json.dumps({'chat_id': TG_CHAT_ID, 'text': text, 'disable_web_page_preview': True}).encode('utf-8')
    req = urllib.request.Request(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage', data=body,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status == 200


def tg_sync(platform, items):
    """Replace the alert list with the client's one; keep the 'fired' mark of unchanged alerts."""
    fresh = {}
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        slug = str(item.get('slug', ''))
        target = number(item.get('target'))
        if not SLUG_RE.match(slug) or not (0 < target <= 1000000):
            continue
        old = TG_ALERTS.get(slug)
        same = bool(old) and old.get('target') == target and old.get('platform') == platform
        fresh[slug] = {'target': target, 'platform': platform, 'name': str(item.get('name', slug))[:60],
                       'fired': old.get('fired') if same else None}
    TG_ALERTS.clear()
    TG_ALERTS.update(fresh)
    tg_save()
    return [s for s, a in TG_ALERTS.items() if a.get('fired')]


def tg_check_once():
    with TG_LOCK:
        pending = [(s, dict(a)) for s, a in TG_ALERTS.items() if not a.get('fired')]
    LOCAL.low = True
    try:
        for slug, alert in pending:
            try:
                price = fetch_live_price(slug, alert['platform'])
            except Exception as error:
                print(f'Alert check failed for {slug}: {type(error).__name__}: {error}')
                continue
            value = number(price.get('medianSell', 0)) if isinstance(price, dict) else 0
            if value <= 0 or value > alert['target']:
                continue
            try:
                tg_send(f"🔔 {alert['name']}: цена {value:g} p (цель ≤ {alert['target']:g} p, {alert['platform'].upper()})")
            except Exception as error:
                print(f'Telegram send failed: {type(error).__name__}: {error}')
                continue
            with TG_LOCK:
                current = TG_ALERTS.get(slug)
                if current and current.get('target') == alert['target']:
                    current['fired'] = time.time()
                    tg_save()
    finally:
        LOCAL.low = False


def tg_alert_loop():
    while True:
        try:
            tg_check_once()
        except Exception as error:
            print(f'Alert loop error: {type(error).__name__}: {error}')
        time.sleep(TG_INTERVAL)


ICON_PNG_B64 = (
    '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'
    'q/mzV04+921Vq9s4tta4NzHGOjffGLHW2f08/5O+OetvbdYLFEWXj7w3e/KsrverkYbZJjK2ZXL6llutSVQU+aMA+eHeoDAQfCkv'
    'CEfWJJN79m669VDSFO5/eQqS0+NLvUp2Y5CzRes7AhBitnF8+fB7SpPgur/ur4e1ukFH/9NXFi9eYMWpp591fxYzPGKtWGvzSoAV'
    'a8j/mb5DFM/OXDnynq5R/7q8REjR1ns+ms6DiwLHX2duvssCRe3BgapFYu22Bx6uT9TESJ/NAzNxxLgXlnqW2Bq6fPg91poqdgMg'
    'Ari+L0+PMYEQEYvIzPEPcb0PgvvPNdW80vzgq1/mSFuX/c/Nva8BWJtXfXPH3+anw6xN31wlwMqZV14SS33tBWrR9KHbR6Y3iYjS'
    'EenAxOdHwFQhKeTeIk1E9cmpbR990LT62pggIqpGZ19/dfbEyWgEXdFLvMTJopk9eYLTBXCZPcmyDe0WRYZRJKKBfdBLLtHkzp/J'
    'z2mLqCi69N676AIaBPe/Pq6PfP07syeP18bGrTF+34u4hd0Vbim7lStMpFixMKervRUJiSItJlG16OLbP5g/e2VkeoPtz6Q/ZpvI'
    '6ObJ6YOHzr72sh4ZJZOQEImI/13arst0ITBrnSzM77j/iYldm+M5YdW/a1IpNs3kwhuvnmk1b/ljPz6+bWuC3ZOdPJWIFy/NzZ44'
    'pmv1YDtYcUl8MSZc0jcdTOszvC+/VH9Gii+ItVFj5PJ778yevKDrCtnP6+tW2YQ++OrvU+q8+4SPtbnvb4N33EYwY8VYss7ZT1NA'
    'Yg1ZVwmwrNTihfPnf/C6bvQ1C8RMm+/+iNJhaVcp/377W/ptSulGffsDD4ulfraHOPf/8uHDzatXbKv57u/++9lTZ6NRhTig8rmK'
    'GnLxB68vXDinaxE5H0WkyurIIBv39S0AnW8frwFuj6yq1eZOnzz9wvPRKOFyv47uf21cnXnl9fNvvK4bIzaO0/5Ob/FtavTdUvig'
    '5BuKgRUbCAOlgmFF5MzLL/Q7CxTTxgOH6lMbicj3ekYcVfV9+q+qKLLGTO7ev/HAAdPs6/AfJrJGzv/gNZvERJTMzb7zW78+e+IM'
    'NKBSAVTEx77xtIljYi7aGelVtqXHl+X6CwAP7Ru1vVM1uVuIdXTkD3/fxoTzltf1tqJ3fuv/NK2WWLGZ9c8qven7IlbEuIXw6YeU'
    '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'
    '3eu3lScDufQvyYqur9XO7ywnByTLenS9HvBaSUBSkG0JREF86Z39YSWmcPdArgFWqSiembn07lsbb95h1mUWyNpoRF3+4Pj5N17T'
    'jYYr/wb1Xhvs/LKl0W+FLc1EXVskiy+39HynSumzfG03gFT8G9LLBb+cwFSW1rdh7PgcagHoqgGU126WDgWWes2kpARL5vpkac9k'
    'Zdmt1TTyq/ZTZOkkUO//zlIvhF/kHMZwUqrZpIVgSsdB55MMXHBghYhOv/jc/s9+al26/yKiavTBV/4gnp2tjY+Z8MSvX/WVpX3C'
    'FT3hDNdCr0737vhV9dgH7lqVnu7M3qz/sOwEHqpYt+NL1HsoQOX+w6UvBil1C61xz9cA9Q1VuEyyir9OT2IQpKUKR4MD6+/agISz'
    'MiZbISWuF0g1Ghd+8PrcmcsjmzdKvL6yQCKqrubPzpx+8TndqNvEkEv1pPXefN4n5XOzs+5PKkx+praueRmmJ6LyYpNVu+mWGGY6'
    'pNafiNSwXfJL1nOJK9/t9KpLr5dH4c0PGK9+o05v1/0m6fDW4RfJuwSX+zus4JfuFLC1+47FqS3BQyzOMvNZb6X1woUL5177XtSg'
    'YFnjurD/1kYjfPqlF+dOn+QossakB74ky/+45yHo/iz5/ra4/iW/qivTfdf8Rtf8Vv2T6Vqv1d4cf+7JrgyThzGMB8G6xgFUGQpQ'
    'T9l4XrZB7f0LPBi+vQzyT+9Yq68MAvKiQFYKZgqmQYRZIJbTLz67/7OfXGdZINYqWTAffOX3VBSJMbndt+JNv2R7sKioAVQe5trJ'
    '8V+d2n73W0Pa1z+u6bUqvZuaNsOwXkz/kEYAPUhwtf/PS788a+msy2C8re0vtgZ3ZocggIL1T+FaD8nXFHtjJ9aoeuPCD96YO3NZ'
    '1dX1GMa0Zu7/KJ///luXD7+na3WbGFfx9ave8wpwuO/F9/mk1REKZmW2Hf6SvEF07d2G0nW0ttdqzxaGSzmf9WX9h1cAekgHrVgG'
    'Kg026NszUz13vbQVVipC+KIepFkgEbFW6Wjh4vlzr6+vLJAQMx37xldNq0nkTnWFPT/5hFQq9fyEC34kGPYQJtlkXVz4y75Wudr0'
    'L23ehzWyVEP+CvPSr2bphW3/aMUO/I1m61fyW/NSb71F6FKlBlmtIjNmpf3m3oN1Rv/0C9+xyXopAovohpo9feXMd1+IRkZskqQl'
    '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'
    'npe8wPoDRAAALMssBuM+uSIa4D4+Tq7+FMP6A0QAAKxFHFAMBTqsGaM1iwmWWtwF0w/WXABwCYEbVgPaBnz6je/tK7m46q+tXkQi'
    'Vdke7n4mGbcuQAQAQK+WV6gHq84qN8fSF7+7orWfl/ynYP0BBACAVQkFQo2QCisra/BQVmrYYfoBBACAVQwFljL5VYsjV8niL8+q'
    'w/oDCAAAaxcKtFtaWRszzGv43QBAAABYaSjQsxispdGH6QcQAAAGQQaugzWG6QcQAAD6Z2dlkB4MAGsuALjgALiGmACmHyACAGA9'
    'xgTSx38LAAgAAIOoBKuoB7D4AAIAwNDrAQDDDaaBAgAABAAAAAAEAAAAAAQAAAAABAAAAMA6IkJbAwAAIAIAAAAAAQAAAAABAAAA'
    'AAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAsOpERNgIAAAAiAAAAABA'
    'AAAAAEAAAAAArEMilAAAAAARAAAAAAgAAAAACAAAAAAIAAAAAAgAAAAACAAAAAAIAAAAAAgAAAAACAAAAAAIAAAAAAgAAAAACAAA'
    'AAAIAAAAAAgAAAAACAAAAAAIAAAAAAgAAAAACAAAAAAIAAAAAAgAAAAACAAAAIASERHjWQAAAEQAAAAAIAAAAAAgAAAAANYfESoA'
    'AACACAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAADAABJhGCgAACACAAAAAAEAAAAAAQAAAAABAAAA'
    'AAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAMChEhFkQAACACAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAE'
    'AAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAEAAAAAAQAAAAABAAAAAAE'
    'AAAAIAAAAAAgAAAAAG4E/v+4h25UUOh21gAAAABJRU5ErkJggg=='
)
ICON_PNG = base64.b64decode(ICON_PNG_B64)
MANIFEST = {
    'name': 'MarketPulse', 'short_name': 'MarketPulse', 'description': 'Аналитика фарма платины Warframe',
    'start_url': '/', 'scope': '/', 'display': 'standalone', 'orientation': 'portrait', 'lang': 'ru',
    'background_color': '#0b1020', 'theme_color': '#0B0F14',
    'icons': [{'src': '/icon.png', 'sizes': '192x192 512x512', 'type': 'image/png', 'purpose': 'any maskable'}],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        super().end_headers()

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', ALLOWED_ORIGIN or '*')
        self.end_headers()
        self.wfile.write(raw)

    def platform(self, qs):
        value = qs.get('platform', ['pc'])[0].lower()
        return {
            'pc': 'pc', 'ps4': 'ps4', 'playstation': 'ps4', 'psn': 'ps4',
            'xbox': 'xbox', 'xb1': 'xbox', 'switch': 'switch',
        }.get(value, 'pc')

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', ALLOWED_ORIGIN or '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Platform, Language')
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            plat = self.platform(qs)

            if path in ('/', '/index.html'):
                try:
                    with open(INDEX_FILE, 'rb') as f:
                        body = f.read()
                except FileNotFoundError:
                    return self.send_json({'error': 'public/index.html not found'}, 500)
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(body)
                return

            if path == '/api/tg/status':
                return self.send_json({'enabled': TG_ENABLED})

            if path == '/manifest.webmanifest':
                raw = json.dumps(MANIFEST, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/manifest+json; charset=utf-8')
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(raw)
                return

            if path in ('/icon.png', '/apple-touch-icon.png'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(ICON_PNG)
                return

            if path == '/sw.js':
                try:
                    with open(os.path.join(BASE_DIR, 'public', 'sw.js'), 'rb') as f:
                        body = f.read()
                except FileNotFoundError:
                    return self.send_json({'error': 'sw.js not found'}, 404)
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                self.end_headers()
                self.wfile.write(body)
                return

            if path == '/api/health':
                return self.send_json({
                    'ok': True,
                    'cacheEntries': len(CACHE),
                    'imageCacheEntries': len(IMAGE_CACHE),
                    'uptimeSeconds': round(time.time() - START_TIME),
                })

            if path == '/api/image':
                src = qs.get('src', [''])[0].strip()
                source = urlparse(src)
                allowed_hosts = {'warframe.market', 'www.warframe.market', 'api.warframe.market'}
                if source.scheme != 'https' or source.hostname not in allowed_hosts:
                    return self.send_json({'error': 'invalid image source'}, 400)
                with IMAGE_LOCK:
                    cached = IMAGE_CACHE.get(src)
                    if cached:
                        if time.time() - cached[2] >= 86400:
                            IMAGE_CACHE.pop(src, None)
                            cached = None
                        else:
                            IMAGE_CACHE.move_to_end(src)
                if cached:
                    content_type, body, created = cached
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    request = urllib.request.Request(src, headers={
                        'User-Agent': UA,
                        'Accept': 'image/avif,image/webp,image/*,*/*',
                        'Referer': 'https://warframe.market/',
                    })
                    with urllib.request.urlopen(request, timeout=10) as response:
                        content_type = (response.headers.get('Content-Type', '') or '').split(';', 1)[0].lower()
                        content_length = response.headers.get('Content-Length')
                        if content_length:
                            try:
                                if int(content_length) > 5 * 1024 * 1024:
                                    return self.send_json({'error': 'image is too large'}, 413)
                            except ValueError:
                                pass
                        if not content_type.startswith('image/'):
                            return self.send_json({'error': 'upstream response is not an image'}, 502)
                        max_size = 5 * 1024 * 1024
                        chunks = []
                        size = 0
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_size:
                                return self.send_json({'error': 'image is too large'}, 413)
                            chunks.append(chunk)
                        body = b''.join(chunks)
                    with IMAGE_LOCK:
                        IMAGE_CACHE[src] = (content_type, body, time.time())
                        IMAGE_CACHE.move_to_end(src)
                        while len(IMAGE_CACHE) > IMAGE_CACHE_MAX_SIZE:
                            IMAGE_CACHE.popitem(last=False)
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                except Exception as error:
                    print(f'Image proxy error: {type(error).__name__}: {error}')
                    return self.send_json({'error': 'image unavailable'}, 502)

            if path == '/api/items':
                try:
                    offset = max(0, int(qs.get('offset', ['0'])[0]))
                except Exception:
                    offset = 0
                try:
                    limit = int(qs.get('limit', ['60'])[0])
                except Exception:
                    limit = 60
                limit = max(12, min(100, limit))
                all_items = dedup_items([public_item(x) for x in get_items(plat)])
                cat = (qs.get('category', ['all'])[0] or 'all').lower()
                prime = (qs.get('prime', ['0'])[0] or '0') == '1'
                if cat != 'all' or prime:
                    def matches_filter(x):
                        tags = {str(t).lower() for t in (x.get('tags') or [])}
                        if prime and 'prime' not in tags:
                            return False
                        if cat != 'all':
                            if cat == 'warframes' and 'warframe' not in tags: return False
                            if cat == 'weapons' and 'weapon' not in tags: return False
                            if cat == 'mods' and 'mod' not in tags: return False
                            if cat == 'relics' and 'relic' not in tags: return False
                            if cat == 'resources' and not ({'resource', 'material'} & tags): return False
                            if cat == 'arcanes' and not ({'arcane', 'arcane_enhancement'} & tags): return False
                            if cat == 'other' and ({'warframe', 'weapon', 'mod', 'relic',
                                                    'resource', 'material', 'arcane',
                                                    'arcane_enhancement'} & tags): return False
                        return True
                    all_items = [x for x in all_items if matches_filter(x)]
                page = all_items[offset:offset + limit]
                return self.send_json({
                    'items': page, 'offset': offset, 'limit': limit,
                    'total': len(all_items),
                    'hasMore': offset + len(page) < len(all_items),
                })

            if path.startswith('/api/item-detail/'):
                slug = unquote(path[len('/api/item-detail/'):].split('?')[0]).strip()
                if not slug:
                    return self.send_json({'error': 'slug required'}, 400)
                if len(slug) > 160:
                    return self.send_json({'error': 'slug too long'}, 414)
                try:
                    art = fetch_item_detail(slug, plat)
                    return self.send_json({
                        'slug': slug,
                        'name': art.get('name', ''),
                        'nameRu': art.get('nameRu', ''),
                        'thumb': art.get('thumb', ''),
                    })
                except Exception as error:
                    print(f'Item detail failed for {slug}: {type(error).__name__}: {error}')
                    return self.send_json({'slug': slug, 'name': '', 'nameRu': '', 'thumb': ''}, 429 if is_rate_limited(error) else 502)

            if path.startswith('/api/history/'):
                slug = unquote(path[len('/api/history/'):].split('?')[0]).strip()
                if not slug:
                    return self.send_json({'error': 'slug required'}, 400)
                if len(slug) > 160:
                    return self.send_json({'error': 'slug too long'}, 414)
                try:
                    return self.send_json(fetch_item_history(slug, plat))
                except Exception as error:
                    print(f'History failed for {slug}: {type(error).__name__}: {error}')
                    return self.send_json({'slug': slug, 'hours': [], 'days': []}, 429 if is_rate_limited(error) else 502)

            if path.startswith('/api/orders/'):
                slug = unquote(path[len('/api/orders/'):].split('?')[0]).strip()
                if not slug:
                    return self.send_json({'error': 'slug required'}, 400)
                if len(slug) > 160:
                    return self.send_json({'error': 'slug too long'}, 414)
                try:
                    return self.send_json(fetch_live_offers(slug, plat))
                except Exception as error:
                    print(f'Orders failed for {slug}: {type(error).__name__}: {error}')
                    return self.send_json({'slug': slug, 'sell': [], 'buy': []}, 429 if is_rate_limited(error) else 502)

            if path.startswith('/api/price/'):
                slug = unquote(path[len('/api/price/'):].split('?')[0]).strip()
                if not slug:
                    return self.send_json({'error': 'slug required'}, 400)
                if len(slug) > 160:
                    return self.send_json({'error': 'slug too long'}, 414)
                try:
                    LOCAL.low = True
                    try:
                        price_data = fetch_live_price(slug, plat)
                    finally:
                        LOCAL.low = False
                    if not price_data:
                        return self.send_json({'slug': slug, 'available': False})
                    median_sell = number(price_data.get('medianSell', 0))
                    if median_sell <= 0:
                        return self.send_json({'slug': slug, 'available': False})
                    return self.send_json({
                        'slug': slug,
                        'available': True,
                        'price': median_sell,
                        'medianBuy': price_data.get('medianBuy', 0),
                        'sellCount': price_data.get('sellCount', 0),
                        'buyCount': price_data.get('buyCount', 0),
                        'liquidityScore': price_data.get('liquidityScore', 0),
                        'riskScore': price_data.get('riskScore', 0),
                        'flipScore': price_data.get('flipScore', 0),
                        'recommendedVolume': price_data.get('recommendedVolume', 1),
                        'paybackScore': price_data.get('paybackScore', 5),
                        'liquidityLabel': price_data.get('liquidityLabel', ''),
                        'riskLabel': price_data.get('riskLabel', ''),
                        'analysisConfidence': price_data.get('analysisConfidence', 'low'),
                        'advice': price_data.get('advice', ''),
                        'liveSampleSize': price_data.get('liveSampleSize', 0),
                        'marketSampleSize': price_data.get('marketSampleSize', 0),
                        'crossedMarket': price_data.get('crossedMarket', False),
                        'rawSpread': price_data.get('rawSpread', price_data.get('spread', 100)),
                        'marketScore': price_data.get('marketScore', price_data.get('score', 0)),
                        'spread': price_data.get('spread', 100),
                        'onlineSell': price_data.get('onlineSell', 0),
                        'onlineBuy': price_data.get('onlineBuy', 0),
                    })
                except Exception as error:
                    print(f'Price failed for {slug}: {type(error).__name__}: {error}')
                    return self.send_json({'slug': slug, 'available': False}, 429 if is_rate_limited(error) else 502)

            if path.startswith('/api/item/'):
                slug = unquote(path[len('/api/item/'):].split('?')[0]).strip()
                if not slug:
                    return self.send_json({'error': 'slug required'}, 400)
                if len(slug) > 160:
                    return self.send_json({'error': 'slug too long'}, 414)
                try:
                    art = fetch_item_detail(slug, plat)
                except Exception as error:
                    print(f'Item detail failed for {slug}: {type(error).__name__}: {error}')
                    art = {'name': '', 'nameRu': '', 'thumb': ''}
                stats_data = None
                orders_error = None
                orders_rate_limited = False
                try:
                    raw_orders = fetch_orders(slug, plat)
                    orders = [normalize_order(o) for o in extract_orders(raw_orders)]
                    if orders:
                        stats_data = stats(orders)
                except HTTPError as error:
                    orders_rate_limited = is_rate_limited(error)
                    orders_error = str(error)
                    print(f'Orders failed for {slug}: {type(error).__name__}: {error}')
                except Exception as error:
                    orders_error = str(error)
                    print(f'Orders failed for {slug}: {type(error).__name__}: {error}')
                response = {
                    'slug': slug,
                    'name': art.get('name', ''),
                    'nameRu': art.get('nameRu', ''),
                    'thumb': art.get('thumb', ''),
                    'stats': stats_data,
                    'statsAvailable': stats_data is not None,
                    'statsRateLimited': orders_rate_limited,
                }
                if orders_rate_limited:
                    response['statsError'] = 'Лимит запросов к API'
                elif orders_error:
                    response['statsError'] = 'Активные ордера недоступны'
                return self.send_json(response)

            if path == '/api/search':
                q = (qs.get('q', [''])[0] or '').strip()
                try:
                    items = get_items(plat)
                except Exception as error:
                    print(f'Search failed: {type(error).__name__}: {error}')
                    return self.send_json({'error': 'Поиск временно недоступен'}, 502)
                results = [public_item(x) for x in search_items(items, q)][:20]
                return self.send_json({'items': results})

            return self.send_json({'error': 'not found'}, 404)
        except Exception as error:
            print(f'Unhandled request error: {type(error).__name__}: {error}')
            try:
                return self.send_json({'error': 'Внутренняя ошибка сервера'}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == '/api/ai/analyze':
                try:
                    length = int(self.headers.get('Content-Length', 0) or 0)
                except (TypeError, ValueError):
                    return self.send_json({'ok': False, 'error': 'некорректный Content-Length'}, 400)
                if length <= 0 or length > 20000:
                    return self.send_json({'ok': False, 'error': 'некорректный запрос'}, 400)
                raw_body = self.rfile.read(length)
                try:
                    raw = json.loads(raw_body.decode('utf-8'))
                except Exception:
                    return self.send_json({'ok': False, 'error': 'некорректный JSON'}, 400)
                if not isinstance(raw, dict):
                    return self.send_json({'ok': False, 'error': 'invalid payload'}, 400)
                slug = str(raw.get('slug', '') or '')[:160].strip()
                if not slug:
                    return self.send_json({'ok': False, 'error': 'slug required'}, 400)
                platform = str(raw.get('platform', 'pc') or 'pc').lower()
                if platform not in ('pc', 'ps4', 'xbox', 'switch'):
                    platform = 'pc'
                name = str(raw.get('name', '') or '')[:200]
                name_ru = str(raw.get('nameRu', '') or '')[:200]

                try:
                    payload = load_ai_payload(slug, platform, name, name_ru)
                except HTTPError as error:
                    print(f'AI payload fetch failed for {slug}: HTTP {error.code}')
                    if is_rate_limited(error):
                        return self.send_json({'ok': False, 'error': 'Лимит запросов к API рынка, попробуй позже', 'rateLimited': True}, 429)
                    return self.send_json({'ok': False, 'error': 'Не удалось получить данные рынка'}, 502)
                except Exception as error:
                    print(f'AI payload fetch failed for {slug}: {type(error).__name__}: {error}')
                    return self.send_json({'ok': False, 'error': 'Недостаточно live-данных для анализа этого предмета'}, 502)

                cache_data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                cache_digest = hashlib.sha256(cache_data.encode('utf-8')).hexdigest()[:16]
                cache_key = payload['slug'] + ':' + payload['platform'] + ':' + cache_digest
                now = time.time()
                with LOCK:
                    hit = AI_CACHE.get(cache_key)
                if hit and now - hit[0] < AI_CACHE_TTL:
                    response = dict(hit[1])
                    response['ok'] = True
                    response['cached'] = True
                    return self.send_json(response)

                client_ip = get_client_ip(self)
                if not ai_rate_limit_ok(client_ip):
                    return self.send_json({'ok': False, 'error': 'Слишком много запросов к ИИ, попробуй через минуту', 'rateLimited': True}, 429)

                try:
                    result = call_ai_analysis(payload)
                except HTTPError as error:
                    print(f'AI analyze failed for {payload["slug"]}: HTTP {error.code}')
                    if is_rate_limited(error):
                        return self.send_json({'ok': False, 'error': 'ИИ временно перегружен, попробуй позже', 'rateLimited': True}, 429)
                    return self.send_json({'ok': False, 'error': 'ИИ-сервис недоступен'}, 502)
                except Exception as error:
                    print(f'AI analyze failed for {payload["slug"]}: {type(error).__name__}: {error}')
                    return self.send_json({'ok': False, 'error': 'Не удалось получить ответ от ИИ'}, 502)

                with LOCK:
                    AI_CACHE[cache_key] = (time.time(), result)
                    AI_CACHE.move_to_end(cache_key)
                    while len(AI_CACHE) > CACHE_MAX_SIZE:
                        AI_CACHE.popitem(last=False)

                response = dict(result)
                response['ok'] = True
                response['cached'] = False
                return self.send_json(response)

            if path in ('/api/tg/sync', '/api/tg/test'):
                if not TG_ENABLED:
                    return self.send_json({'error': 'telegram disabled'}, 404)
                try:
                    length = int(self.headers.get('Content-Length', 0) or 0)
                except (TypeError, ValueError):
                    return self.send_json({'error': 'bad length'}, 400)
                if length <= 0 or length > 30000:
                    return self.send_json({'error': 'bad length'}, 400)
                try:
                    raw = json.loads(self.rfile.read(length).decode('utf-8'))
                except Exception:
                    return self.send_json({'error': 'bad json'}, 400)
                if not isinstance(raw, dict) or not hmac.compare_digest(str(raw.get('key', '')).encode('utf-8'), TG_KEY.encode('utf-8')):
                    time.sleep(1.0)
                    return self.send_json({'error': 'bad key'}, 403)
                if path == '/api/tg/test':
                    try:
                        tg_send('✅ MarketPulse подключён. Алерты цен будут приходить сюда.')
                    except Exception as error:
                        print(f'Telegram test failed: {type(error).__name__}: {error}')
                        return self.send_json({'error': 'telegram error'}, 502)
                    return self.send_json({'ok': True})
                platform = str(raw.get('platform', 'pc') or 'pc').lower()
                if platform not in ('pc', 'ps4', 'xbox', 'switch'):
                    platform = 'pc'
                items = raw.get('alerts')
                with TG_LOCK:
                    fired = tg_sync(platform, items if isinstance(items, list) else [])
                return self.send_json({'ok': True, 'count': len(TG_ALERTS), 'fired': fired})

            return self.send_json({'error': 'not found'}, 404)
        except Exception as error:
            print(f'Unhandled POST error: {type(error).__name__}: {error}')
            try:
                return self.send_json({'error': 'Внутренняя ошибка сервера'}, 500)
            except Exception:
                pass


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '4173'))
    print(f'MarketPulse listening on http://0.0.0.0:{port}')
    if TG_ENABLED:
        tg_load()
        threading.Thread(target=tg_alert_loop, daemon=True).start()
        print('Telegram price alerts: ON')
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()