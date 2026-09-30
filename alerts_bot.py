"""Private TVOЁ + Yandex Wordstat analytics bot. Configure environment variables, then run this file."""
"""TVOЁ collection, Wordstat measurements, persistence, and trend calculations."""
import json
import base64
import binascii
import hashlib
import hmac
import html
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

TVOE_URL = 'https://api.tvoe.live/collections'
TVOE_COMING_SOON_URL = 'https://api.tvoe.live/coming-soon'
WORDSTAT_URL = 'https://searchapi.api.cloud.yandex.net/v2/wordstat/'
UTC = timezone.utc

TVOE_PUBLIC_HEADERS = {
    'Accept': 'application/json',
    'Origin': 'https://tvoe.live',
    'Referer': 'https://tvoe.live/',
    'User-Agent': 'Mozilla/5.0 (compatible; TVOE-Wordstat-Bot/1.0; +https://tvoe.live/)',
}


def now():
    return datetime.now(UTC).isoformat(timespec='seconds')


class APIError(Exception):
    pass


def request_json(url, payload=None, headers=None, timeout=20, attempts=3):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    request = Request(url, data=body, headers={'Accept': 'application/json', **({'Content-Type': 'application/json; charset=utf-8'} if body else {}), **(headers or {})})
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            code = exc.code
            if code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
                continue
            raise APIError(f'HTTP {code} from {url.split("/")[2]}') from None
        except (URLError, TimeoutError) as exc:
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
                continue
            raise APIError(f'Network error from {url.split("/")[2]}: {type(exc).__name__}') from None
        except (ValueError, UnicodeError):
            raise APIError(f'Invalid JSON from {url.split("/")[2]}') from None


def collection_items(response):
    if isinstance(response, dict):
        collections = response.get('collections', response.get('data', response.get('result', response)))
        if isinstance(collections, dict):
            collections = collections.get('collections', collections.get('items', []))
    else:
        collections = response
    if not isinstance(collections, list):
        raise APIError('TVOE collections shape unknown')
    for collection in collections:
        if isinstance(collection, dict) and (collection.get('type') == 'willPublishedSoon' or collection.get('name') == 'Скоро в подписке'):
            items = collection.get('items')
            if isinstance(items, dict):
                items = items.get('items', items.get('results'))
            if not isinstance(items, list):
                raise APIError('TVOE items shape unknown')
            return items
    raise APIError('TVOE willPublishedSoon collection missing')


def coming_soon_items(response):
    if isinstance(response, dict):
        response = response.get('data', response.get('items', response))
    if not isinstance(response, list):
        raise APIError('TVOE coming-soon shape unknown')
    return response


def tvoe_request(url):
    return request_json(url, headers=TVOE_PUBLIC_HEADERS)


def fetch_tvoe_items(transport=tvoe_request):
    try:
        return collection_items(transport(TVOE_URL))
    except APIError as exc:
        if 'HTTP 403' not in str(exc):
            raise
        try:
            return coming_soon_items(transport(TVOE_COMING_SOON_URL))
        except APIError as fallback_exc:
            raise APIError('TVOE /collections blocked; official /coming-soon fallback failed: ' + str(fallback_exc)) from None


def normalize_item(item):
    if not isinstance(item, dict):
        raise APIError('TVOE item is not an object')
    name = item.get('name')
    if not isinstance(name, str) or not name.strip():
        raise APIError('TVOE item missing name')
    kind = item.get('categoryAlias')
    ident = item.get('id') or item.get('_id') or item.get('contentId')
    url = item.get('url')
    if not ident:
        ident = 'name:' + name.strip().casefold() + ':' + str(kind or '')
    return {'id': str(ident), 'title': name.strip(), 'type': kind, 'genre': item.get('genreName'), 'poster_date': item.get('posterDate'), 'url': url, 'seasons_count': item.get('seasonsCount')}


def kind_suffix(kind):
    x = str(kind or '').lower()
    if 'serial' in x or 'series' in x or 'сериал' in x:
        return 'сериал'
    if 'mult' in x or 'animation' in x or 'мульт' in x:
        return 'мультфильм'
    return 'фильм'


def choose_query(item, top):
    title = item['title']
    phrase = title
    results = top.get('results') or []
    # Short or generic titles tend to include unrelated searches. Expose them as unverified.
    generic = len(title) <= 3 or (len(title.split()) == 1 and (title.casefold() in {'клиника', 'животные', 'док', 'дом', 'мир', 'любовь'} or len(title) < 11))
    if not generic:
        return phrase, 'Название достаточно конкретно; проверьте совпадение вручную при омонимии.', False
    suffix = kind_suffix(item.get('type'))
    candidate = f'{title} {suffix}'
    matched = any(suffix in str(row.get('phrase', '')).lower() and title.casefold() in str(row.get('phrase', '')).casefold() for row in results if isinstance(row, dict))
    return candidate, 'Уточнено из-за неоднозначности названия.' if matched else 'Неоднозначное название; соответствие произведению требует ручной проверки.', not matched


