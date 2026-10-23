"""MarketPulse backend: frontend + cached warframe.market proxy (API v2)."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
from urllib.error import HTTPError
from collections import OrderedDict
import os
import json
import math
import hashlib
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


def fetch_json(url, ttl=60, platform='pc', language='en', method='GET', body=None):
    key = f'{platform}:{language}:{method}:{url}'
    now = time.time()
    with LOCK:
        hit = CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        err_hit = ERROR_CACHE.get(key)
        if err_hit and now - err_hit[0] < ERROR_CACHE_TTL:
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
    if isinstance(last, HTTPError) and last.code == 429:
        with LOCK:
            ERROR_CACHE[key] = (time.time(), last)
            ERROR_CACHE.move_to_end(key)
            while len(ERROR_CACHE) > CACHE_MAX_SIZE:
                ERROR_CACHE.popitem(last=False)
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
    api_key = os.environ.get('AQ.Ab8RN6IrphuAeNw70oqpN0FFqEC0ZTu93XFo8-7_1Pm2WwUxwg')
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
    if GEMINI_MODEL.startswith('gemini-3'):
        generation_config['thinkingConfig'] = {'thinkingLevel': GEMINI_THINKING_LEVEL}
    body = json.dumps({
        'contents': [{'role': 'user', 'parts': [{'text': user_content}]}],
        'systemInstruction': {'parts': [{'text': AI_SYSTEM_PROMPT}]},
        'generationConfig': generation_config,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent',
        data=body,
        headers={
            'x-goog-api-key': api_key,
            'content-type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode('utf-8'))
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


def fetch_live_price(slug, platform):
    raw = fetch_orders(slug, platform)
    orders = [normalize_order(o) for o in extract_orders(raw)]
    if not orders:
        return None
    return stats(orders)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

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
                    price_data = fetch_live_price(slug, plat)
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
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()