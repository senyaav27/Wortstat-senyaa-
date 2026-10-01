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

        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS review_queue (title_id TEXT PRIMARY KEY, added_at TEXT NOT NULL, reviewed_at TEXT, notified_at TEXT)')
            if not self.meta('review_queue_migrated'):
                self.db.execute("INSERT OR IGNORE INTO review_queue(title_id,added_at,notified_at) SELECT title_id,at,at FROM events WHERE kind='new' AND at > (SELECT MIN(at) FROM events WHERE kind='new')")
                self.set_meta('review_queue_migrated','1')

    def new_rows(self):
        return self.rows('first_seen DESC', 'id IN (SELECT title_id FROM review_queue WHERE reviewed_at IS NULL)', 10000)

    def mark_reviewed(self, ident):
        with self.lock, self.db:
            self.db.execute('UPDATE review_queue SET reviewed_at=? WHERE title_id=?', (now(),ident))

    def pending_new(self):
        return self.rows('first_seen DESC', 'id IN (SELECT title_id FROM review_queue WHERE notified_at IS NULL)', 10000)

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
                    if previous:
                        self.db.execute('INSERT OR IGNORE INTO review_queue(title_id,added_at) VALUES(?,?)', (item['id'],timestamp))
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
        self.on_synced = None

    def refresh(self):
        if not self.lock.acquire(blocking=False):
            return {'busy': True}
        stats = {'new': [], 'measured': 0, 'dynamics': 0, 'alerts': [], 'errors': []}
        try:
            items = fetch_tvoe_items(self.transport)
            stats['new'] = self.store.sync(items)
            if self.on_synced:
                try:
                    self.on_synced()
                except RuntimeError:
                    stats['errors'].append('Telegram: уведомление о новинках ожидает повторной отправки')
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
             'new':'id IN (SELECT title_id FROM review_queue WHERE reviewed_at IS NULL)'}[kind]
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
:root{color-scheme:light;--paper:#f7f9fa;--white:#fff;--ink:#15232d;--muted:#52616a;--line:#dce3e7;--accent:#d64e36;--soft:#fff1ed}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.45;-webkit-font-smoothing:antialiased}
a{color:inherit}a:focus-visible,input:focus-visible,button:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
header{background:var(--white);border-bottom:1px solid var(--line)}.head{max-width:1120px;margin:auto;padding:18px 24px;display:flex;align-items:center;justify-content:space-between;gap:18px}
.brand{font-weight:850;font-size:1.32rem;letter-spacing:-.06em;text-decoration:none}.brand span{color:var(--accent)}.brand small{display:block;font-weight:550;letter-spacing:0;font-size:.7rem;color:var(--muted);margin-top:-3px}
.stamp{font-size:.82rem;color:var(--muted);text-align:right}.stamp strong{display:block;font-weight:650;color:var(--ink)}
main{max-width:1120px;margin:auto;padding:44px 24px 72px}.hero{display:flex;align-items:end;justify-content:space-between;gap:28px;padding-bottom:34px;border-bottom:1px solid var(--line)}
h1{font-size:clamp(2.4rem,5.7vw,5rem);line-height:1.02;letter-spacing:-.065em;margin:0 0 14px;font-weight:780}.hero p{margin:0;color:var(--muted);font-size:1rem}.hero-facts{text-align:right;white-space:nowrap}.hero-facts strong{font-size:1.12rem;display:block}.hero-facts span{font-size:.82rem;color:var(--muted)}
.signal-summary{display:flex;gap:18px;align-items:center;justify-content:space-between;margin:26px 0 4px;padding:20px 22px;background:var(--white);border:1px solid var(--line);border-radius:16px;text-decoration:none}
.signal-summary.has-signals{border-color:#efb7a8;background:var(--soft)}.signal-summary strong{font-size:1.02rem;display:block}.signal-summary span{font-size:.85rem;color:var(--muted)}.signal-summary b{color:var(--accent);font-size:.9rem;white-space:nowrap}
nav{display:flex;gap:28px;border-bottom:1px solid var(--line);margin-top:26px;overflow-x:auto}.tab{white-space:nowrap;text-decoration:none;padding:13px 2px 14px;color:var(--muted);font-size:.95rem;font-weight:650;border-bottom:3px solid transparent}.tab.active{color:var(--ink);border-color:var(--accent)}
.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:20px;padding-top:27px;margin-bottom:16px}.section-head h2{font-size:1.5rem;letter-spacing:-.035em;margin:0}.section-head p{margin:0;color:var(--muted);font-size:.85rem}
.controls{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap;margin:18px 0 10px}.modes{display:flex;gap:5px;background:#e9eef0;border-radius:10px;padding:4px;overflow-x:auto}
.mode{padding:8px 12px;border-radius:7px;text-decoration:none;white-space:nowrap;font-size:.86rem;font-weight:650;color:var(--muted)}.mode.active{background:var(--white);color:var(--ink);box-shadow:0 1px 3px #15232d18}
form{display:flex;gap:8px;min-width:260px}input{min-width:0;width:210px;border:1px solid #c4d0d5;border-radius:9px;background:var(--white);padding:10px 12px;font:inherit;font-size:.9rem}button{border:0;border-radius:9px;background:var(--ink);color:#fff;padding:10px 14px;font:inherit;font-size:.9rem;font-weight:700;cursor:pointer}
.list{background:var(--white);border:1px solid var(--line);border-radius:16px;overflow:hidden}.entry{display:grid;grid-template-columns:108px minmax(0,1fr) 164px;gap:20px;align-items:center;padding:18px 22px;border-bottom:1px solid var(--line)}.entry:last-child{border-bottom:0}
.when{font-weight:700;color:var(--muted);font-size:.85rem}.title{font-size:1.13rem;line-height:1.25;font-weight:740;letter-spacing:-.025em;margin:0 0 5px}.title a{text-decoration:none}.title a:hover{text-decoration:underline}
.detail{color:var(--muted);font-size:.82rem}.trend{display:inline-block;color:var(--accent);font-weight:650;margin-left:7px}.demand{text-align:right;font-size:1.11rem;font-weight:760;font-variant-numeric:tabular-nums}.demand small{display:block;font-weight:450;color:var(--muted);font-size:.72rem}
.empty{padding:52px 22px;color:var(--muted)}.empty strong{display:block;color:var(--ink);font-size:1.05rem;margin-bottom:5px}
.notice{margin-top:28px;color:var(--muted);font-size:.82rem;max-width:760px}.lock{max-width:540px;margin:12vh auto;padding:28px}.lock h1{font-size:2.8rem}.lock p{color:var(--muted)}.lock code{font:inherit;color:var(--accent)}
@media(max-width:700px){.head{padding:14px 18px}.stamp{font-size:.68rem}main{padding:31px 16px 55px}.hero{display:block;padding-bottom:25px}.hero-facts{text-align:left;margin-top:20px}.signal-summary{padding:16px;gap:10px}.signal-summary b{font-size:.78rem}nav{gap:22px}.section-head{display:block}.section-head p{margin-top:5px}.controls{display:block}.modes{width:100%;justify-content:space-between}.mode{flex:1;text-align:center;padding:8px 7px;font-size:.78rem}form{margin-top:12px;min-width:0}input{width:100%;flex:1}.entry{grid-template-columns:minmax(0,1fr) auto;gap:5px 10px;padding:15px 16px}.when{grid-column:1 / -1;color:var(--accent);font-size:.77rem}.demand{font-size:1rem}.demand small{font-size:.65rem}.title{font-size:1.06rem}.detail{font-size:.76rem}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


def dashboard_document(store, view, query, nonce, mode='date', csrf=''):
    csrf_field = '<input type="hidden" name="csrf" value="' + html.escape(csrf,quote=True) + '">'
    legacy = {'soon':('catalog','date'),'top':('catalog','top'),
              'growth':('catalog','growth'),'ads':('signals','date')}
    view, mode = legacy.get(view,(view,mode))
    view = view if view in ('catalog','signals','new') else 'catalog'
    mode = mode if mode in ('date','top','growth') else 'date'
    all_rows = store.rows('title ASC',limit=10000)
    new_rows = store.new_rows()
    signal_rows = [row for row in all_rows if row['total_count'] is not None
                   and row['total_count'] > 50000 and row['trend']=='растёт'
                   and not row['ambiguous']]
    if view == 'signals':
        rows = sorted(signal_rows,key=lambda r:r['delta'] or 0,reverse=True)
    elif view == 'new':
        rows = sorted(new_rows,key=lambda r:r['first_seen'],reverse=True)
    elif mode == 'top':
        rows = sorted((row for row in all_rows if row['total_count'] is not None
                       and not row['ambiguous']),key=lambda r:r['total_count'],reverse=True)
    elif mode == 'growth':
        rows = sorted((row for row in all_rows if row['trend']=='растёт'
                       and not row['ambiguous']),key=lambda r:r['delta'] or 0,reverse=True)
    else:
        rows = sorted(all_rows,key=lambda r:(poster_sort(r['poster_date']),r['title']))
    if query:
        rows = [row for row in rows if query.casefold() in row['title'].casefold()]
    escape = html.escape
    tabs = [('catalog','Каталог',len(all_rows)),('signals','Сигналы',len(signal_rows)),
            ('new','Новое',len(new_rows))]
    links = ''.join(f'<a class="tab{" active" if view==key else ""}" href="/?view={key}"'
                    f'{" aria-current=page" if view==key else ""}>{label} · {count}</a>'
                    for key,label,count in tabs)
    modes = [('date','По дате'),('top','По спросу'),('growth','Растут')]
    mode_links = ''.join(f'<a class="mode{" active" if mode==key else ""}" '
                         f'href="/?view=catalog&amp;mode={key}"'
                         f'{" aria-current=page" if mode==key else ""}>{label}</a>'
                         for key,label in modes) if view=='catalog' else ''
    entries = []
    seen = set()
    for row in rows:
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        date_label = escape(str(row['poster_date'] or 'Дата не указана'))
        title = escape(row['title'])
        kind = {'serials':'Сериал','films':'Фильм'}.get(row['type'],str(row['type'] or 'Тип не указан'))
        detail = escape(kind) + (' · запрос неоднозначен' if row['ambiguous'] else '')
        if row['trend']=='растёт' and row['delta'] is not None:
            detail += '<span class="trend">↑ ' + f'{row["delta"]:+,}'.replace(',',' ') + ' за 7 дней</span>'
        count = f'{row["total_count"]:,}'.replace(',',' ') if row['total_count'] is not None else '—'
        raw_url = row['url'] or ''
        safe_link = 'https://tvoe.live' + raw_url if raw_url.startswith('/') and not raw_url.startswith('//') else ''
        heading = f'<a href="{escape(safe_link,quote=True)}" rel="noopener noreferrer">{title}</a>' if safe_link else title
        review = (f'<form action="/review" method="post">{csrf_field}<input type="hidden" name="id" value="{escape(row["id"],quote=True)}"><button type="submit">Проверено для рекламы</button></form>' if view=='new' else '')
        entries.append(f'<article class="entry"><div class="when">{date_label}</div>'
                       f'<div><h3 class="title">{heading}</h3><div class="detail">{detail}</div></div>'
                       f'<div class="demand">{count}<small>запросов / 30 дней</small>{review}</div></article>')
    empty = {'catalog':('Тайтлы пока не загружены','Бот обновит коллекцию автоматически.'),
             'signals':('Рекламных сигналов пока нет','Здесь появятся однозначные запросы выше 50 000 с растущим спросом.'),
             'new':('Новых тайтлов пока нет','Добавления остаются здесь до отметки «Проверено для рекламы».')}
    if query and not entries:
        empty_text = ('Ничего не найдено','Попробуйте другое название или откройте каталог.')
    else:
        empty_text = empty[view]
    content = '<div class="list">' + (''.join(entries) if entries else
              f'<div class="empty"><strong>{empty_text[0]}</strong>{empty_text[1]}</div>') + '</div>'
    updated = store.meta('last_tvoe','')
    try:
        stamp = datetime.fromisoformat(updated.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Moscow'))
        updated_text = stamp.strftime('%d.%m.%Y в %H:%M МСК')
    except ValueError:
        updated_text = 'ожидается первое обновление'
    checked = sum(row['total_count'] is not None for row in all_rows)
    heading = {'catalog':'Каталог тайтлов','signals':'Сигналы для рекламы','new':'Новые в подписке'}[view]
    descriptions = {'catalog':'Один список с разными способами просмотра.',
                    'signals':'Спрос выше 50 000 и рост за последние 7 дней. Только однозначные запросы.',
                    'new':'Новые добавления в «Скоро в подписке». Остаются здесь до вашей проверки рекламы.'}
    controls = f'<div class="modes" aria-label="Сортировка каталога">{mode_links}</div>' if view=='catalog' else ''
    searched = escape(query,quote=True)
    form = (f'<form action="/" method="get"><input type="hidden" name="view" value="{view}">'
            + (f'<input type="hidden" name="mode" value="{mode}">' if view=='catalog' else '')
            + f'<input type="search" name="q" value="{searched}" placeholder="Поиск по названию" aria-label="Поиск по названию"><button type="submit">Найти</button></form>')
    summary = ('<strong>' + (f'{len(signal_rows)} сигналов для проверки рекламы' if signal_rows else 'Сигналов для рекламы пока нет') + '</strong>'
               '<span>Критерий: более 50 000 запросов и растущий спрос</span>')
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{heading} · TVOЁ</title><style nonce="{nonce}">{DASHBOARD_CSS}</style></head>
<body><header><div class="head"><a class="brand" href="/">TVO<span>Ё</span><small>Аналитика спроса</small></a><div class="stamp">Данные TVOЁ и Wordstat<strong>{escape(updated_text)}</strong></div></div></header>
<main><section class="hero"><div><h1>Скоро в подписке</h1><p>Контент TVOЁ и поисковый спрос в одном рабочем списке.</p></div><div class="hero-facts"><strong>{len(all_rows)} тайтлов · {checked} проверено</strong><span>Коллекция проверяется каждый час, Wordstat - раз в сутки</span><form action="/refresh" method="post">{csrf_field}<button type="submit">↻ Обновить</button></form></div></section>
<a class="signal-summary{" has-signals" if signal_rows else ""}" href="/?view=signals"><div>{summary}</div><b>Открыть →</b></a>
<nav aria-label="Разделы">{links}</nav><div class="section-head"><h2>{heading}</h2><p>{descriptions[view]}</p></div>
<div class="controls">{controls}{form}</div>{content}
<p class="notice">Wordstat показывает количество поисковых запросов по фразе, а не просмотры фильма. Общие названия помечены как неоднозначные. Сигнал помогает выбрать тайтл для проверки рекламной гипотезы, а не гарантирует результат кампании.</p></main></body></html>'''


def start_dashboard(store, secret, allowed_id, port, app=None):
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

        def csrf_token(self):
            jar = SimpleCookie()
            jar.load(self.headers.get('Cookie',''))
            session = jar['tvoe_session'].value
            return hmac.new(secret.encode(), b'tvoe-csrf-v1:' + session.encode(), hashlib.sha256).hexdigest()

        def do_POST(self):
            path = urlparse(self.path).path
            if path in ('/refresh','/review'):
                if not self.authorized():
                    return self.respond(403)
                try:
                    size = int(self.headers.get('Content-Length','0'))
                    if size < 0 or size > 4096:
                        return self.respond(413)
                    fields = parse_qs(self.rfile.read(size).decode('utf-8'))
                except (ValueError,UnicodeError):
                    return self.respond(400)
                if not hmac.compare_digest(fields.get('csrf',[''])[0], self.csrf_token()):
                    return self.respond(403)
                if path == '/review':
                    store.mark_reviewed(fields.get('id',[''])[0])
                    target = '/?view=new'
                else:
                    if app is None:
                        return self.respond(503)
                    app.refresh_async()
                    target = '/?refresh=started'
                self.send_response(303)
                self.send_header('Location',target)
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                return
            if path != '/auth':
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
                view = params.get('view',['catalog'])[0]
                mode = params.get('mode',['date'])[0]
                query = params.get('q',[''])[0][:100].strip()
                document = dashboard_document(store,view,query,nonce,mode,self.csrf_token())
                if params.get('refresh') == ['started']:
                    document = document.replace('<main>', '<main><p role="status">Обновление запрошено. Результат придёт в Telegram. После завершения перезагрузите страницу.</p>', 1)
                body = document.encode('utf-8')
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

    def send_new_alerts(self):
        rows = self.store.pending_new()
        for offset in range(0,len(rows),8):
            batch = rows[offset:offset+8]
            message = '🆕 Новое в «Скоро в подписке» TVOЁ:\n' + '\n'.join(
                f'{r["title"]} - {r["poster_date"] or "дата не указана"}' for r in batch)
            self.bot.send(message + '\nСписок сохранён в разделе «Новое» до вашей проверки рекламы.', MENU)
            with self.store.lock, self.store.db:
                self.store.db.executemany('UPDATE review_queue SET notified_at=? WHERE title_id=?', [(now(),r['id']) for r in batch])

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
    analyzer.on_synced = app.send_new_alerts
    start_dashboard(store,os.environ['TELEGRAM_BOT_TOKEN'],bot.allowed,int(os.environ.get('PORT','8080')),app)
    def schedule():
        next_analysis = 0
        while True:
            try:
                if time.monotonic() >= next_analysis:
                    result = analyzer.refresh()
                    if not result.get('busy'):
                        app.send_growth_alerts(result['alerts'])
                        next_analysis = time.monotonic() + 86400
                elif analyzer.lock.acquire(blocking=False):
                    try:
                        store.sync(fetch_tvoe_items(analyzer.transport))
                        app.send_new_alerts()
                    finally:
                        analyzer.lock.release()
            except Exception as exc:
                print('Scheduled refresh failed: '+type(exc).__name__, flush=True)
            time.sleep(3600)
    threading.Thread(target=schedule,daemon=True).start()
    app.loop()


if __name__ == '__main__':
    main()