def trend(points):
    valid = sorted((p for p in points if isinstance(p, dict) and str(p.get('count', '')).isdigit()), key=lambda p: p.get('date', ''))
    if len(valid) < 8:
        return 'недостаточно данных', None
    n = min(7, len(valid) // 2)
    older = sum(int(x['count']) for x in valid[-2*n:-n])
    newer = sum(int(x['count']) for x in valid[-n:])
    delta = newer - older
    # Noise guard: avoid labeling small changes as directional.
    if older == 0 and newer == 0:
        direction = 'стабильно'
    elif older == 0:
        direction = 'растёт'
    elif abs(delta) / older < 0.15:
        direction = 'стабильно'
    else:
        direction = 'растёт' if delta > 0 else 'падает'
    return direction, delta


class Wordstat:
    def __init__(self, api_key, folder_id, transport=request_json):
        self.api_key, self.folder_id, self.transport = api_key, folder_id, transport

    def call(self, method, phrase, extra=None):
        payload = {'phrase': phrase, 'regions': ['225'], 'devices': ['DEVICE_ALL'], 'folderId': self.folder_id, **(extra or {})}
        data = self.transport(WORDSTAT_URL + method, payload, {'Authorization': 'Api-Key ' + self.api_key})
        if not isinstance(data, dict):
            raise APIError('Invalid Wordstat response')
        return data

    def top(self, phrase):
        data = self.call('topRequests', phrase, {'numPhrases': '10'})
        if not str(data.get('totalCount', '')).isdigit():
            raise APIError('Wordstat totalCount missing')
        return data

    def dynamics(self, phrase):
        end = datetime.now(UTC)
        start = end - timedelta(days=28)
        data = self.call('dynamics', phrase, {'period': 'PERIOD_DAILY', 'fromDate': start.isoformat().replace('+00:00', 'Z'), 'toDate': end.isoformat().replace('+00:00', 'Z')})
        if not isinstance(data.get('results'), list):
            raise APIError('Wordstat dynamics results missing')
        return data['results']


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS titles (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT, genre TEXT,
                    poster_date TEXT, url TEXT, seasons_count TEXT,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, status TEXT NOT NULL,
                    wordstat_query TEXT, query_note TEXT, ambiguous INTEGER DEFAULT 0,
                    total_count INTEGER, measured_at TEXT, trend TEXT, delta INTEGER,
                    dynamics_json TEXT
                );
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title_id TEXT NOT NULL,
                    measured_at TEXT NOT NULL, query TEXT NOT NULL, total_count INTEGER NOT NULL,
                    dynamics_json TEXT, trend TEXT, delta INTEGER,
                    FOREIGN KEY(title_id) REFERENCES titles(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title_id TEXT, at TEXT,
                    kind TEXT, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            ''')

    def meta(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return row['value'] if row else default

    def set_meta(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def sync(self, items):
        timestamp = now()
        previous = self.meta('last_tvoe', '')
        normalized = [normalize_item(item) for item in items]
        if len({item['id'] for item in normalized}) != len(normalized):
            raise APIError('Duplicate TVOE identifiers')
        new = []
        with self.lock, self.db:
            prior = {row['id']: dict(row) for row in self.db.execute('SELECT * FROM titles WHERE status="active"')}
            for item in normalized:
                old = self.db.execute('SELECT * FROM titles WHERE id=?', (item['id'],)).fetchone()
                if not old:
                    new.append(item['title'])
                    self.db.execute('INSERT INTO titles(id,title,type,genre,poster_date,url,seasons_count,first_seen,last_seen,status) VALUES(?,?,?,?,?,?,?,?,?,"active")', (item['id'],item['title'],item['type'],item['genre'],item['poster_date'],item['url'],str(item['seasons_count']) if item['seasons_count'] is not None else None,timestamp,timestamp))
                    self.db.execute('INSERT INTO events(title_id,at,kind,detail) VALUES(?,?,?,?)', (item['id'],timestamp,'new',item['title']))
                else:
                    for field in ('title','type','genre','poster_date','url'):
                        if old[field] != item[field]:
                            self.db.execute('INSERT INTO events(title_id,at,kind,detail) VALUES(?,?,?,?)', (item['id'],timestamp,'changed',field))
                    self.db.execute('UPDATE titles SET title=?,type=?,genre=?,poster_date=?,url=?,seasons_count=?,last_seen=?,status="active" WHERE id=?', (item['title'],item['type'],item['genre'],item['poster_date'],item['url'],str(item['seasons_count']) if item['seasons_count'] is not None else None,timestamp,item['id']))
                prior.pop(item['id'], None)
            for ident in prior:
                self.db.execute('UPDATE titles SET status="removed" WHERE id=?', (ident,))
                self.db.execute('INSERT INTO events(title_id,at,kind,detail) VALUES(?,?,?,?)', (ident,timestamp,'removed',prior[ident]['title']))
            self.db.execute('INSERT INTO metadata(key,value) VALUES("previous_tvoe",?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (previous,))
            self.db.execute('INSERT INTO metadata(key,value) VALUES("last_tvoe",?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (timestamp,))
        return new

    def measured(self, ident, query, note, ambiguous, count, points):
        stamp = now()
        with self.lock, self.db:
            previous = self.db.execute('SELECT dynamics_json,trend,delta FROM titles WHERE id=?',(ident,)).fetchone()
            direction, delta = trend(points) if points is not None else (previous['trend'],previous['delta'])
            serialized = json.dumps(points, ensure_ascii=False) if points is not None else previous['dynamics_json']
            self.db.execute('UPDATE titles SET wordstat_query=?,query_note=?,ambiguous=?,total_count=?,measured_at=?,trend=?,delta=?,dynamics_json=? WHERE id=?', (query,note,int(ambiguous),count,stamp,direction,delta,serialized,ident))
            self.db.execute('INSERT INTO measurements(title_id,measured_at,query,total_count,dynamics_json,trend,delta) VALUES(?,?,?,?,?,?,?)', (ident,stamp,query,count,serialized,direction,delta))

    def attach_dynamics(self, ident, points):
        direction, delta = trend(points)
        serialized = json.dumps(points, ensure_ascii=False)
        with self.lock, self.db:
            self.db.execute('UPDATE titles SET dynamics_json=?,trend=?,delta=? WHERE id=?', (serialized,direction,delta,ident))
            self.db.execute('UPDATE measurements SET dynamics_json=?,trend=?,delta=? WHERE id=(SELECT MAX(id) FROM measurements WHERE title_id=?)', (serialized,direction,delta,ident))

    def rows(self, order='total_count DESC', where='status="active"', limit=100, offset=0, params=()):
        allowed = {'total_count DESC', 'poster_date ASC', 'delta DESC', 'first_seen DESC', 'title ASC'}
        if order not in allowed:
            raise ValueError('Unsupported sort')
        with self.lock:
            return [dict(row) for row in self.db.execute(f'SELECT * FROM titles WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?', (*params,limit,offset))]

    def find(self, term):
        return self.rows('title ASC', 'status="active" AND title LIKE ?', 10, 0, ('%'+term+'%',))

    def history(self, ident, limit=6):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM measurements WHERE title_id=? ORDER BY id DESC LIMIT ?', (ident,limit))]


class Analyzer:
    def __init__(self, store, wordstat, transport=tvoe_request, top_budget=100, dynamics_budget=12):
        self.store, self.wordstat, self.transport = store, wordstat, transport
        self.top_budget, self.dynamics_budget = top_budget, dynamics_budget
        self.lock = threading.Lock()

    def refresh(self):
        if not self.lock.acquire(blocking=False):
            return {'busy': True}
        stats = {'new': [], 'measured': 0, 'dynamics': 0, 'alerts': [], 'errors': []}
        try:
            items = fetch_tvoe_items(self.transport)
            stats['new'] = self.store.sync(items)
            rows = self.store.rows('first_seen DESC', limit=10000)
            # Refresh never measured entries first, then stale measurements.
            rows.sort(key=lambda r: r['measured_at'] or '')
            for row in rows[:self.top_budget]:
                try:
                    initial = self.wordstat.top(row['title'])
                    query, note, ambiguous = choose_query(row, initial)
                    top = self.wordstat.top(query) if query != row['title'] else initial
                    count = int(top['totalCount'])
                    self.store.measured(row['id'],query,note,ambiguous,count,None)
                    if count <= 50000:
                        self.store.set_meta('growth_alert:'+row['id'],'quiet')
                    stats['measured'] += 1
                except APIError as exc:
                    stats['errors'].append(str(exc))
                    if 'HTTP 429' in str(exc):
                        break
            candidates = [row for row in self.store.rows('title ASC',limit=10000)
                          if row['total_count'] is not None and row['total_count'] > 50000
                          and not row['ambiguous'] and row['wordstat_query']]
            # Rotate through eligible titles across days, including ones not measured in this pass.
            candidates.sort(key=lambda row: (self.store.meta('dynamics_checked:'+row['id'],''),
                                             -(row['total_count'] or 0)))
            for row in candidates[:self.dynamics_budget]:
                try:
                    points = self.wordstat.dynamics(row['wordstat_query'])
                    self.store.attach_dynamics(row['id'],points)
                    self.store.set_meta('dynamics_checked:'+row['id'],now())
                    stats['dynamics'] += 1
                    direction, delta = trend(points)
                    if direction == 'растёт' and delta is not None and delta > 0:
                        if self.store.meta('growth_alert:'+row['id']) != 'sent':
                            stats['alerts'].append({
                                'id':row['id'], 'title':row['title'],
                                'count':row['total_count'], 'delta':delta,
                                'query':row['wordstat_query']
                            })
                    else:
                        self.store.set_meta('growth_alert:'+row['id'],'quiet')
                except APIError as exc:
                    stats['errors'].append(str(exc))
                    if 'HTTP 429' in str(exc):
                        break
            checked = sum(row['total_count'] is not None for row in self.store.rows('title ASC',limit=10000))
            if checked == len(rows) and not stats['errors']:
                self.store.set_meta('last_full',now())
            self.store.set_meta('last_errors', ', '.join(dict.fromkeys(stats['errors']))[:500])
            return stats
        except APIError as exc:
            stats['errors'].append(str(exc))
            self.store.set_meta('last_errors', str(exc))
            return stats
        finally:
            self.lock.release()


"""Private Telegram long-polling interface. Token only comes from the environment."""
import json
import os
import threading
import time
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MENU = {'inline_keyboard': [
    [{'text':'🔥 Популярное','callback_data':'top:0'}, {'text':'📈 Рост','callback_data':'growth:0'}],
    [{'text':'🆕 Новое','callback_data':'new:0'}, {'text':'📅 Скоро','callback_data':'soon:0'}],
    [{'text':'🔄 Обновить','callback_data':'refresh'}, {'text':'🔎 Поиск','callback_data':'find'}],
    [{'text':'ℹ️ Статус','callback_data':'status'}]
]}


def authorized(update, allowed):
    source = update.get('callback_query') or update.get('message') or {}
    sender = source.get('from') or {}
    chat = (source.get('message') or source).get('chat') or {}
    return sender.get('id') == allowed and chat.get('id') == allowed and chat.get('type') == 'private'


def date_text(value):
    if not value:
        return 'не указана'
    try:
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).strftime('%d.%m.%Y')
    except ValueError:
        return str(value)[:30]


MONTHS = {'января':1,'февраля':2,'марта':3,'апреля':4,'мая':5,'июня':6,'июля':7,'августа':8,'сентября':9,'октября':10,'ноября':11,'декабря':12}


def poster_sort(value, today=None):
    today = today or datetime.now(ZoneInfo('Europe/Moscow')).date()
    if not value:
        return date.max
    raw = str(value).lower().strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    match = re.fullmatch(r'(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?', raw)
    if not match or match.group(2) not in MONTHS:
        return date.max
    try:
        target = date(int(match.group(3) or today.year), MONTHS[match.group(2)], int(match.group(1)))
        if not match.group(3) and target < today - timedelta(days=7):
            target = date(today.year+1,target.month,target.day)
        return target
    except ValueError:
        return date.max


def item_text(row, rank=None):
    number = f'{rank}. ' if rank else '🎬 '
    count = f'{row["total_count"]:,}'.replace(',', ' ') if row['total_count'] is not None else 'нет данных'
    caveat = ' ⚠️ Неоднозначный запрос' if row.get('ambiguous') else ''
    delta = f' ({row["delta"]:+d} за 7 дней)' if row.get('delta') is not None else ''
    return f'{number}{row["title"]}\nТип: {row["type"] or "не указан"} | В TVOЁ: {date_text(row["poster_date"])}\nWordstat: {count} / 30 дней{caveat}\nЗапрос: {row["wordstat_query"] or "не измерен"}\nТренд: {row["trend"] or "нет данных"}{delta}'


def render_list(store, kind, page):
    page = max(0, min(page, 100))
    order = {'top':'total_count DESC','growth':'delta DESC','soon':'poster_date ASC','new':'first_seen DESC'}[kind]
    where = {'top':'status="active" AND total_count IS NOT NULL AND ambiguous=0',
             'growth':'status="active" AND trend="растёт" AND ambiguous=0',
             'soon':'status="active" AND poster_date IS NOT NULL',
             'new':'status="active" AND first_seen > COALESCE((SELECT value FROM metadata WHERE key="previous_tvoe"), "9999")'}[kind]
    if kind == 'soon':
        rows = sorted(store.rows('title ASC', where, 10000), key=lambda r: poster_sort(r['poster_date']))[page*5:(page+1)*5]
    else:
        rows = store.rows(order, where, 5, page*5)
    labels = {'top':'🔥 Популярное','growth':'📈 Растущий спрос','soon':'📅 Скоро в подписке','new':'🆕 Новые тайтлы'}
    text = labels[kind] + f' - страница {page+1}\n\n' + ('\n\n'.join(item_text(row, page*5+i+1) for i,row in enumerate(rows)) if rows else 'Пока нет данных.')
    buttons = []
    if page:
        buttons.append({'text':'⬅️','callback_data':f'{kind}:{page-1}'})
    if len(rows) == 5:
        buttons.append({'text':'➡️','callback_data':f'{kind}:{page+1}'})
    return text[:4000], {'inline_keyboard':[buttons, [{'text':'🏠 Меню','callback_data':'menu'}]] if buttons else [[{'text':'🏠 Меню','callback_data':'menu'}]]}


class Telegram:
    def __init__(self, token, allowed):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.allowed = int(allowed)

    def call(self, method, payload):
        url = self.base + method
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = Request(url, data=data, headers={'Content-Type':'application/json; charset=utf-8'})
        try:
            with urlopen(request, timeout=45 if method == 'getUpdates' else 15) as response:
                result = json.load(response)
            if not result.get('ok'):
                raise RuntimeError('Telegram API rejected request')
            return result.get('result')
        except (HTTPError, URLError, TimeoutError, ValueError):
            raise RuntimeError('Telegram API unavailable') from None

    def send(self, text, keyboard=None, chat_id=None):
        payload = {'chat_id':self.allowed if chat_id is None else chat_id, 'text':text[:4000], 'link_preview_options':{'is_disabled':True}}
        if keyboard:
            payload['reply_markup'] = keyboard
        return self.call('sendMessage', payload)

    def answer(self, callback_id):
        self.call('answerCallbackQuery', {'callback_query_id':callback_id})


def signed_access(secret, allowed_id, lifetime):
    expires = int(time.time()) + lifetime
    message = f'{allowed_id}:{expires}'.encode()
    payload = base64.urlsafe_b64encode(message).decode().rstrip('=')
    signature = hmac.new(secret.encode(), b'tvoe-dashboard-v1:' + payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + signature


def valid_access(token, secret, allowed_id):
    try:
        payload, signature = token.split('.', 1)
        expected = hmac.new(secret.encode(), b'tvoe-dashboard-v1:' + payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        ident, expires = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode().split(':', 1)
        return ident == str(allowed_id) and int(time.time()) < int(expires)
    except (ValueError, UnicodeError, binascii.Error):
        return False


DASHBOARD_CSS = """
:root{color-scheme:light;--paper:#f5f4f0;--ink:#172536;--muted:#536273;--line:#d9dee2;--blue:#225b84;--coral:#ca5c48}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
a{color:inherit}a:focus-visible,input:focus-visible{outline:3px solid var(--coral);outline-offset:3px}
header{background:#172536;color:#fff;padding:24px max(24px,calc((100vw - 1100px)/2));display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{font-weight:800;font-size:1.22rem;letter-spacing:-.04em;text-decoration:none}.brand span{color:#f29c7e}.stamp{font-size:.85rem;color:#cbd9e1}
main{max-width:1100px;margin:auto;padding:42px 24px 90px}.intro{display:grid;grid-template-columns:1.3fr .7fr;gap:32px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:36px}
h1{font-size:clamp(2.4rem,6vw,5.3rem);letter-spacing:-.065em;line-height:1.02;margin:0;font-weight:760;max-width:760px}
.intro p{color:var(--muted);font-size:1.05rem;margin:0 0 5px}.count{font-size:3.4rem;line-height:1;font-weight:800;letter-spacing:-.06em;color:var(--blue)}
.statlabel{font-size:.88rem;color:var(--muted)}.metrics{display:flex;gap:36px;padding:23px 0 8px;flex-wrap:wrap}.metric strong{font-size:1.22rem;display:block}.metric span{font-size:.83rem;color:var(--muted)}
nav{display:flex;gap:8px;overflow:auto;padding:32px 0 22px}.tab{white-space:nowrap;text-decoration:none;border:1px solid var(--line);padding:10px 18px;border-radius:100px;font-weight:650;font-size:.92rem}.tab.active{background:var(--ink);color:#fff;border-color:var(--ink)}
.toolbar{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:16px}.toolbar h2{margin:0;font-size:1.4rem;letter-spacing:-.035em}form{display:flex;gap:8px}input{min-width:0;width:210px;border:1px solid #bcc9d1;border-radius:8px;background:#fff;padding:10px 12px;font:inherit}button{border:0;border-radius:8px;background:var(--blue);color:#fff;padding:10px 14px;font:inherit;font-weight:700;cursor:pointer}
.entry{display:grid;grid-template-columns:110px minmax(0,1fr) 155px;gap:22px;align-items:center;border-top:1px solid var(--line);padding:19px 0}.entry:last-of-type{border-bottom:1px solid var(--line)}
.when{font-weight:750;color:var(--coral);font-size:.97rem}.title{font-size:1.24rem;line-height:1.2;font-weight:720;letter-spacing:-.03em;margin:0 0 5px}.detail{color:var(--muted);font-size:.84rem}.demand{text-align:right;font-size:1.05rem;font-weight:720}.demand small{display:block;font-weight:400;color:var(--muted);font-size:.75rem}
.empty{padding:46px 0;color:var(--muted)}.notice{margin-top:32px;color:var(--muted);font-size:.82rem;max-width:700px}.lock{max-width:530px;margin:12vh auto;padding:28px}.lock h1{font-size:2.8rem}.lock p{color:var(--muted)}.lock code{font:inherit;color:var(--blue)}
@media(max-width:700px){header{padding:18px 20px}main{padding:30px 20px 70px}.intro{display:block}.intro p{margin-top:22px}.metrics{gap:18px 28px}.entry{grid-template-columns:78px 1fr;gap:10px}.demand{grid-column:2;text-align:left;margin-top:-6px}.toolbar{display:block}.toolbar form{margin-top:14px}input{width:100%;flex:1}.stamp{font-size:.7rem}}
"""


def dashboard_document(store, view, query, nonce):
    labels = {'soon':'Скоро в подписке','ads':'Сигналы для рекламы','top':'Популярное','growth':'Растущий спрос','new':'Новое'}
    view = view if view in labels else 'soon'
    all_rows = store.rows('title ASC', limit=10000)
    active = len(all_rows)
    checked = sum(row['total_count'] is not None for row in all_rows)
    if query:
        rows = [row for row in all_rows if query.casefold() in row['title'].casefold()]
    elif view == 'ads':
        rows = sorted((row for row in all_rows if row['total_count'] is not None
                       and row['total_count'] > 50000 and row['trend']=='растёт'
                       and not row['ambiguous']),key=lambda r:r['delta'] or 0,reverse=True)
    elif view == 'top':
        rows = sorted((row for row in all_rows if row['total_count'] is not None and not row['ambiguous']),key=lambda r:r['total_count'],reverse=True)
    elif view == 'growth':
        rows = sorted((row for row in all_rows if row['trend']=='растёт' and not row['ambiguous']),key=lambda r:r['delta'] or 0,reverse=True)
    elif view == 'new':
        previous = store.meta('previous_tvoe','9999')
        rows = sorted((row for row in all_rows if row['first_seen'] > previous),key=lambda r:r['first_seen'],reverse=True)
    else:
        rows = sorted(all_rows,key=lambda r:(poster_sort(r['poster_date']),r['title']))
    escape = html.escape
    links = ''.join(f'<a class="tab{" active" if view==key else ""}" href="/?view={key}">{label}</a>' for key,label in labels.items())
    entries = []
    for row in rows[:100]:
        date_label = escape(str(row['poster_date'] or 'Дата не указана'))
        title = escape(row['title'])
        kind = escape(str(row['type'] or 'Тип не указан'))
        note = ' · Запрос неоднозначен' if row['ambiguous'] else ''
        count = f'{row["total_count"]:,}'.replace(',',' ') if row['total_count'] is not None else '—'
        demand = f'<div class="demand">{count}<small>запросов за 30 дней</small></div>'
        entries.append(f'<article class="entry"><div class="when">{date_label}</div><div><h3 class="title">{title}</h3><div class="detail">{kind}{note}</div></div>{demand}</article>')
    content = ''.join(entries) if entries else '<p class="empty">Пока нет данных для этого раздела.</p>'
    updated = store.meta('last_tvoe','ещё не обновлялась')
    updated_text = escape(updated.replace('T',' ')[:19] + ' UTC' if 'T' in updated else updated)
    searched = escape(query,quote=True)
    title = 'Поиск' if query else labels[view]
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{escape(title)} · TVOЁ</title><style nonce="{nonce}">{DASHBOARD_CSS}</style></head>
<body><header><a class="brand" href="/">TVO<span>Ё</span> / спрос на контент</a><div class="stamp">Обновлено: {updated_text}</div></header>
<main><section class="intro"><h1>Что скоро появится в подписке</h1><div><div class="count">{active}</div><div class="statlabel">тайтла в текущей коллекции</div><p>Поисковый спрос и даты из TVOЁ в одном месте.</p></div></section>
<div class="metrics"><div class="metric"><strong>{checked} из {active}</strong><span>проверено в Wordstat</span></div><div class="metric"><strong>24 часа</strong><span>между автоматическими проверками</span></div></div>
<nav aria-label="Разделы">{links}</nav><div class="toolbar"><h2>{escape(title)}</h2><form action="/" method="get"><input type="search" name="q" value="{searched}" placeholder="Название тайтла" aria-label="Название тайтла"><button type="submit">Найти</button></form></div>{content}
<p class="notice">Wordstat показывает спрос на поисковую фразу, а не просмотры тайтла. Общие названия могут совпадать с другими темами. Список обновляется ботом автоматически.</p></main></body></html>'''


def start_dashboard(store, secret, allowed_id, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Avoid logging signed access links or cookie values.

        def respond(self, status, body=b'', content_type='text/html; charset=utf-8', cookie=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            if cookie:
                self.send_header('Set-Cookie',cookie)
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get('Cookie',''))
            except Exception:
                return False
            return 'tvoe_session' in jar and valid_access(jar['tvoe_session'].value,secret,allowed_id)

        def do_POST(self):
            if urlparse(self.path).path != '/auth':
                return self.respond(404)
            if int(self.headers.get('Content-Length','0')) > 4096:
                return self.respond(413)
            try:
                data = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                token = data.get('token','')
            except (ValueError, UnicodeError):
                return self.respond(400)
            if not isinstance(token,str) or not valid_access(token,secret,allowed_id):
                return self.respond(403)
            cookie = 'tvoe_session=' + signed_access(secret,allowed_id,30*86400) + '; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax'
            return self.respond(204,b'',cookie=cookie)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == '/health':
                return self.respond(200,b'ok','text/plain; charset=utf-8')
            if parsed.path != '/':
                return self.respond(404)
            nonce = secrets.token_urlsafe(16)
            self.send_response(200)
            if self.authorized():
                params = parse_qs(parsed.query)
                view = params.get('view',['soon'])[0]
                query = params.get('q',[''])[0][:100].strip()
                body = dashboard_document(store,view,query,nonce).encode('utf-8')
            else:
                body = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Вход · TVOЁ</title><style nonce="{nonce}">{DASHBOARD_CSS}</style></head><body><main class="lock"><h1>Личный обзор TVOЁ</h1><p>Откройте бота в Telegram и отправьте <code>/web</code>. Он пришлёт временную ссылку для входа.</p></main><script nonce="{nonce}">if(location.hash.startsWith('#login=')){{const token=location.hash.slice(7);history.replaceState(null,'','/');fetch('/auth',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token}})}}).then(r=>{{if(r.ok)location.reload();}});}}</script></body></html>'''.encode('utf-8')
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; form-action 'self'; base-uri 'none'")
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('0.0.0.0',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server


class BotApp:
    def __init__(self, bot, store, analyzer):
        self.bot, self.store, self.analyzer = bot, store, analyzer

    def send_growth_alerts(self, alerts):
        for row in alerts:
            count = f'{row["count"]:,}'.replace(',',' ')
            delta = f'{row["delta"]:+,}'.replace(',',' ')
            message = (f'📈 Сигнал для проверки рекламы: {row["title"]}\n'
                       f'Wordstat: {count} запросов за 30 дней.\n'
                       f'Динамика: {delta} запросов за последние 7 дней к предыдущим 7.\n'
                       f'Запрос: {row["query"]}\n'
                       'Проверьте соответствие запроса тайтлу и экономику кампании перед запуском.')
            self.bot.send(message,MENU)
            self.store.set_meta('growth_alert:'+row['id'],'sent')

    def refresh_async(self, notify=True):
        if self.analyzer.lock.locked():
            self.bot.send('Обновление уже выполняется.')
            return
        self.bot.send('🔄 Обновление запущено.')
        def run():
            result = self.analyzer.refresh()
            if result.get('busy'):
                self.bot.send('Обновление уже выполняется.')
                return
            if notify:
                message = f'Обновление завершено. Новых: {len(result["new"])}; Wordstat: {result["measured"]}; динамика: {result["dynamics"]}.'
                if result['errors']:
                    message += '\nОшибки: ' + '; '.join(dict.fromkeys(result['errors']))[:500]
                self.bot.send(message, MENU)
                self.send_growth_alerts(result['alerts'])
        threading.Thread(target=run, daemon=True).start()

    def handle(self, update):
        if not authorized(update, self.bot.allowed):
            callback = update.get('callback_query')
            if callback:
                try: self.bot.answer(callback['id'])
                except RuntimeError: pass
            source = (callback or update.get('message') or {})
            chat = (source.get('message') or source).get('chat') or {}
            if chat.get('type') == 'private' and isinstance(chat.get('id'), int):
                try: self.bot.send('Нет доступа.', chat_id=chat['id'])
                except RuntimeError: pass
            return
        callback = update.get('callback_query')
        if callback:
            try: self.bot.answer(callback['id'])
            except RuntimeError: pass
        raw = (callback or update.get('message') or {}).get('data') if callback else (update.get('message') or {}).get('text','')
        raw = raw or ''
        command = raw.split()[0].split('@')[0].lstrip('/').split(':', 1)[0] if raw else ''
        if raw in ('menu','start') or command == 'start':
            self.bot.send('🎬 TVOЁ - спрос на контент «Скоро в подписке». Выберите раздел.', MENU)
        elif command in ('top','growth','soon','new'):
            try: page = int(raw.split(':',1)[1]) if ':' in raw else 0
            except ValueError: page = 0
            message, keyboard = render_list(self.store, command, page)
            self.bot.send(message, keyboard)
        elif command == 'refresh':
            self.refresh_async()
        elif command == 'status':
            count = len(self.store.rows(limit=10000))
            checked = len(self.store.rows(where='status="active" AND total_count IS NOT NULL',limit=10000))
            self.bot.send(f'Коллекция: {self.store.meta("last_tvoe","ещё не обновлялась")}\nТайтлов: {count}; проверено Wordstat: {checked}\nПолный анализ: {self.store.meta("last_full") or "ещё нет"}\nОшибки: {self.store.meta("last_errors","нет") or "нет"}', MENU)
        elif command == 'web':
            domain = os.environ.get('DASHBOARD_BASE_URL') or ('https://' + os.environ.get('RAILWAY_PUBLIC_DOMAIN','').strip())
            if not domain.startswith('https://') or domain == 'https://':
                self.bot.send('Веб-страница ещё не подключена.')
            else:
                token = signed_access(os.environ['TELEGRAM_BOT_TOKEN'],self.bot.allowed,600)
                self.bot.send('Ваш личный обзор TVOЁ. Ссылка действует 10 минут:\n' + domain.rstrip('/') + '/#login=' + token)
        elif command == 'find':
            self.bot.send('Введите /title и название, например: /title Шрек 5')
        elif command == 'title':
            term = raw.partition(' ')[2].strip()
            if not term:
                self.bot.send('Пример: /title Шрек 5')
                return
            rows = self.store.find(term)
            if not rows:
                self.bot.send('Тайтл не найден в текущей коллекции.')
                return
            for row in rows[:3]:
                history = self.store.history(row['id'])
                measurements = '\n'.join(f'{date_text(h["measured_at"])}: {h["total_count"]:,}'.replace(',', ' ') for h in history)
                points = json.loads(row['dynamics_json']) if row.get('dynamics_json') else []
                recent = ', '.join(f'{date_text(p.get("date"))}: {p.get("count")}' for p in points[-5:])
                link = ('https://tvoe.live' + row['url']) if row.get('url','').startswith('/') else (row.get('url') or '')
                self.bot.send(item_text(row)+'\nTVOЁ: '+(link or 'ссылка не указана')+'\nИстория:\n'+(measurements or 'нет')+'\nДинамика: '+(recent or 'нет')+'\n'+(row['query_note'] or ''))
        else:
            self.bot.send('Выберите команду в меню.', MENU)

    def loop(self):
        offset = None
        while True:
            try:
                updates = self.bot.call('getUpdates', {'offset':offset,'timeout':30,'allowed_updates':['message','callback_query']})
                for update in updates:
                    offset = update['update_id'] + 1
                    self.handle(update)
            except (RuntimeError, KeyError) as exc:
                print('Bot polling error: '+type(exc).__name__, flush=True)
                time.sleep(5)


def main():
    required = ['YANDEX_API_KEY','YANDEX_FOLDER_ID','TELEGRAM_BOT_TOKEN','TELEGRAM_ALLOWED_USER_ID']
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit('Missing environment variables: '+', '.join(missing))
    store = Store(os.environ.get('DATABASE_PATH','data/tvoe.sqlite3'))
    bot = Telegram(os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_ALLOWED_USER_ID'])
    analyzer = Analyzer(store, Wordstat(os.environ['YANDEX_API_KEY'], os.environ['YANDEX_FOLDER_ID']), top_budget=int(os.environ.get('TOP_BUDGET','100')), dynamics_budget=int(os.environ.get('DYNAMICS_BUDGET','12')))
    app = BotApp(bot,store,analyzer)
    start_dashboard(store,os.environ['TELEGRAM_BOT_TOKEN'],bot.allowed,int(os.environ.get('PORT','8080')))
    def schedule():
        while True:
            try:
                result = analyzer.refresh()
                if result['new'] and store.meta('notified_once'):
                    bot.send('🆕 Новое в TVOЁ: '+', '.join(result['new'][:10]), MENU)
                app.send_growth_alerts(result['alerts'])
                store.set_meta('notified_once','1')
            except Exception as exc:
                print('Scheduled refresh failed: '+type(exc).__name__, flush=True)
            time.sleep(86400)
    threading.Thread(target=schedule,daemon=True).start()
    app.loop()


if __name__ == '__main__':
    main()
