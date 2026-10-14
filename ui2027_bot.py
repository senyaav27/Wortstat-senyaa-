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
            if code in (500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
                continue
            raise APIError(f'HTTP {code} from {url.split("/")[2]}') from None
        except (URLError, TimeoutError, OSError) as exc:
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
    return {'id': str(ident), 'title': name.strip(), 'type': kind, 'genre': item.get('genreName'), 'poster_date': item.get('posterDate'), 'url': url, 'seasons_count': item.get('seasonsCount'), 'year':str(item.get('year') or '') if str(item.get('year') or '').isdigit() and 1880<=int(item['year'])<=datetime.now(UTC).year+10 else ''}


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


def weekly_comparison(points):
    today = datetime.now(UTC).date()
    values = {}
    for point in points or []:
        try:
            day = date.fromisoformat(str(point['date'])[:10])
            count = int(point['count'])
            if count >= 0 and day < today and day not in values:
                values[day] = count
        except (ValueError, TypeError, KeyError):
            continue
    if not values:
        return None
    end = max(values)
    days = [end - timedelta(days=i) for i in range(13,-1,-1)]
    if any(day not in values for day in days):
        return None
    older = sum(values[d] for d in days[:7])
    newer = sum(values[d] for d in days[7:])
    return {'older':older,'newer':newer,'delta':newer-older,
            'percent':(newer-older)*100/older if older else None,
            'from':days[0].isoformat(),'split':days[7].isoformat(),'to':end.isoformat()}


def trend(points):
    comparison = weekly_comparison(points)
    if comparison is None:
        return 'недостаточно данных', None
    older, newer, delta = comparison['older'],comparison['newer'],comparison['delta']
    direction = ('стабильно' if older == newer or (older and abs(delta)/older < .15)
                 else 'растёт' if delta > 0 else 'падает')
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
        self.radar_lock = threading.Lock()
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
            self.db.execute('CREATE TABLE IF NOT EXISTS ad_status (title_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS ad_tests (id INTEGER PRIMARY KEY AUTOINCREMENT, title_id TEXT NOT NULL, at TEXT NOT NULL, start_date TEXT, end_date TEXT, spend REAL, registrations INTEGER, trials INTEGER, payments INTEGER, note TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS review_queue (title_id TEXT PRIMARY KEY, added_at TEXT NOT NULL, reviewed_at TEXT, notified_at TEXT)')
            if not self.meta('review_queue_migrated'):
                self.db.execute("INSERT OR IGNORE INTO review_queue(title_id,added_at,notified_at) SELECT title_id,at,at FROM events WHERE kind='new' AND at > (SELECT MIN(at) FROM events WHERE kind='new')")
                self.set_meta('review_queue_migrated','1')

            self.db.execute('CREATE TABLE IF NOT EXISTS watchlist (title_id TEXT PRIMARY KEY, name_key TEXT UNIQUE NOT NULL, added_at TEXT NOT NULL)')

            film_schema(self)
            self.origin_lock=threading.Lock()
            if 'family' not in {r[1] for r in self.db.execute('PRAGMA table_info(title_origin)')}:self.db.execute('ALTER TABLE title_origin ADD COLUMN family TEXT')

            # Recalculate current classification from saved points, preserving history.
            for row in self.db.execute('SELECT id,dynamics_json FROM titles WHERE dynamics_json IS NOT NULL').fetchall():
                try: direction,delta = trend(json.loads(row['dynamics_json']))
                except (ValueError,TypeError): continue
                self.db.execute('UPDATE titles SET trend=?,delta=? WHERE id=?',(direction,delta,row['id']))

    def save_ad(self, ident, fields):
        status = fields.get('status','')
        if status not in ('Рассмотреть','Запущено','Отложено'):
            raise ValueError('Выберите статус')
        if not self.db.execute('SELECT 1 FROM titles WHERE id=?',(ident,)).fetchone():
            raise ValueError('Тайтл не найден')
        numbers = []
        for key in ('spend','registrations','trials','payments'):
            raw = fields.get(key,'').strip().replace(',','.')
            value = (float(raw) if key=='spend' else int(raw)) if raw else None
            if value is not None and (value < 0 or value > 1000000000 or not __import__('math').isfinite(value)):
                raise ValueError('Показатели должны быть конечными неотрицательными числами')
            numbers.append(value)
        dates = [fields.get(k,'').strip() for k in ('start_date','end_date')]
        for d in dates:
            if d: date.fromisoformat(d)
        if all(dates) and dates[0] > dates[1]:
            raise ValueError('Начало периода позже окончания')
        note = fields.get('note','').strip()[:1000]
        with self.lock, self.db:
            self.db.execute('INSERT INTO ad_status VALUES(?,?,?) ON CONFLICT(title_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at',(ident,status,now()))
            if any(v is not None for v in numbers) or note:
                self.db.execute('INSERT INTO ad_tests(title_id,at,start_date,end_date,spend,registrations,trials,payments,note) VALUES(?,?,?,?,?,?,?,?,?)',(ident,now(),*dates,*numbers,note))

    def ad_info(self, ident):
        with self.lock:
            row = self.db.execute('SELECT status FROM ad_status WHERE title_id=?',(ident,)).fetchone()
            tests = [dict(x) for x in self.db.execute('SELECT * FROM ad_tests WHERE title_id=? ORDER BY id DESC',(ident,))]
        return (row['status'] if row else 'Рассмотреть'), tests

    def add_watch(self, title='', ident=''):
        title = ' '.join(str(title).split())
        if not ident and (not 1 <= len(title) <= 160 or any(ord(c)<32 for c in title)):
            raise ValueError('Введите название фильма: от 1 до 160 символов.')
        with self.lock, self.db:
            if ident:
                row = self.db.execute('SELECT * FROM titles WHERE id=?',(ident,)).fetchone()
                if not row: raise ValueError('Тайтл не найден.')
                title = row['title']
            key = ' '.join(re.sub(r'[^\w]+',' ',title.casefold()).split())
            if not key: raise ValueError('Введите название фильма.')
            existing = self.db.execute('SELECT title_id FROM watchlist WHERE name_key=?',(key,)).fetchone()
            if existing:
                resumed=not tracking_enabled(self,existing['title_id'])
                resume_tracking(self,existing['title_id'])
                return existing['title_id'], resumed
            if not ident:
                matches = [r for r in self.db.execute('SELECT id,title FROM titles')
                           if ' '.join(re.sub(r'[^\w]+',' ',r['title'].casefold()).split())==key]
                if len(matches)==1: ident=matches[0]['id']
                else:
                    ident='watch:'+hashlib.sha256(key.encode()).hexdigest()[:32]
                    stamp=now()
                    self.db.execute('INSERT OR IGNORE INTO titles(id,title,type,first_seen,last_seen,status) VALUES(?,?,"films",?,?,"watched")',(ident,title,stamp,stamp))
            resume_tracking(self,ident)
            self.db.execute('INSERT INTO watchlist VALUES(?,?,?)',(ident,key,now()))
            return ident, True

    def watch_rows(self):
        return self.rows('title ASC','id IN (SELECT title_id FROM watchlist)',10000)

    def tracking_rows(self):
        return all_tracking(self)

    def new_rows(self):
        return [r for r in self.rows('first_seen DESC', 'id IN (SELECT title_id FROM review_queue) AND id NOT IN (SELECT title_id FROM promotions WHERE active=1)', 10000) if foreign_title(self,r) and detail_for(self,r)['availability']=='available' and not self.meta('feed_baseline:'+r['id'],'')]

    def mark_reviewed(self, ident):
        with self.lock, self.db:
            self.db.execute('UPDATE review_queue SET reviewed_at=? WHERE title_id=?', (now(),ident))

    def pending_new(self):
        return [r for r in self.rows('first_seen DESC', 'id IN (SELECT title_id FROM review_queue WHERE notified_at IS NULL) AND id NOT IN (SELECT title_id FROM promotions WHERE active=1)', 10000) if foreign_title(self,r) and detail_for(self,r)['availability']=='available']

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
                self.db.execute('INSERT OR IGNORE INTO film_details(title_id,availability,evidence,updated_at) VALUES(?,"soon","Раздел Скоро TVOЁ",?)',(item['id'],timestamp))
                if item.get('year'):
                    self.db.execute('UPDATE film_details SET year=? WHERE title_id=? AND COALESCE(year,"")=""',(item['year'],item['id']))
                self.db.execute('UPDATE film_details SET availability="soon",updated_at=? WHERE title_id=? AND availability="unknown" AND evidence="Раздел Скоро TVOЁ"',(timestamp,item['id']))
                prior.pop(item['id'], None)
            for ident in prior:
                self.db.execute('UPDATE titles SET status="removed" WHERE id=?', (ident,))
                self.db.execute('UPDATE film_details SET availability="unknown" WHERE title_id=? AND availability="soon" AND evidence="Раздел Скоро TVOЁ"',(ident,))
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
            return [dict(row) for row in self.db.execute(f'SELECT * FROM titles WHERE ({where}) AND id NOT IN (SELECT title_id FROM title_control WHERE deleted=1) ORDER BY {order} LIMIT ? OFFSET ?', (*params,limit,offset))]

    def find(self, term):
        return [r for r in self.rows('title ASC', 'title LIKE ?', 10, 0, ('%'+term+'%',)) if foreign_title(self,r)]

    def history(self, ident, limit=6):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM measurements WHERE title_id=? ORDER BY id DESC LIMIT ?', (ident,limit))]


# Independent radar and promotion state, kept in the existing SQLite database.
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

RADAR_SOURCES = (
    ('google_ru','Google Trends · Россия','rss','https://trends.google.com/trending/rss?geo=RU'),
    ('interfax','Интерфакс · Новости кино','rss','https://www.interfax.ru/rss.asp'),
    ('google_news','Google Новости · Кино','rss','https://news.google.com/rss/search?q=%28%D1%84%D0%B8%D0%BB%D1%8C%D0%BC+OR+%D1%81%D0%B5%D1%80%D0%B8%D0%B0%D0%BB+OR+%D1%82%D1%80%D0%B5%D0%B9%D0%BB%D0%B5%D1%80%29+when%3A2d&hl=ru&gl=RU&ceid=RU%3Aru'),
    ('kinopoisk','Кинопоиск','telegram','https://t.me/s/kinopoisk'),
    ('kinoafisha','Киноафиша','telegram','https://t.me/s/kinoafishainfo'),
    ('filmpro','Фильм Про','telegram','https://t.me/s/filmpro_official'),
)


def name_key(value):
    return ' '.join(re.sub(r'[^\w]+',' ',str(value).casefold().replace('ё','е')).split())


def safe_url(value):
    try:
        parsed = urlparse(str(value or ''))
        return str(value) if parsed.scheme=='https' and parsed.netloc and not parsed.username and not parsed.password else ''
    except ValueError:
        return ''


def film_schema(store):
    with store.lock, store.db:
        store.db.executescript('''
        CREATE TABLE IF NOT EXISTS film_details (
            title_id TEXT PRIMARY KEY, year TEXT, poster TEXT,
            availability TEXT NOT NULL DEFAULT 'unknown', available_at TEXT,
            evidence TEXT, query_override TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS title_origin (title_id TEXT PRIMARY KEY,fingerprint TEXT,status TEXT NOT NULL DEFAULT 'unknown',countries_json TEXT,entity_id TEXT,kind TEXT,family TEXT,year TEXT,checked_at TEXT,next_check TEXT,error INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS title_control (title_id TEXT PRIMARY KEY, paused INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, name_key TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS promotions (
            title_id TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1,
            added_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decline_state (
            title_id TEXT PRIMARY KEY, period_end TEXT, confirmations INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0, checked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS radar_sources (
            id TEXT PRIMARY KEY, name TEXT, kind TEXT, url TEXT, checked_at TEXT,
            success_at TEXT, error TEXT, failures INTEGER DEFAULT 0,
            next_check TEXT, etag TEXT, modified TEXT
        );
        CREATE TABLE IF NOT EXISTS radar_items (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, reason TEXT, published_at TEXT, found_at TEXT NOT NULL,
            match_id TEXT, confirmed_id TEXT, dismissed INTEGER NOT NULL DEFAULT 0
        );
        ''')
        store.db.execute("INSERT OR IGNORE INTO film_details(title_id,availability,evidence,updated_at) SELECT id,'soon','Раздел Скоро TVOЁ',last_seen FROM titles WHERE status='active'")
        for ident,name,kind,url in RADAR_SOURCES:
            store.db.execute('INSERT OR IGNORE INTO radar_sources(id,name,kind,url) VALUES(?,?,?,?)',(ident,name,kind,url))


def detail_for(store, row):
    with store.lock:
        found=store.db.execute('SELECT * FROM film_details WHERE title_id=?',(row['id'],)).fetchone()
    detail=dict(found) if found else {'availability':'unknown','year':'','poster':'','available_at':'','query_override':''}
    if detail['availability']=='unknown' and row['status']=='active' and row['poster_date']:
        try:
            if date.fromisoformat(str(row['poster_date'])[:10]) > datetime.now(UTC).date(): detail['availability']='soon'
        except ValueError: pass
    return detail


def set_availability(store, ident, state, evidence='Подтверждено пользователем', year='', poster='', available_at=''):
    if state not in ('available','soon','unknown','unavailable'): raise ValueError('Выберите доступность фильма.')
    if year and (not year.isdigit() or not 1880<=int(year)<=datetime.now(UTC).year+10): raise ValueError('Проверьте год фильма.')
    if available_at: date.fromisoformat(available_at[:10])
    with store.lock, store.db:
        row=store.db.execute('SELECT * FROM titles WHERE id=?',(ident,)).fetchone()
        if not row: raise ValueError('Фильм не найден.')
        previous=store.db.execute('SELECT * FROM film_details WHERE title_id=?',(ident,)).fetchone()
        old=previous['availability'] if previous else 'unknown'
        # Missing availability in a feed is not evidence of withdrawal. Explicit
        # true/false from the trusted catalog can supersede an older manual check.
        if previous and state=='unknown' and evidence!='Подтверждено пользователем': return
        stamp=now()
        store.db.execute('INSERT INTO film_details(title_id,availability,available_at,evidence,year,poster,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET availability=excluded.availability,available_at=excluded.available_at,evidence=excluded.evidence,year=CASE WHEN excluded.year!="" THEN excluded.year ELSE film_details.year END,poster=CASE WHEN excluded.poster!="" THEN excluded.poster ELSE film_details.poster END,updated_at=excluded.updated_at',
                         (ident,state,available_at or (stamp if state=='available' else ''),evidence,year,safe_url(poster),stamp))
        if state=='available' and old!='available':
            store.set_meta('feed_baseline:'+ident,'')
            store.db.execute('INSERT INTO review_queue(title_id,added_at,reviewed_at,notified_at) VALUES(?,?,NULL,NULL) ON CONFLICT(title_id) DO UPDATE SET added_at=excluded.added_at,reviewed_at=NULL,notified_at=NULL',(ident,stamp))
            store.db.execute('INSERT INTO events(title_id,at,kind,detail) VALUES(?,?,"published",?)',(ident,stamp,evidence))


def tracking_enabled(store,ident):
    with store.lock:row=store.db.execute('SELECT paused,deleted FROM title_control WHERE title_id=?',(ident,)).fetchone()
    return not row or not (row['paused'] or row['deleted'])


def resume_tracking(store,ident):
    with store.lock,store.db:
        store.db.execute('UPDATE title_control SET paused=0,deleted=0,updated_at=? WHERE title_id=?',(now(),ident))


def add_manual_title(store,title,kind='films',year=''):
    if kind not in ('films','serials','cartoons') or (year and (not year.isdigit() or not 1880<=int(year)<=datetime.now(UTC).year+10)):
        raise ValueError('Проверьте тип и год тайтла.')
    with store.lock:same=[dict(r) for r in store.db.execute('SELECT * FROM titles') if name_key(r['title'])==name_key(title)]
    exact=[r for r in same if r['type']==kind and detail_for(store,r).get('year')==year] if year else []
    if len(exact)==1:return store.add_watch(ident=exact[0]['id'])
    conflict=bool(same and (len(same)>1 or any(r['type']!=kind or (year and detail_for(store,r).get('year') and detail_for(store,r).get('year')!=year) or (year and r['status']!='watched' and not detail_for(store,r).get('year')) for r in same)))
    if conflict and not year:raise ValueError('Есть одноимённые тайтлы. Укажите год.')
    chosen=title+' ('+year+', '+kind_suffix(kind)+')' if conflict else title
    ident,added=store.add_watch(chosen)
    with store.lock,store.db:
        store.db.execute('UPDATE titles SET type=? WHERE id=? AND status="watched"',(kind,ident))
        if year:
            query=title+' '+kind_suffix(kind)+' '+year
            previous=store.db.execute('SELECT wordstat_query FROM titles WHERE id=?',(ident,)).fetchone()[0]
            store.db.execute('INSERT INTO film_details(title_id,year,query_override,updated_at) VALUES(?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET year=excluded.year,query_override=CASE WHEN COALESCE(film_details.query_override,"")="" THEN excluded.query_override ELSE film_details.query_override END',(ident,year,query,now()))
            current=store.db.execute('SELECT query_override FROM film_details WHERE title_id=?',(ident,)).fetchone()[0]
            if previous and previous!=current:
                store.db.execute('UPDATE titles SET measured_at=NULL,total_count=NULL,dynamics_json=NULL,trend=NULL,delta=NULL WHERE id=?',(ident,))
                store.set_meta('dynamics_checked:'+ident,'');store.set_meta('growth_alert:'+ident,'quiet');store.set_meta('demand_alert:'+ident,'quiet')
    return ident,added


def pause_tracking(store,ident,deleted=False):
    with store.lock,store.db:
        row=store.db.execute('SELECT title FROM titles WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('Тайтл не найден.')
        key=name_key(row['title'])
        store.db.execute('INSERT INTO title_control VALUES(?,?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET paused=1,deleted=MAX(title_control.deleted,excluded.deleted),name_key=excluded.name_key,updated_at=excluded.updated_at',(ident,1,int(deleted),key,now()))
        if deleted:
            store.db.execute('UPDATE promotions SET active=0,updated_at=? WHERE title_id=?',(now(),ident))
            store.db.execute('DELETE FROM watchlist WHERE title_id=?',(ident,))
            store.db.execute('UPDATE radar_items SET dismissed=1 WHERE confirmed_id=? OR match_id=?',(ident,ident))
            for row in store.db.execute('SELECT id,title FROM radar_items WHERE confirmed_id IS NULL AND match_id IS NULL').fetchall():
                if name_key(row['title'])==key:store.db.execute('UPDATE radar_items SET dismissed=1 WHERE id=?',(row['id'],))
        store.db.execute('DELETE FROM decline_state WHERE title_id=?',(ident,))


def deleted_name(store,title):
    with store.lock:return bool(store.db.execute('SELECT 1 FROM title_control WHERE deleted=1 AND name_key=?',(name_key(title),)).fetchone())


def set_promotion(store, ident, active):
    with store.lock, store.db:
        if not store.db.execute('SELECT 1 FROM titles WHERE id=?',(ident,)).fetchone(): raise ValueError('Фильм не найден.')
        stamp=now()
        old=store.db.execute('SELECT active FROM promotions WHERE title_id=?',(ident,)).fetchone()
        store.db.execute('INSERT INTO promotions VALUES(?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at',(ident,int(active),stamp,stamp))
        if not active or not old or not old['active']: store.db.execute('DELETE FROM decline_state WHERE title_id=?',(ident,))
        if active:resume_tracking(store,ident)
        else:pause_tracking(store,ident)


def promotion_rows(store):
    return store.rows('title ASC','id IN (SELECT title_id FROM promotions WHERE active=1)',10000)


def all_tracking(store):
    # A departure from the coming-soon list is not a departure from the cinema.
    return [r for r in store.rows('title ASC','(status!="watched" OR id IN (SELECT title_id FROM watchlist) OR id IN (SELECT title_id FROM promotions WHERE active=1)) AND id NOT IN (SELECT title_id FROM title_control WHERE paused=1)',10000) if foreign_title(store,r)]


def demand_comparison(row):
    try: return weekly_comparison(json.loads(row.get('dynamics_json') or '[]'))
    except (ValueError,TypeError): return None


def signal_fresh(store, row, comparison=None):
    comparison = comparison or demand_comparison(row)
    try:
        if not comparison: return False
        lag=(datetime.now(UTC).date()-date.fromisoformat(comparison['to'])).days
        age=datetime.now(UTC)-datetime.fromisoformat(row['measured_at'])
        return 0<lag<=7 and timedelta(0)<=age<=timedelta(days=3)
    except (ValueError,TypeError,KeyError): return False


def fresh_demand(store,row):
    try:
        age=datetime.now(UTC)-datetime.fromisoformat(row['measured_at'])
        return timedelta(0)<=age<=timedelta(days=3) and not row['ambiguous'] and not store.meta('top_error:'+row['id'],'')
    except (ValueError,TypeError,KeyError):return False


def radar_tracked_ids(store):
    with store.lock:
        return {r[0] for r in store.db.execute('SELECT COALESCE(confirmed_id,match_id) FROM radar_items WHERE dismissed=0 AND COALESCE(confirmed_id,match_id) IS NOT NULL')}


def growth_rows(store, early=False):
    rows=[];promoted={r['id'] for r in promotion_rows(store)}
    for row in all_tracking(store):
        if row['id'] in promoted:continue
        n=row['total_count'] or 0
        if not early:
            # Demand threshold alone controls membership. Missing/stale dynamics
            # and query uncertainty are shown on the card, not hidden by the list.
            radar_ids=radar_tracked_ids(store)
            c=demand_comparison(row)
            radar_growth=row['id'] in radar_ids and fresh_demand(store,row) and c and signal_fresh(store,row,c) and row['trend']=='растёт' and not store.meta('dynamics_error:'+row['id'],'')
            if n>50000 or radar_growth:rows.append(row)
            continue
        if not fresh_demand(store,row):continue
        c=demand_comparison(row)
        growing=bool(c and signal_fresh(store,row,c) and row['trend']=='растёт' and not store.meta('dynamics_error:'+row['id'],''))
        if growing and 1000<=n<=50000 and c['older']>=100 and c['delta']>=100:rows.append(row)
    return sorted(rows,key=lambda r:(r['total_count'] or 0,r['delta'] or 0),reverse=True)


def growth_alert_rows(store):
    promoted={r['id'] for r in promotion_rows(store)};radar=radar_tracked_ids(store);result=[]
    for row in all_tracking(store):
        if row['id'] in promoted or not fresh_demand(store,row):continue
        c=demand_comparison(row)
        growing=bool(c and signal_fresh(store,row,c) and row['trend']=='растёт' and c['percent'] is not None and not store.meta('dynamics_error:'+row['id'],''))
        if growing or (row['total_count'] or 0)>50000:result.append(row)
    return result


def confirmed_decline(store,row):
    c=demand_comparison(row)
    with store.lock:state=store.db.execute('SELECT * FROM decline_state WHERE title_id=?',(row['id'],)).fetchone()
    return bool(state and state['confirmations']>=2 and c and c['percent'] is not None and c['percent']<=-30 and signal_fresh(store,row,c) and not row['ambiguous'] and not store.meta('dynamics_error:'+row['id'],''))


def evaluate_decline(store, row):
    if not foreign_title(store,row):return None
    c=demand_comparison(row)
    if row['ambiguous'] or not signal_fresh(store,row,c) or not c or c['percent'] is None: return None
    if store.meta('dynamics_error:'+row['id'],''): return None
    with store.lock, store.db:
        previous=store.db.execute('SELECT * FROM decline_state WHERE title_id=?',(row['id'],)).fetchone()
        if previous and previous['period_end']==c['to']: return None  # Re-reading cached data is not confirmation.
        previous_count=previous['confirmations'] if previous else 0
        sent=previous['notified'] if previous else 0
        if c['percent']<=-30:
            continuous=previous and previous['period_end'] and 0<(date.fromisoformat(c['to'])-date.fromisoformat(previous['period_end'])).days<=7
            count=previous_count+1 if continuous else 1
        else:
            count=0
            if c['percent']>-15: sent=0
        store.db.execute('INSERT INTO decline_state VALUES(?,?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET period_end=excluded.period_end,confirmations=excluded.confirmations,notified=excluded.notified,checked_at=excluded.checked_at',(row['id'],c['to'],count,sent,now()))
        if count>=2 and not sent: return {**row,'comparison':c}
    return None


def pending_declines(store):
    result=[]
    for row in promotion_rows(store):
        if not foreign_title(store,row):continue
        c=demand_comparison(row)
        with store.lock: state=store.db.execute('SELECT * FROM decline_state WHERE title_id=?',(row['id'],)).fetchone()
        if state and state['confirmations']>=2 and not state['notified'] and c and signal_fresh(store,row,c) and c['percent'] is not None and c['percent']<=-30 and not row['ambiguous'] and not store.meta('dynamics_error:'+row['id'],''):
            result.append({**row,'comparison':c})
    return result



# Official Wikidata metadata. No page scraping, credentials or paid API.
WIKIDATA_API = 'https://www.wikidata.org/w/api.php'
RUSSIAN_ORIGINS = {'Q159','Q15180','Q34266'}


def origin_fingerprint(store,row):
    year=detail_for(store,row).get('year','') if not row['id'].startswith('radar:') else row.get('year','')
    return hashlib.sha256((name_key(row['title'])+'|'+str(year or '')+'|'+str(row.get('type') or '')).encode()).hexdigest()


def origin_info(store,row):
    if isinstance(row,str):
        with store.lock:r=store.db.execute('SELECT * FROM titles WHERE id=?',(row,)).fetchone()
        if not r:return {'status':'unknown'}
        row=dict(r)
    with store.lock:r=store.db.execute('SELECT * FROM title_origin WHERE title_id=?',(row['id'],)).fetchone()
    return dict(r) if r and r['fingerprint']==origin_fingerprint(store,row) else {'status':'unknown'}


def foreign_title(store,row):
    return origin_info(store,row)['status']=='foreign'


def save_origin(store,row,data):
    stamp=now();status=data.get('status','unknown')
    if status not in ('foreign','russian','unknown'):status='unknown'
    with store.lock,store.db:
        # Year and kind come from the uniquely matched entity, never from its popularity.
        if status in ('foreign','russian') and not row['id'].startswith('radar:'):
            year=data.get('year','');kind=data.get('kind','')
            if year:
                store.db.execute('INSERT INTO film_details(title_id,year,updated_at) VALUES(?,?,?) ON CONFLICT(title_id) DO UPDATE SET year=CASE WHEN COALESCE(film_details.year,"")="" THEN excluded.year ELSE film_details.year END',(row['id'],year,stamp))
            if kind in ('films','serials','cartoons'):
                # Preserve source kind; display_kind comes from this metadata record.
                pass
        retry=(datetime.now(UTC)+timedelta(days=30 if status!='unknown' else 1)).isoformat()
        if data.get('error'):retry=(datetime.now(UTC)+timedelta(hours=1)).isoformat()
        store.db.execute('INSERT INTO title_origin(title_id,fingerprint,status,countries_json,entity_id,kind,family,year,checked_at,next_check,error) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET fingerprint=excluded.fingerprint,status=excluded.status,countries_json=excluded.countries_json,entity_id=excluded.entity_id,kind=excluded.kind,family=excluded.family,year=excluded.year,checked_at=excluded.checked_at,next_check=excluded.next_check,error=excluded.error',
            (row['id'],origin_fingerprint(store,row),status,json.dumps(data.get('countries',[]),ensure_ascii=False),data.get('entity_id',''),data.get('kind',''),data.get('family') or (data.get('kind') if data.get('kind') in ('films','serials') else ''),data.get('year',''),stamp,retry,bool(data.get('error'))))


class OriginUnavailable(Exception):
    pass


def _wikidata_request(params):
    from urllib.parse import urlencode
    import gzip,io
    url=WIKIDATA_API+'?'+urlencode({'format':'json','maxlag':'5',**params})
    request=Request(url,headers={'User-Agent':'TVOE-Demand-Monitor/2.2 (https://github.com/senyaav27/Wortstat-senyaa-)','Accept':'application/json','Accept-Encoding':'gzip'})
    try:
        with urlopen(request,timeout=12) as response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise OriginUnavailable()
            if response.headers.get('Content-Encoding')=='gzip':raw=gzip.GzipFile(fileobj=io.BytesIO(raw)).read(2_000_001)
        if len(raw)>2_000_000:raise OriginUnavailable()
        data=json.loads(raw)
        if not isinstance(data,dict) or data.get('error'):raise OriginUnavailable()
        return data
    except (HTTPError,URLError,OSError,TimeoutError,ValueError,UnicodeError) as exc:
        raise OriginUnavailable() from None


WIKIDATA_IO_LOCK=threading.Lock()
WIKIDATA_LAST_CALL=0.0


def wikidata_request(params):
    # One request at a time, an overall deadline also covers blocked DNS.
    from queue import Queue,Empty
    if not WIKIDATA_IO_LOCK.acquire(blocking=False):raise OriginUnavailable()
    result=Queue(maxsize=1)
    def run():
        global WIKIDATA_LAST_CALL
        try:
            delay=max(0.0,1.0-(time.monotonic()-WIKIDATA_LAST_CALL))
            if delay:time.sleep(delay)
            WIKIDATA_LAST_CALL=time.monotonic()
            result.put(_wikidata_request(params))
        except Exception:result.put(None)
        finally:WIKIDATA_IO_LOCK.release()
    worker=threading.Thread(target=run,daemon=True);worker.start()
    try:data=result.get(timeout=15)
    except Empty:raise OriginUnavailable() from None
    if data is None:raise OriginUnavailable()
    return data


def claim_values(entity,prop):
    return [r.get('mainsnak',{}).get('datavalue',{}).get('value') for r in entity.get('claims',{}).get(prop,[]) if r.get('rank')!='deprecated']


def claim_ids(entity,prop):
    return {v['id'] for v in claim_values(entity,prop) if isinstance(v,dict) and re.fullmatch(r'Q\d+',v.get('id',''))}


class WikidataOrigin:
    def __init__(self,transport=wikidata_request):
        self.transport=transport;self.classes={};self.countries={}

    def entities(self,ids):
        if not ids:return {}
        return self.transport({'action':'wbgetentities','ids':'|'.join(sorted(ids)[:50]),'props':'labels|aliases|claims','languages':'ru|en'}).get('entities',{})

    def kind(self,entity):
        found=claim_ids(entity,'P31');front=set(found)
        for _ in range(2):
            if found & {'Q11424','Q5398426','Q202866','Q581714'}:break
            missing=front-self.classes.keys()
            if missing:self.classes.update(self.entities(missing))
            front={p for q in front for p in claim_ids(self.classes.get(q,{}),'P279')}-found
            found.update(front)
            if not front:break
        family='serials' if found & {'Q5398426','Q581714'} else 'films' if found & {'Q11424','Q202866'} else ''
        if found & {'Q202866','Q581714'}:return 'cartoons',family
        return family,family

    def resolve(self,title,year='',kind=''):
        title=re.sub(r'\s*\(\d{4},\s*(?:фильм|сериал|мультфильм)\)\s*$','',title).strip()
        search=self.transport({'action':'wbsearchentities','search':title,'language':'ru','uselang':'ru','limit':'50','type':'item'})
        if search.get('search-continue') is not None:return {'status':'unknown'}
        ids={x['id'] for x in search.get('search',[]) if re.fullmatch(r'Q\d+',x.get('id',''))}
        entities=self.entities(ids);matches=[]
        for q,e in entities.items():
            aliases=[v.get('value','') for v in e.get('labels',{}).values()]
            aliases += [v.get('value','') for values in e.get('aliases',{}).values() for v in values]
            if name_key(title) not in {name_key(a) for a in aliases}:continue
            years={v['time'][1:5] for v in claim_values(e,'P577') if isinstance(v,dict) and re.match(r'\+\d{4}-',v.get('time','')) and v.get('precision',0)>=9}
            if year and year not in years:continue
            entity_kind,family=self.kind(e)
            if not entity_kind or (kind in ('films','serials') and family!=kind) or (kind=='cartoons' and entity_kind!='cartoons'):continue
            matches.append((q,e,entity_kind,family,min(years) if years else ''))
        # Never pick the first search result when remakes or namesakes exist.
        if len(matches)!=1:return {'status':'unknown'}
        q,e,entity_kind,family,entity_year=matches[0];country_ids=claim_ids(e,'P495')
        if not country_ids:return {'status':'unknown'}
        result={'entity_id':q,'kind':entity_kind,'family':family,'year':year or entity_year,'countries':[]}
        if country_ids & RUSSIAN_ORIGINS:return {**result,'status':'russian','countries':sorted(country_ids)}
        missing=country_ids-self.countries.keys()
        if missing:self.countries.update(self.entities(missing))
        for country in sorted(country_ids):
            country_entity=self.countries.get(country,{})
            codes={str(v).upper() for v in claim_values(country_entity,'P297')}
            if codes & {'RU','SU'}:return {**result,'status':'russian'}
            if not codes or not all(re.fullmatch(r'[A-Z]{2}',v) for v in codes):return {'status':'unknown'}
            label=country_entity.get('labels',{}).get('ru',{}).get('value','')
            if not label:return {'status':'unknown'}
            result['countries'].append(label)
        return {**result,'status':'foreign'}


def raw_radar_rows(store):
    cutoff=(datetime.now(UTC)-timedelta(days=7)).isoformat()
    with store.lock:
        return [dict(r) for r in store.db.execute('SELECT r.*,s.name source_name FROM radar_items r JOIN radar_sources s ON s.id=r.source_id WHERE r.dismissed=0 AND COALESCE(NULLIF(r.published_at,""),r.found_at)>=? ORDER BY r.found_at DESC LIMIT 500',(cutoff,))]


def radar_identity(store,row):
    ident=row.get('confirmed_id') or row.get('match_id')
    if ident:
        with store.lock:title=store.db.execute('SELECT * FROM titles WHERE id=?',(ident,)).fetchone()
        if title:return dict(title)
    return {'id':'radar:'+row['id'],'title':row['title'],'type':store.meta('radar_kind:'+row['id'],''),'year':''}


def refresh_origins(store,resolver=None,budget=10,target_id=None):
    if store.meta('origin_retry_at','')>now():return 0
    if not store.origin_lock.acquire(blocking=False):return 0
    try:
        resolver=resolver or WikidataOrigin()
        rows=store.rows('first_seen DESC','1=1',10000)
        rows += [radar_identity(store,r) for r in raw_radar_rows(store)]
        if target_id:rows.sort(key=lambda r:r['id']!=target_id)
        done=0;seen=set();cached={}
        for row in rows:
            if row['id'] in seen:continue
            seen.add(row['id']);info=origin_info(store,row)
            if info.get('next_check','')>now():continue
            if done>=budget:break
            fingerprint=origin_fingerprint(store,row)
            year=row.get('year','') if row['id'].startswith('radar:') else detail_for(store,row).get('year','')
            try:
                data=cached.get(fingerprint)
                if data is None:
                    data=resolver.resolve(row['title'],year or '',row.get('type') or '')
                    cached[fingerprint]=data;done+=1
                with store.lock:
                    latest=row if row['id'].startswith('radar:') else store.db.execute('SELECT * FROM titles WHERE id=?',(row['id'],)).fetchone()
                    if not latest or origin_fingerprint(store,dict(latest))!=fingerprint:continue
                    save_origin(store,dict(latest),data)
            except OriginUnavailable:
                if info['status'] in ('foreign','russian'):
                    with store.lock,store.db:store.db.execute('UPDATE title_origin SET next_check=?,error=1 WHERE title_id=?',((datetime.now(UTC)+timedelta(hours=1)).isoformat(),row['id']))
                else:save_origin(store,row,{'status':'unknown','error':True})
                store.set_meta('origin_retry_at',(datetime.now(UTC)+timedelta(hours=1)).isoformat())
                store.set_meta('origin_error','Источник стран временно недоступен. Неопределённые тайтлы скрыты.')
                break  # Back off globally, no loop over titles after 429/maxlag/error.
        else:store.set_meta('origin_error','')
        auto_track_radar(store)
        return done
    finally:store.origin_lock.release()

def bounded_text(url, headers=None, max_bytes=2_000_000):
    req=Request(url,headers={'User-Agent':'TVOE-Demand-Monitor/2.0','Accept':'application/rss+xml, application/xml, text/html;q=0.8',**(headers or {})})
    with urlopen(req,timeout=12) as response:
        raw=response.read(max_bytes+1)
        if len(raw)>max_bytes: raise ValueError('Source too large')
        return raw.decode('utf-8-sig'),dict(response.headers)


def parse_rss(raw, google=False):
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper(): raise ValueError('Unsafe XML')
    root=ET.fromstring(raw)
    if root.tag not in ('rss','{http://www.w3.org/2005/Atom}feed'): raise ValueError('Not a feed')
    ns='{https://trends.google.com/trending/rss}'
    result=[]
    for node in root.findall('./channel/item')[:100]:
        title=node.findtext('title','').strip();url=safe_url(node.findtext('link',''))
        if not title or not url: continue
        try: stamp=parsedate_to_datetime(node.findtext('pubDate','')).astimezone(UTC).isoformat()
        except (ValueError,TypeError,AttributeError): stamp=''
        description=html.unescape(re.sub('<[^>]+>',' ',node.findtext('description','')))
        news=' '.join(x.findtext(ns+'news_item_title','') for x in node.findall(ns+'news_item'))
        result.append({'title':title,'text':title+' '+description+' '+news,'url':url,'published_at':stamp})
    return result


class PublicTelegramParser(HTMLParser):
    def __init__(self):
        super().__init__();self.posts=[];self.current=None;self.depth=0;self.capture=0
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);classes=a.get('class','').split()
        if 'tgme_widget_message' in classes and re.fullmatch(r'[A-Za-z0-9_]+/\d+',a.get('data-post','')):
            self.current={'title':'','text':'','url':'https://t.me/'+a['data-post'],'published_at':''};self.depth=1
        elif self.current and tag not in ('br','img','meta','link','input','hr'): self.depth+=1
        if self.current:
            if 'tgme_widget_message_text' in classes:self.capture=self.depth
            if tag=='time':self.current['published_at']=a.get('datetime','')
            if tag=='br' and self.capture:self.current['text']+=' '
    def handle_endtag(self,tag):
        if tag in ('br','img','meta','link','input','hr'):return
        if self.current:
            if self.capture==self.depth:self.capture=0
            self.depth-=1
            if self.depth<=0:
                if self.current['text'].strip():self.posts.append(self.current)
                self.current=None;self.capture=0
    def handle_data(self,data):
        if self.current and self.capture:self.current['text']+=data


def parse_telegram(raw):
    if 'tgme_widget_message' not in raw or 'Site Unavailable' in raw or 'captcha' in raw.lower(): raise ValueError('Public preview unavailable')
    parser=PublicTelegramParser();parser.feed(raw)
    if not parser.posts: raise ValueError('No readable public posts')
    return parser.posts[-30:]


def reason_for(text, google=False):
    lower=text.casefold()
    if 'трейлер' in lower:return 'Упоминание трейлера'
    if 'сезон' in lower:return 'Упоминание сезона'
    if 'премьер' in lower or 'релиз' in lower:return 'Упоминание премьеры или релиза'
    if 'прокат' in lower or 'сбор' in lower:return 'Упоминание проката'
    return 'Всплеск поискового интереса' if google else 'Упоминание в публикации'


def radar_candidates(store, item, google=False):
    text=item['text'];key=' '+name_key(text)+' '
    matches=[]
    for row in store.rows('title ASC','1=1',10000):
        k=name_key(row['title'])
        if len(k)>=5 and (' '+k+' ') in key: matches.append(row)
    candidates={name_key(r['title']):(r['title'],r['id'] if sum(name_key(x['title'])==name_key(r['title']) for x in matches)==1 else None) for r in matches}
    context=bool(re.search(r'фильм|сериал|трейлер|мультфильм|кино|премьер|сезон',text,re.I))
    if context:
        for quoted in re.finditer(r'«([^»\n]{2,160})»',text):
            title=quoted.group(1)
            prefix=text[max(0,quoted.start()-65):quoted.start()]
            # Singular film/series context next to the quote; do not treat publishers
            # and streaming services quoted elsewhere in a headline as movie names.
            explicit=re.search(r'(?:фильм(?:а|е|ом|у)?|сериал(?:а|е|ом|у)?|мультфильм(?:а|е|ом|у)?|трейлер(?:а|е|ом|у)?|хоррор(?:а|е|ом|у)?|комеди(?:я|и|ю)|триллер(?:а|е|ом|у)?)\s+(?:[^\s«»():.,]{1,20}\s+){0,2}$',prefix,re.I)
            if explicit and not re.search(r'\b(?:в|на|от|для|с|у|канале|платформе)\s+$',prefix,re.I) and (2<=len(title.split())<=15 or len(title)>=5):
                candidates.setdefault(name_key(title),(title,None))
        if google and not candidates and re.search(r'фильм|сериал',item['title'],re.I):
            title=re.sub(r'\s+(фильм|сериал)(\s+\d{4})?$', '',item['title'],flags=re.I).strip()
            if len(title)>=5:candidates[name_key(title)]=(title,None)
    return list(candidates.values())[:8]


def import_radar(store, source, items):
    stamp=now();added=0
    with store.lock,store.db:
        for item in items:
            published=item.get('published_at','')
            if published:
                try:
                    age=datetime.now(UTC)-datetime.fromisoformat(published.replace('Z','+00:00'))
                    if age>timedelta(days=7) or age<timedelta(hours=-2):continue
                except (ValueError,TypeError):published=''
            for title,ident in radar_candidates(store,item,source['id']=='google_ru'):
                if not title or not safe_url(item['url']) or deleted_name(store,title):continue
                stable=hashlib.sha256((source['id']+'|'+item['url']+'|'+name_key(title)+'|'+published).encode()).hexdigest()
                cur=store.db.execute('INSERT OR IGNORE INTO radar_items(id,source_id,url,title,reason,published_at,found_at,match_id) VALUES(?,?,?,?,?,?,?,?)',(stable,source['id'],item['url'],title[:160],reason_for(item['text'],source['id']=='google_ru'),published,stamp,ident))
                added+=cur.rowcount
                # Keep only structured identity hints, never the full publication.
                if cur.rowcount:
                    kinds={k.lower() for k in re.findall(r'\b(фильм|сериал)(?:а|е|ом|у)?\b',item['text'],re.I)}
                    if len(kinds)==1:
                        store.set_meta('radar_kind:'+stable,'serials' if next(iter(kinds)).lower()=='сериал' else 'films')
    return added


def radar_rows(store):
    return [r for r in raw_radar_rows(store) if foreign_title(store,radar_identity(store,r))]


def auto_track_radar(store):
    """Discover candidates within existing Wordstat budgets, without claiming availability."""
    grouped={}
    for row in radar_rows(store):
        if row['confirmed_id'] or deleted_name(store,row['title']):continue
        grouped.setdefault(name_key(row['title']),[]).append(row)
    added=0
    for rows in grouped.values():
        # Known catalogue IDs are safe; unknown works require independent sources
        # and consistent film/series context. Google News is an aggregator.
        known={r['match_id'] for r in rows if r['match_id']}
        if len(known)>1:continue
        kinds={store.meta('radar_kind:'+r['id'],'') for r in rows}-{''}
        independent={r['source_id'] for r in rows if r['source_id']!='google_news'}
        if known:
            ident=next(iter(known))
            if not tracking_enabled(store,ident):continue
            if store.meta('auto_radar_seen:'+ident,''):continue
            ident,new=store.add_watch(ident=ident)
        else:
            verified=origin_info(store,radar_identity(store,rows[0]))
            if verified['status']!='foreign':continue
            # Do not choose among remakes or overwrite a manually identified title.
            matches=[r for r in store.rows('title ASC','1=1',10000) if name_key(r['title'])==name_key(rows[0]['title'])]
            if matches:continue
            ident,new=store.add_watch(rows[0]['title'])
            kind=verified.get('family') or verified.get('kind') or (next(iter(kinds)) if len(kinds)==1 else 'films')
            with store.lock,store.db:
                store.db.execute('UPDATE titles SET type=? WHERE id=? AND status="watched"',(kind,ident))
        if not known:
            save_origin(store,dict(store.db.execute('SELECT * FROM titles WHERE id=?',(ident,)).fetchone()),{**verified,'countries':json.loads(verified.get('countries_json') or '[]')})
        with store.lock,store.db:
            store.set_meta('auto_radar_seen:'+ident,now())
            store.set_meta('auto_radar:'+ident,'Найден автоматически по источникам. Доступность TVOЁ не подтверждена.')
            for row in rows:store.db.execute('UPDATE radar_items SET confirmed_id=? WHERE id=?',(ident,row['id']))
        added+=int(new)
    return added


def refresh_radar(store, transport=bounded_text):
    if not store.radar_lock.acquire(blocking=False):return 0
    try:return _refresh_radar(store,transport)
    finally:store.radar_lock.release()


def _refresh_radar(store, transport=bounded_text):
    with store.lock:sources=[dict(r) for r in store.db.execute('SELECT * FROM radar_sources')]
    added=0
    for source in sources:
        if source['next_check'] and source['next_check']>now():continue
        headers={}
        if source['etag']:headers['If-None-Match']=source['etag']
        if source['modified']:headers['If-Modified-Since']=source['modified']
        stamp=now()
        try:
            raw,h=transport(source['url'],headers)
            items=parse_telegram(raw) if source['kind']=='telegram' else parse_rss(raw,source['id']=='google_ru')
            added+=import_radar(store,source,items)
            with store.lock,store.db:
                store.db.execute('UPDATE radar_sources SET checked_at=?,success_at=?,error="",failures=0,next_check=?,etag=?,modified=? WHERE id=?',(stamp,stamp,(datetime.now(UTC)+timedelta(minutes=30)).isoformat(),h.get('ETag',''),h.get('Last-Modified',''),source['id']))
        except HTTPError as exc:
            if exc.code==304:
                with store.lock,store.db:store.db.execute('UPDATE radar_sources SET checked_at=?,success_at=?,error="",next_check=? WHERE id=?',(stamp,stamp,(datetime.now(UTC)+timedelta(minutes=30)).isoformat(),source['id']))
                continue
            radar_error(store,source,stamp)
        except (URLError,OSError,TimeoutError,ValueError,ET.ParseError,UnicodeError):radar_error(store,source,stamp)
    auto_track_radar(store)
    return added


def radar_error(store,source,stamp):
    failures=source['failures']+1
    with store.lock,store.db:
        store.db.execute('UPDATE radar_sources SET checked_at=?,error=?,failures=?,next_check=? WHERE id=?',(stamp,'Источник временно недоступен. Последние найденные публикации сохранены.',failures,(datetime.now(UTC)+timedelta(minutes=min(360,30*2**min(failures,4)))).isoformat(),source['id']))


def parse_catalog_feed(raw):
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():raise ValueError('Небезопасный XML.')
    root=ET.fromstring(raw)
    if root.tag!='yml_catalog':raise ValueError('Загрузите каталог TVOЁ в формате YML/XML.')
    offers=root.findall('./shop/offers/offer')
    if not offers or len(offers)>10000:raise ValueError('Фид пуст или содержит более 10 000 тайтлов.')
    parsed=[];seen=set()
    for offer in offers:
        ident=offer.get('id','').strip();title=offer.findtext('name','').strip();link=offer.findtext('url','').strip()
        if not ident or len(ident)>160 or not title or len(title)>160 or ident in seen:raise ValueError('Проверьте ID и названия: они должны быть заполнены, без повторов ID.')
        seen.add(ident)
        if link:
            u=urlparse(link)
            if u.scheme!='https' or u.hostname not in ('tvoe.live','www.tvoe.live'):raise ValueError('Ссылки фида должны вести на TVOЁ.')
        params={p.get('name','').casefold():p.text or '' for p in offer.findall('param')}
        year=params.get('год','').strip()
        if year and (not year.isdigit() or not 1880<=int(year)<=datetime.now(UTC).year+10):raise ValueError('Проверьте год в фиде.')
        kind='serials' if 'сериал' in params.get('тип','').casefold() else 'films'
        available=offer.get('available','').casefold()
        state='available' if available=='true' else 'unavailable' if available=='false' else 'unknown'
        parsed.append({'id':ident,'name':title,'url':link,'type':kind,'year':year,'poster':offer.findtext('picture',''),'availability':state})
    return parsed


def import_catalog_feed(store,raw):
    items=parse_catalog_feed(raw)  # Validate everything before mutating the existing data.
    baseline=not store.meta('catalog_feed_updated','')
    stamp=now()
    with store.lock,store.db:
        for item in items:
            row=store.db.execute('SELECT * FROM titles WHERE id=?',(item['id'],)).fetchone()
            if not row:
                store.db.execute('INSERT INTO titles(id,title,type,url,first_seen,last_seen,status) VALUES(?,?,?,?,?,?,"active")',(item['id'],item['name'],item['type'],item['url'],stamp,stamp))
            else:
                store.db.execute('UPDATE titles SET title=?,type=?,url=?,last_seen=? WHERE id=?',(item['name'],item['type'],item['url'],stamp,item['id']))
            before=detail_for(store,{'id':item['id'],'status':'active','poster_date':None})['availability']
            set_availability(store,item['id'],item['availability'],'Каталог TVOЁ из фида',year=item['year'],poster=item['poster'])
            if baseline and before!='available':
                # First full catalog establishes a baseline, not a mass new-title alert.
                store.db.execute('UPDATE review_queue SET reviewed_at=?,notified_at=? WHERE title_id=?',(stamp,stamp,item['id']))
                store.set_meta('feed_baseline:'+item['id'],'1')
        store.set_meta('catalog_feed_updated',stamp)
    return len(items)


def refresh_catalog_feed(store):
    url=os.environ.get('TVOE_CATALOG_FEED_URL','').strip()
    if not url:return False
    if not safe_url(url):
        store.set_meta('catalog_feed_error','Проверьте адрес фида TVOЁ.');return False
    try:
        raw,_=bounded_text(url,max_bytes=8_000_000)
        import_catalog_feed(store,raw);store.set_meta('catalog_feed_error','');return True
    except (HTTPError,URLError,OSError,TimeoutError,ValueError,ET.ParseError):
        store.set_meta('catalog_feed_error','Фид TVOЁ временно недоступен. Сохранённый каталог не удалён.');return False


def confirm_radar(store, ident, title='', year='', kind='films'):
    with store.lock,store.db:
        row=store.db.execute('SELECT * FROM radar_items WHERE id=? AND dismissed=0',(ident,)).fetchone()
        if not row:raise ValueError('Публикация не найдена.')
        if kind not in ('films','serials','cartoons'):raise ValueError('Выберите фильм или сериал.')
        if year and (not year.isdigit() or not 1880<=int(year)<=datetime.now(UTC).year+10):raise ValueError('Проверьте год.')
        if row['confirmed_id']:return row['confirmed_id'],False
        chosen=title.strip() or row['title']
        same=[r for r in store.rows('title ASC','1=1',10000) if name_key(r['title'])==name_key(chosen)]
        exact=[r for r in same if r['type']==kind and detail_for(store,r).get('year')==year] if year else []
        conflict=(any((detail_for(store,r).get('year') and detail_for(store,r).get('year')!=year) or (r['type'] and r['type']!=kind and r['status']!='watched') for r in same) if year else len(same)>1)
        if len(exact)==1:
            film_id,added=store.add_watch(ident=exact[0]['id'])
        elif year and conflict:
            film_id,added=store.add_watch(chosen+' ('+year+', '+kind_suffix(kind)+')')
        elif not year and conflict:
            raise ValueError('Есть несколько тайтлов с таким названием. Укажите год.')
        else:
            film_id,added=store.add_watch(chosen)
        store.db.execute('UPDATE titles SET type=? WHERE id=? AND status="watched"',(kind,film_id))
        if year:
            previous_query=store.db.execute('SELECT wordstat_query FROM titles WHERE id=?',(film_id,)).fetchone()[0]
            query=chosen+' '+kind_suffix(kind)+' '+year
            store.db.execute('INSERT INTO film_details(title_id,year,query_override,updated_at) VALUES(?,?,?,?) ON CONFLICT(title_id) DO UPDATE SET year=excluded.year,query_override=COALESCE(NULLIF(film_details.query_override,""),excluded.query_override),updated_at=excluded.updated_at',(film_id,year,query,now()))
            effective_query=store.db.execute('SELECT query_override FROM film_details WHERE title_id=?',(film_id,)).fetchone()[0]
            if previous_query and previous_query!=effective_query:
                store.db.execute('UPDATE titles SET measured_at=NULL,total_count=NULL,dynamics_json=NULL,trend=NULL,delta=NULL WHERE id=?',(film_id,))
                store.set_meta('dynamics_checked:'+film_id,'')
                store.set_meta('growth_alert:'+film_id,'quiet')
                store.db.execute('DELETE FROM decline_state WHERE title_id=?',(film_id,))
        store.db.execute('UPDATE radar_items SET confirmed_id=? WHERE id=?',(film_id,ident))
    return film_id,added


class Analyzer:
    def __init__(self, store, wordstat, transport=tvoe_request, top_budget=100, dynamics_budget=12):
        self.store, self.wordstat, self.transport = store, wordstat, transport
        self.top_budget, self.dynamics_budget = top_budget, dynamics_budget
        self.lock = threading.Lock()
        self.on_synced = None

    def refresh(self, watch_id=None):
        if not self.lock.acquire(blocking=False):
            return {'busy': True}
        stats = {'new': [], 'measured': 0, 'dynamics': 0, 'alerts': [], 'errors': []}
        try:
            if watch_id is None:
                auto_track_radar(self.store)
                try:
                    refresh_catalog_feed(self.store)
                    items = fetch_tvoe_items(self.transport)
                    stats['new'] = self.store.sync(items)
                    if self.on_synced:
                        self.on_synced()
                except (APIError, RuntimeError) as exc:
                    stats['errors'].append(str(exc))
            rows = self.store.tracking_rows()
            if watch_id is not None:
                rows = [r for r in rows if r['id']==watch_id]
            def fresh(stamp):
                try: return datetime.now(UTC)-datetime.fromisoformat(stamp) < timedelta(hours=24)
                except (ValueError,TypeError): return False
            # Reuse stored data for the day, including when a title is added twice.
            rows = [r for r in rows if not fresh(r['measured_at'])]
            if self.store.meta('wordstat_retry_at','')>now():
                rows=[]
                stats['errors'].append('Wordstat: ожидание после ограничения частоты запросов')
            # Refresh never measured entries first, then stale measurements.
            promoted={r['id'] for r in promotion_rows(self.store)}
            rows.sort(key=lambda r: (r['id'] not in promoted,r['measured_at'] or ''))
            day=datetime.now(UTC).date().isoformat()
            top_key='daily_top:'+day
            top_remaining=max(0,self.top_budget-int(self.store.meta(top_key,'0')))
            self.store.set_meta('wordstat_top_queue',str(max(0,len(rows)-top_remaining)))
            for row in rows[:top_remaining]:
                if not tracking_enabled(self.store,row['id']):continue
                self.store.set_meta(top_key,str(int(self.store.meta(top_key,'0'))+1))
                try:
                    override = detail_for(self.store,row).get('query_override')
                    initial = self.wordstat.top(override or row['title'])
                    query, note, ambiguous = ((override, 'Запрос подтверждён пользователем.', False) if override else choose_query({**row,'type':origin_info(self.store,row).get('family') or row['type']}, initial))
                    if not tracking_enabled(self.store,row['id']):continue
                    top = self.wordstat.top(query) if query != (override or row['title']) else initial
                    count = int(top['totalCount'])
                    self.store.measured(row['id'],query,note,ambiguous,count,None)
                    if count <= 50000:
                        self.store.set_meta('radar_alert:'+row['id'],'quiet')
                        self.store.set_meta('demand_alert:'+row['id'],'quiet')
                    self.store.set_meta('top_error:'+row['id'],'')
                    stats['measured'] += 1
                except APIError as exc:
                    self.store.set_meta('top_error:'+row['id'], now()+' | '+str(exc))
                    stats['errors'].append(str(exc))
                    if 'HTTP 429' in str(exc):
                        self.store.set_meta('wordstat_retry_at',(datetime.now(UTC)+timedelta(hours=1)).isoformat())
                        break
            watched = {r['id'] for r in self.store.watch_rows()}
            candidates = [row for row in self.store.tracking_rows()
                          if row['total_count'] is not None and not row['ambiguous'] and row['wordstat_query']]
            candidates = [r for r in candidates if (watch_id is None or r['id']==watch_id)
                          and not fresh(self.store.meta('dynamics_checked:'+r['id'],''))]
            if any('HTTP 429' in e for e in stats['errors']) or self.store.meta('wordstat_retry_at','')>now(): candidates=[]
            # Rotate through eligible titles across days, including ones not measured in this pass.
            def overdue(row):
                try:return datetime.now(UTC)-datetime.fromisoformat(self.store.meta('dynamics_checked:'+row['id'],''))>=timedelta(days=6)
                except (ValueError,TypeError):return True
            candidates.sort(key=lambda row: (not overdue(row),row['id'] not in promoted,(row['total_count'] or 0)<=50000,self.store.meta('dynamics_checked:'+row['id'],''),
                                             -(row['total_count'] or 0)))
            dyn_key='daily_dynamics:'+day
            dyn_remaining=max(0,self.dynamics_budget-int(self.store.meta(dyn_key,'0')))
            self.store.set_meta('wordstat_dynamics_queue',str(max(0,len(candidates)-dyn_remaining)))
            for row in candidates[:dyn_remaining]:
                if not tracking_enabled(self.store,row['id']):continue
                self.store.set_meta(dyn_key,str(int(self.store.meta(dyn_key,'0'))+1))
                try:
                    points = self.wordstat.dynamics(row['wordstat_query'])
                    self.store.attach_dynamics(row['id'],points)
                    self.store.set_meta('dynamics_checked:'+row['id'],now())
                    self.store.set_meta('dynamics_error:'+row['id'],'')
                    stats['dynamics'] += 1
                    direction, delta = trend(points)
                    if direction in ('стабильно','падает'):
                        self.store.set_meta('growth_alert:'+row['id'],'quiet')
                except APIError as exc:
                    self.store.set_meta('dynamics_error:'+row['id'],now()+' | '+str(exc))
                    stats['errors'].append(str(exc))
                    if 'HTTP 429' in str(exc):
                        self.store.set_meta('wordstat_retry_at',(datetime.now(UTC)+timedelta(hours=1)).isoformat())
                        break
            for promoted in promotion_rows(self.store):
                evaluate_decline(self.store,promoted)
            tracking = self.store.tracking_rows()
            checked = sum(row['total_count'] is not None for row in tracking)
            if watch_id is None and checked == len(tracking) and not stats['errors']:
                self.store.set_meta('last_full',now())
            if watch_id is None:
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
    [{'text':'🎯 В рекламе','callback_data':'ads'}, {'text':'👁 Следить','callback_data':'watch'}],
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
             'new':'id IN (SELECT title_id FROM review_queue WHERE reviewed_at IS NULL) AND id IN (SELECT title_id FROM film_details WHERE availability="available")'}[kind]
    if kind=='growth':
        rows=growth_rows(store)[page*5:(page+1)*5]
    elif kind=='new':
        rows=store.new_rows()[page*5:(page+1)*5]
    elif kind == 'soon':
        rows = sorted([r for r in store.rows('title ASC', where+' AND id NOT IN (SELECT title_id FROM promotions WHERE active=1)', 10000) if foreign_title(store,r)], key=lambda r: poster_sort(r['poster_date']))[page*5:(page+1)*5]
    else:
        rows = [r for r in store.rows(order, where+' AND id NOT IN (SELECT title_id FROM promotions WHERE active=1)',10000) if foreign_title(store,r)][page*5:(page+1)*5]
    labels = {'top':'🔥 Популярное','growth':'🔎 Сигналы: спрос выше 50 000','soon':'📅 Скоро в подписке','new':'🆕 Новые тайтлы'}
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
        except HTTPError as exc:
            raise RuntimeError('Telegram HTTP '+str(exc.code)) from None
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            raise RuntimeError('Telegram '+type(exc).__name__) from None

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



def title_analysis(store, row, csrf_field):
    esc = html.escape
    try: points = json.loads(row['dynamics_json'] or '[]')
    except (ValueError,TypeError): points=[]
    comparison = weekly_comparison(points)
    checked = store.meta('dynamics_checked:'+row['id'],'')
    freshness = ('Проверено '+date_text(row['measured_at'])) if row['measured_at'] else 'Спрос ещё не проверен'
    if row['measured_at']:
        try:
            if datetime.now(UTC)-datetime.fromisoformat(row['measured_at']) > timedelta(days=3): freshness += ' · данные старше 3 дней'
        except ValueError: pass
    errors = [store.meta(k+row['id'],'') for k in ('top_error:','dynamics_error:')]
    err = '<p class="data-error">Не удалось обновить часть данных спроса. Показаны последние сохранённые данные.</p>' if any(errors) else ''
    info = '<p>'+esc(freshness)+' · Динамика: '+esc(row['trend'] or 'ещё не получена')+'<br>Динамика проверена: '+esc(date_text(checked) if checked else 'дата неизвестна')+'</p>'+err
    chart = ''
    valid=[]
    for pt in points:
        try:
            d=date.fromisoformat(str(pt['date'])[:10]);c=int(pt['count'])
            if d < datetime.now(UTC).date() and c>=0: valid.append((d,c))
        except (ValueError,TypeError,KeyError): pass
    valid=sorted(valid)[-28:]
    if len(valid)>1:
        lo,hi=valid[0][0],valid[-1][0];span=max(1,(hi-lo).days);peak=max(1,max(c for _,c in valid))
        path=[]
        previous=None
        for d,c in valid:
            op='M' if previous is None or (d-previous).days!=1 else 'L'
            path.append(f'{op}{8+(d-lo).days*304/span:.1f},{92-c*76/peak:.1f}');previous=d
        chart=f'<svg class="demand-chart" viewBox="0 0 320 104" role="img" aria-label="Поисковые запросы по дням"><path d="{" ".join(path)}" fill="none" stroke="currentColor" stroke-width="2"/></svg><p>{lo.isoformat()} - {hi.isoformat()} · максимум {peak} запросов в день</p>'
    if comparison:
        pct=f'{comparison["percent"]:+.1f}%' if comparison['percent'] is not None else 'процент не определён: предыдущий период равен нулю'
        info += f'<p><strong>{pct}</strong> · {comparison["older"]} → {comparison["newer"]} запросов<br>{comparison["from"]} - {comparison["to"]}: два полных периода по 7 дней (UTC) по последнюю доступную дату Wordstat.</p>'
    else: info += '<p>Для сравнения нужны 14 полных дней подряд по последнюю доступную дату Wordstat. Пропуски не считаются нулями.</p>'
    return f'<details class="analysis"><summary>Спрос и динамика</summary>{info}{chart}</details>'



DASHBOARD_CSS = '''
:root{--bg:#f7f8f6;--white:#fff;--ink:#172421;--muted:#69746e;--line:#dfe5df;--green:#125b43;--soft:#e9f5ed;--warn:#fff2df;--red:#9a403d}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:var(--green)}button,input,select{font:inherit}button,.button{cursor:pointer;display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:10px 16px;border-radius:10px;border:1px solid var(--green);background:var(--green);color:white;font-weight:650;text-decoration:none;text-align:center}button.secondary,.button.secondary{background:white;color:var(--ink);border-color:var(--line)}button:hover,.button:hover{filter:brightness(.94)}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #79b49a;outline-offset:3px}input,select{border:1px solid #cbd5cc;border-radius:9px;background:#fff;color:var(--ink);padding:10px 12px;min-height:44px;min-width:0;max-width:100%}form{margin:0}.head{max-width:1200px;margin:auto;display:flex;align-items:center;justify-content:space-between;gap:24px;padding:28px 24px 18px}.brand{font-size:30px;letter-spacing:-1px;font-weight:800;text-decoration:none;color:var(--ink)}.brand small{font-size:16px;font-weight:500;letter-spacing:0;color:var(--muted);display:block}.stamp{font-size:12px;color:var(--muted);text-align:right}.stamp strong{display:block;color:var(--ink);font-size:13px}main{max-width:1200px;margin:auto;padding:0 24px 56px}.hero{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:12px 0 22px}.hero h1{font-size:30px;letter-spacing:-.8px;line-height:1.18;margin:0 0 5px}.hero p,.section-head p{margin:0;color:var(--muted)}.hero-facts{display:flex;gap:10px;align-items:center}.hero-facts>span{font-size:12px;color:var(--muted);max-width:160px}nav{display:flex;gap:6px;background:white;border:1px solid var(--line);border-radius:13px;padding:7px;margin-bottom:24px}.tab{flex:1;display:flex;align-items:center;justify-content:center;gap:7px;padding:11px 8px;border-radius:9px;text-decoration:none;color:var(--ink);white-space:nowrap;font-weight:600}.tab.active{background:var(--green);color:#fff}.tab small{font-size:11px;opacity:.7}.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:20px;margin-bottom:18px}.section-head h2{font-size:23px;margin:0;letter-spacing:-.5px}.section-head p{max-width:540px;font-size:13px}.controls{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:20px}.controls form{display:flex;gap:8px}.controls input[type=search]{width:230px}.modes{display:flex;flex-wrap:wrap;gap:7px}.mode{display:block;padding:8px 13px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);text-decoration:none;font-size:13px}.mode.active{background:var(--green);color:white;border-color:var(--green)}.list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.entry{border:1px solid var(--line);border-radius:15px;background:#fff;padding:20px;min-width:0}.card-top{display:grid;grid-template-columns:80px minmax(0,1fr);gap:16px}.poster{width:80px;height:112px;border-radius:9px;object-fit:cover;background:#ecf0eb}.poster-placeholder{display:flex;align-items:center;justify-content:center;color:var(--green);font-size:32px;font-weight:700}.title{font-size:19px;line-height:1.3;margin:0 0 7px;overflow-wrap:anywhere}.title a{text-decoration:none;color:var(--ink)}.detail{font-size:12px;color:var(--muted);margin-bottom:9px;overflow-wrap:anywhere}.badge{display:inline-block;border-radius:6px;padding:4px 8px;font-size:12px;font-weight:600;margin:2px 4px 6px 0;background:#edf0ed;color:var(--muted)}.badge.available{background:#dff3e5;color:var(--green)}.badge.soon,.badge.warn{background:var(--warn);color:#87551d}.decline-card{border:2px solid var(--red);background:#fff5f4}.badge.fall{background:#fbe9e7;color:var(--red)}.demand{font-weight:750;font-size:21px;line-height:1.4}.demand small{font-size:12px;font-weight:400;color:var(--muted)}.trend{color:var(--green);font-size:15px;font-weight:700}.fall-text{color:var(--red)}.spark{display:block;width:100%;height:68px;margin:12px 0 4px;color:var(--green)}.spark.falling{color:var(--red)}.data-date{font-size:11px;color:var(--muted)}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}.actions form{display:inline-flex}.actions button,.actions .button{font-size:12px;padding:9px 12px}.analysis{margin-top:13px;border-top:1px solid var(--line);padding-top:10px;font-size:12px;overflow-wrap:anywhere}.analysis summary{cursor:pointer;color:var(--muted);min-height:34px;padding:5px 0}.analysis p{color:var(--muted)}.analysis .data-error{color:var(--red)}.demand-chart{width:100%;height:120px;color:var(--green)}.edit-form{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:12px}.edit-form label{display:grid;gap:4px;font-size:12px;color:var(--muted)}.edit-form .wide{grid-column:1/-1}.edit-form input,.edit-form select{width:100%}.edit-form button{justify-self:start}.watch-add{width:100%;padding:16px;border:1px solid var(--line);border-radius:12px;background:#fff;display:flex;gap:8px;flex-wrap:wrap}.watch-add label{width:100%;font-size:13px;font-weight:600}.watch-add input{flex:1;width:auto;min-width:120px}.empty{padding:32px;border:1px dashed var(--line);border-radius:12px;grid-column:1/-1;color:var(--muted);background:#fff}.empty strong{display:block;margin-bottom:6px;color:var(--ink)}.notice{font-size:12px;line-height:1.6;color:var(--muted);margin:18px 0}.status-notice{background:#fff8e9;border:1px solid #e6d8ba;border-radius:10px;padding:12px 16px}.source-list{margin-bottom:20px;border:1px solid var(--line);border-radius:12px;background:#fff;padding:16px;font-size:12px}.source-list summary{font-weight:600;cursor:pointer;min-height:30px}.source-row{display:flex;justify-content:space-between;align-items:start;gap:16px;border-top:1px solid var(--line);padding-top:9px;margin-top:9px}.source-row span{color:var(--muted);text-align:right}.radar-card .title{font-size:18px}.radar-card .reason{color:var(--green);margin:8px 0}.lock{max-width:520px;margin:12vh auto;padding:28px}.lock h1{font-size:26px}.health-line{font-size:12px;color:var(--muted);margin-top:24px}.danger{color:var(--red)!important}.pagination{display:flex;gap:9px;justify-content:center;margin-top:20px}@media(max-width:760px){.head{padding:20px 16px 12px;align-items:start}.brand{font-size:25px}.brand small{font-size:12px}.stamp{font-size:10px;max-width:125px}.stamp strong{font-size:11px}main{padding:0 16px 40px}.hero{display:block;margin-top:14px}.hero h1{font-size:26px}.hero p{font-size:13px}.hero-facts{margin-top:14px}.hero-facts>span{max-width:none}nav{flex-wrap:wrap;gap:4px;padding:5px}.tab{flex:1 0 28%;padding:10px 5px;font-size:12px}.section-head{display:block}.section-head p{margin-top:6px}.controls{display:block}.controls>form{margin-top:12px;width:100%}.controls input[type=search]{width:auto;flex:1}.modes{margin-bottom:12px}.list{grid-template-columns:1fr}.entry{padding:16px}.card-top{grid-template-columns:68px minmax(0,1fr);gap:12px}.poster{width:68px;height:96px}.title{font-size:17px}.demand{font-size:19px}.actions{gap:7px}.source-row{display:block}.source-row span{display:block;text-align:left;margin-top:3px}.watch-add input{flex-basis:55%}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
:root{--scroll-thumb:#a5b6ac;--scroll-track:#eef2ee;--scroll-hover:#788f80}html{scrollbar-gutter:stable;scrollbar-color:var(--scroll-thumb) var(--scroll-track);scrollbar-width:auto}*{scrollbar-color:var(--scroll-thumb) var(--scroll-track)}*::-webkit-scrollbar{width:12px;height:12px}*::-webkit-scrollbar-track{background:var(--scroll-track)}*::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border:3px solid var(--scroll-track);border-radius:8px}*::-webkit-scrollbar-thumb:hover{background:var(--scroll-hover)}.list{overflow-anchor:none}.watch-add #add-title{flex:1 0 100%;width:100%}.watch-add label:not([for]){display:grid;gap:5px;flex:1;min-width:120px;width:auto;font-size:12px;font-weight:400;color:var(--muted)}.watch-add label input,.watch-add label select{width:100%}.dataset-controls{width:100%;display:flex;flex-wrap:wrap;gap:10px;align-items:end}.controls .dataset-controls input[type=search]{width:100%;min-width:0}.dataset-controls label{display:grid;gap:5px;font-size:12px;color:var(--muted);min-width:135px;flex:1}.dataset-controls select,.dataset-controls input{width:100%}.dataset-query{flex:2!important}.action-status{position:fixed;bottom:calc(16px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);width:max-content;max-width:calc(100vw - 32px);padding:12px 16px;border:1px solid var(--green);border-radius:10px;background:var(--soft);color:var(--green);z-index:20}.action-status.error{background:#fff0ed;color:var(--red);border-color:var(--red)}.action-status[hidden]{display:none}.form-error{color:var(--red);font-size:12px;flex-basis:100%;margin:8px 0}button:disabled{opacity:.55;cursor:wait}button[aria-busy=true]{box-shadow:inset 0 0 0 2px #ffffff66}dialog{border:1px solid var(--line);border-radius:15px;max-width:480px;width:calc(100vw - 32px);max-height:calc(100dvh - 40px);overflow:auto;padding:24px;color:var(--ink);background:var(--white)}dialog::backdrop{background:#17242166}dialog h2{font-size:22px;margin-top:0}dialog .danger{background:var(--red);border-color:var(--red);color:white!important}.load-more{margin-top:20px}@media(max-width:760px){.dataset-controls{display:grid!important;grid-template-columns:1fr 1fr}.controls .dataset-controls input[type=search]{width:100%;min-width:0}.dataset-controls label{min-width:0}.dataset-controls .dataset-query{grid-column:1/-1}.dataset-controls button,.dataset-controls a{width:100%}}@media(forced-colors:active){*{scrollbar-color:auto}.action-status,dialog{border:1px solid CanvasText}}

'''


def short_date(value):
    if not value:return 'ещё нет данных'
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y')
    except ValueError:return str(value)[:10]


def sparkline(row):
    try:points=json.loads(row.get('dynamics_json') or '[]')
    except (ValueError,TypeError):return ''
    values=[]
    for p in points:
        try:
            d=date.fromisoformat(str(p['date'])[:10]);n=int(p['count'])
            if d<datetime.now(UTC).date() and n>=0:values.append((d,n))
        except (ValueError,TypeError,KeyError):pass
    values=sorted(dict(values).items())[-14:]
    if len(values)<2:return ''
    peak=max(1,max(n for _,n in values));span=max(1,(values[-1][0]-values[0][0]).days);commands=[];old=None
    for d,n in values:
        commands.append(('M' if old is None or (d-old).days!=1 else 'L')+f'{4+(d-values[0][0]).days*312/span:.1f},{60-n*52/peak:.1f}');old=d
    return f'<svg class="spark {"falling" if row["trend"]=="падает" else ""}" viewBox="0 0 320 68" role="img" aria-label="Динамика спроса"><path d="{" ".join(commands)}" fill="none" stroke="currentColor" stroke-width="2.5"/></svg>'


def availability_badge(store,row):
    d=detail_for(store,row);state=d['availability']
    labels={'available':'В подписке TVOЁ','soon':'Скоро'+(' · '+short_date(row['poster_date']) if row['poster_date'] else ''),'unknown':'Доступность не подтверждена','unavailable':'Недоступен в TVOЁ'}
    return f'<span class="badge {"available" if state=="available" else "soon" if state=="soon" else ""}">{html.escape(labels[state])}</span>'


def action_form(path, csrf, fields, label, secondary=False):
    hidden=''.join(f'<input type="hidden" name="{html.escape(k,quote=True)}" value="{html.escape(str(v),quote=True)}">' for k,v in fields.items())
    return f'<form novalidate action="{path}" method="post">{csrf}{hidden}<button class="{"secondary" if secondary else ""}" type="submit">{label}</button></form>'


def film_card(store,row,view,csrf,watched,promoted):
    esc=html.escape;d=detail_for(store,row);c=demand_comparison(row);is_fresh=signal_fresh(store,row,c)
    title=esc(row['title']);url=row.get('url') or ''
    url='https://tvoe.live'+url if url.startswith('/') and not url.startswith('//') else safe_url(url)
    heading=f'<a href="{esc(url,quote=True)}" target="_blank" rel="noopener noreferrer">{title}</a>' if url else title
    poster=safe_url(d.get('poster',''))
    picture=(f'<img class="poster" src="{esc(poster,quote=True)}" alt="Постер: {esc(row["title"],quote=True)}" loading="lazy" referrerpolicy="no-referrer">' if poster else f'<div class="poster poster-placeholder" aria-hidden="true">{esc(row["title"][:1].upper())}</div>')
    count=f'{row["total_count"]:,}'.replace(',',' ') if row['total_count'] is not None else 'Ожидает проверки'
    pct=(f'{c["percent"]:+.1f}%' if c and c['percent'] is not None else '')
    kind='Мультфильм' if origin_info(store,row).get('kind')=='cartoons' else 'Сериал' if row['type']=='serials' else 'Фильм'
    subtitle=kind+(' · '+d['year'] if d.get('year') else '')
    info=origin_info(store,row)
    subtitle+=' · '+', '.join(json.loads(info.get('countries_json') or '[]'))
    subtitle+=' · Добавлен '+stamp_ru(added_stamp(store,row,view))
    badges=availability_badge(store,row)
    if not tracking_enabled(store,row['id']):badges+='<span class="badge warn">Слежение остановлено</span>'
    if row['ambiguous']:badges+='<span class="badge warn">Уточните запрос</span>'
    if row['total_count'] is not None and not fresh_demand(store,row) and not row['ambiguous']:badges+='<span class="badge warn">Спрос требует обновления</span>'
    if c and not is_fresh:badges+='<span class="badge warn">Динамика устарела</span>'
    if view=='new':badges+='<span class="badge available">Опубликован · '+esc(short_date(d.get('available_at')))+' </span>'
    if row['id'] in promoted:badges+='<span class="badge">В рекламе</span>'
    change=f' <span class="trend {"fall-text" if c and c["delta"]<0 else ""}">{pct}</span>' if pct else ''
    controls=''
    if url:controls+=f'<a class="button secondary" href="{esc(url,quote=True)}" target="_blank" rel="noopener noreferrer">Открыть фильм</a>'
    controls+=action_form('/watch',csrf,{'id':row['id']},'Следить',True)
    if row['id'] in watched and view=='watch':controls+=action_form('/unwatch',csrf,{'id':row['id']},'Убрать из списка',True)
    controls+=action_form('/delete',csrf,{'id':row['id']},'Удалить',True)
    if row['id'] not in promoted:controls+=action_form('/promotion',csrf,{'id':row['id'],'active':'1'},'В рекламу')
    else:controls+=action_form('/promotion',csrf,{'id':row['id'],'active':'0'},'Убрать из рекламы',True)
    # New titles stay in the queue until moved to advertising.
    declining=view=='ads' and confirmed_decline(store,row)
    if declining:
        controls+='<span class="badge fall">Подтверждённый спад · рассмотреть снятие с рекламы</span>'
    edit=(f'<details class="analysis"><summary>Уточнить фильм и доступность</summary><form novalidate class="edit-form" action="/film" method="post">{csrf}<input type="hidden" name="id" value="{esc(row["id"],quote=True)}"><input type="hidden" name="return" value="{view}">'
          f'<label>Тип<select name="type" aria-label="Тип"><option value="films" {"selected" if kind=="Фильм" else ""}>Фильм</option><option value="serials" {"selected" if kind=="Сериал" else ""}>Сериал</option><option value="cartoons" {"selected" if kind=="Мультфильм" else ""}>Мультфильм</option></select></label>'
          f'<label>Год<input name="year" inputmode="numeric" maxlength="4" value="{esc(d.get("year") or "",quote=True)}" placeholder="Например, 2026"></label>'
          '<label class="wide">Доступность в TVOЁ<select name="availability" aria-label="Доступность в TVOЁ">'+''.join(f'<option value="{key}" {"selected" if d["availability"]==key else ""}>{label}</option>' for key,label in [('unknown','Не подтверждена'),('available','Доступен в подписке'),('soon','Скоро'),('unavailable','Недоступен')])+'</select></label>'
          f'<label class="wide">Подтверждённый запрос Wordstat<input name="query" maxlength="180" value="{esc(d.get("query_override") or "",quote=True)}" placeholder="Название + фильм / сериал / год"></label>'
          '<p class="notice wide">Подтверждайте доступность после проверки карточки TVOЁ. Уточнение запроса сохраняет старую историю отдельно.</p><button type="submit">Сохранить</button></form></details>')
    date_info=(f'Последние данные · {short_date(c["to"])} · два полных периода по 7 дней' if c else 'Динамика ещё не получена')
    return (f'<article data-entry-id="{esc(row['id'],quote=True)}" data-title="{esc(row['title'],quote=True)}" class="entry {"decline-card" if declining else ""}"><div class="card-top">{picture}<div><h3 class="title">{heading}</h3><div class="detail">{esc(subtitle)}</div>{badges}<div class="demand">{count}<small>{" запросов / 30 дней" if row["total_count"] is not None else ""}</small>{change}</div><div class="detail">Wordstat проверен · {esc(short_date(row["measured_at"]))}</div></div></div>'
            +sparkline(row)+f'<div class="data-date">{esc(date_info)}</div><div class="actions">{controls}</div>'+title_analysis(store,row,csrf)+edit+'</article>')


def source_status(store):
    with store.lock:sources=[dict(r) for r in store.db.execute('SELECT * FROM radar_sources ORDER BY id')]
    rows=[]
    for source in sources:
        text=('Временно недоступен' if source['error'] else 'Проверка ещё не выполнена' if not source['success_at'] else 'Получены данные · '+short_date(source['success_at']))
        rows.append(f'<div class="source-row"><a href="{html.escape(source["url"],quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(source["name"])}</a><span>{text}</span></div>')
    return '<details class="source-list" open><summary>Источники и обновление</summary>'+''.join(rows)+'</details>'


def radar_card(store,row,csrf):
    esc=html.escape
    confirmed=row.get('confirmed_id')
    state=('Название отслеживается · спрос проверяет Wordstat' if confirmed else 'Название найдено в каталоге · проверьте совпадение' if row.get('match_id') else 'Совпадение требует проверки')
    film_id=confirmed or row.get('match_id')
    if film_id and not tracking_enabled(store,film_id):state='Слежение остановлено · нажмите «Следить» для возобновления'
    fields={'active':'1','radar_id':row['id']}
    fields.update({'id':film_id} if film_id else {'title':row['title']})
    promote=action_form('/promotion',csrf,fields,'В рекламу')
    follow_fields={k:v for k,v in fields.items() if k!='active'}
    promote+=action_form('/watch',csrf,follow_fields,'Следить',True)
    promote+=action_form('/delete',csrf,follow_fields,'Удалить',True)
    demand=''
    if film_id:
        tracked=[r for r in store.tracking_rows() if r['id']==film_id]
        if tracked:
            r=tracked[0];n=r['total_count']
            demand='<p class="demand">'+(f'{n:,}'.replace(',',' ')+' <small>запросов / 30 дней</small>' if n is not None else 'Ожидает Wordstat')+'</p>'
    if film_id and confirmed:
        countries=json.loads(origin_info(store,film_id).get('countries_json') or '[]')
        state+=' · '+', '.join(countries)
    form='' if confirmed else (f'<details class="analysis"><summary>Подтвердить фильм</summary><form novalidate class="edit-form" action="/radar-confirm" method="post">{csrf}<input type="hidden" name="id" value="{esc(row["id"],quote=True)}">'
          f'<label class="wide">Название<input name="title" maxlength="160" required value="{esc(row["title"],quote=True)}"></label><label>Год<input name="year" maxlength="4" inputmode="numeric"></label><label>Тип<select name="type" aria-label="Тип"><option value="films">Фильм</option><option value="serials">Сериал</option><option value="cartoons">Мультфильм</option></select></label><button class="wide" type="submit">Подтвердить и следить</button></form></details>')
    return (f'<article data-entry-id="radar:{esc(row['id'],quote=True)}" data-title="{esc(row['title'],quote=True)}" class="entry radar-card"><div class="detail">{esc(row["source_name"])} · {esc(short_date(row["published_at"] or row["found_at"]))}</div><h3 class="title">{esc(row["title"])}</h3><p class="reason">{esc(row["reason"] or "Упоминание")}</p><span class="badge {"available" if confirmed else "warn"}">{state}</span><div class="actions"><a class="button secondary" href="{esc(row["url"],quote=True)}" target="_blank" rel="noopener noreferrer">Открыть источник</a>'+action_form('/radar-dismiss',csrf,{'id':row['id']},'Скрыть',True)+promote+'</div>'+demand+form+'</article>')




DASHBOARD_SCRIPT = r'''
(() => {
  if (window.tvoeDashboardReady) return;
  window.tvoeDashboardReady = true;
  let listRequest = 0, listController, mutationBusy = false, clearTimer, sessionExpired=false;
  const toast = (message, error=false) => {
    const region=document.getElementById('action-status');
    region.textContent=message;
    region.classList.toggle('error',error);
    region.hidden=false;
    clearTimeout(clearTimer);
    if (!error) clearTimer=setTimeout(()=>{region.hidden=true;},6500);
  };
  const snapshot = () => ({y:window.scrollY,focus:{name:document.activeElement?.getAttribute('name'),card:document.activeElement?.closest('[data-entry-id]')?.dataset.entryId,action:document.activeElement?.closest('form')?.getAttribute('action'),label:document.activeElement?.tagName==='BUTTON'?document.activeElement.textContent.trim():null},cards:[...document.querySelectorAll('[data-entry-id]')].map(el=>({id:el.dataset.entryId,y:el.getBoundingClientRect().top,bottom:el.getBoundingClientRect().bottom})).filter(r=>r.bottom>0)});
  const restore = saved => {
    const cards=[...document.querySelectorAll('[data-entry-id]')];
    const anchor=saved.cards.find(r=>cards.some(c=>c.dataset.entryId===r.id));
    if (anchor) {
      const el=cards.find(c=>c.dataset.entryId===anchor.id);
      window.scrollBy(0,el.getBoundingClientRect().top-anchor.y);
    } else window.scrollTo(0,saved.y);
    const f=saved.focus||{};
    let scope=f.card?cards.find(c=>c.dataset.entryId===f.card):document.querySelector('main');
    let target=f.name?scope?.querySelector('[name="'+CSS.escape(f.name)+'"]'):null;
    if(!target && f.label)target=[...(scope?.querySelectorAll('button')||[])].find(b=>b.textContent.trim()===f.label && (!f.action || b.closest('form')?.getAttribute('action')===f.action));
    if(!target && f.card)target=cards.find(c=>c.getBoundingClientRect().bottom>0)?.querySelector('button');
    target?.focus({preventScroll:true});
  };
  async function refreshView(url=location.href,save=snapshot(),push=false) {
    const version=++listRequest;
    document.querySelector('main')?.setAttribute('aria-busy','true');
    listController?.abort();listController=new AbortController();
    const controller=listController;
    const timer=setTimeout(()=>controller.abort(),15000);
    try {
      const response=await fetch(url,{signal:controller.signal,credentials:'same-origin',cache:'no-store'});
      if (!response.ok) throw new Error('Не удалось обновить список. Повторите действие.');
      const doc=new DOMParser().parseFromString(await response.text(),'text/html');
      if (version!==listRequest) return;
      const main=doc.querySelector('main');
      if (!main || main.classList.contains('lock')) {sessionExpired=true;throw new Error('Сессия завершилась. Откройте /web в боте заново.');}
      document.querySelector('main').replaceWith(main);
      document.title=doc.title;
      if (push) history.pushState({},'',url);
      restore(save);
    } finally {clearTimeout(timer);if(version===listRequest)document.querySelector('main')?.removeAttribute('aria-busy');}
  }
  function formError(form,text) {
    let error=form.querySelector('.form-error');
    if (!error) {error=document.createElement('p');error.className='form-error';error.setAttribute('role','alert');form.append(error);}
    error.textContent=text;
  }
  function validForm(form) {
    for(const field of form.querySelectorAll('input,select')) {
      field.removeAttribute('aria-invalid');
      if (!field.validity.valid) {
        field.setAttribute('aria-invalid','true');
        formError(form,'Проверьте заполнение поля «'+(field.getAttribute('aria-label')||field.labels?.[0]?.textContent.trim()||'Название')+'».');
        field.focus({preventScroll:true});return false;
      }
    }
    return true;
  }
  const dialog=document.getElementById('delete-dialog');
  const confirmDelete=form=>new Promise(resolve=>{
    const trigger=document.activeElement;
    const title=form.closest('[data-title]')?.dataset.title||form.querySelector('[name=title]')?.value||'Тайтл';
    dialog.querySelector('[data-delete-name]').textContent=title;
    dialog.returnValue='cancel';
    dialog.onclose=()=>{trigger?.focus({preventScroll:true});resolve(dialog.returnValue==='delete');};
    dialog.showModal();dialog.querySelector('[data-delete-cancel]').focus({preventScroll:true});
  });
  document.addEventListener('submit',async event=>{
    const form=event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if(form.method.toLowerCase()==='dialog')return;
    event.preventDefault();
    if (!validForm(form)) return;
    if (mutationBusy) {toast('Дождитесь завершения текущего действия.');return;}
    if (form.method.toLowerCase()==='get') {
      const url=new URL(form.action);url.search=new URLSearchParams(new FormData(form)).toString();
      try {await refreshView(url.href,snapshot(),true);}catch(error){if(error.name!=='AbortError')toast(error.message,true);}
      return;
    }
    if (new URL(form.action).pathname==='/delete' && !(await confirmDelete(form))) return;
    mutationBusy=true;
    const saved=snapshot(),button=event.submitter;
    form.setAttribute('aria-busy','true');
    const buttons=[...document.querySelectorAll('button[type=submit],form button:not([type])')];
    buttons.forEach(el=>el.disabled=true);
    button?.setAttribute('aria-busy','true');
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
    try {
      const data=new FormData(form);
      const multipart=form.enctype==='multipart/form-data';
      const response=await fetch(form.action,{method:'POST',credentials:'same-origin',headers:{'X-TVOE-UI':'async',...(multipart?{}:{'Content-Type':'application/x-www-form-urlencoded'})},body:multipart?data:new URLSearchParams(data),signal:controller.signal});
      if (!response.ok) {
        const text=new DOMParser().parseFromString(await response.text(),'text/html').body.textContent.trim();
        throw new Error(response.status===403?'Сессия завершилась. Откройте /web в боте заново.':text||'Изменение не сохранено. Повторите действие.');
      }
      const result=await response.json();
      try {await refreshView(location.href,saved);}catch(error){toast('Изменение сохранено. Не удалось обновить список - обновите страницу.',true);return;}
      toast(result.message||'Изменение сохранено.');
    }catch(error) {
      const text=error.name==='AbortError'?'Ответ не получен. Обновите список перед повтором.':error.message;
      formError(form,text);toast(text,true);
    }finally{
      clearTimeout(timer);mutationBusy=false;form.removeAttribute('aria-busy');button?.removeAttribute('aria-busy');buttons.forEach(el=>el.disabled=false);
    }
  });
  document.addEventListener('click',async event=>{
    const clear=event.target.closest('[data-clear-search]');
    if(clear) {
      event.preventDefault();
      const form=clear.closest('form'),field=form.querySelector('[name=q]');field.value='';field.focus({preventScroll:true});form.requestSubmit();return;
    }
    const link=event.target.closest('a.mode,[data-load-more],[data-filter-reset]');
    if(!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button!==0)return;
    event.preventDefault();
    try {await refreshView(link.href,snapshot(),true);}catch(error){if(error.name!=='AbortError')toast(error.message,true);}
  });
  document.addEventListener('change',event=>{
    if(event.target.matches('.dataset-controls select'))event.target.form.requestSubmit();
  });
  document.addEventListener('keydown',event=>{
    if(event.key==='Enter' && event.isComposing && event.target.matches('input'))event.preventDefault();
  });
  const hasDrafts=()=>[...document.querySelectorAll('form input:not([type=hidden]),form select')].some(e=>{
    if(e.type==='file')return e.files.length>0;
    if(e.tagName==='SELECT'){const initial=[...e.options].find(o=>o.defaultSelected)||e.options[0];return e.value!==initial?.value;}
    return e.value!==e.defaultValue;
  });
  setInterval(()=>{
    if(document.hidden || sessionExpired || mutationBusy || dialog.open || hasDrafts() || document.querySelector('main')?.getAttribute('aria-busy')==='true' || document.activeElement?.matches('input,select'))return;
    refreshView(location.href,snapshot()).catch(error=>{if(error.name!=='AbortError')toast('Список не обновился. Сохранённые данные остаются на экране.',true);});
  },60000);
  window.addEventListener('popstate',()=>refreshView(location.href,snapshot()).catch(error=>toast(error.message,true)));
})();
'''

def interaction_shell(nonce):
    return ('<div id="action-status" class="action-status" role="status" aria-live="polite" hidden></div>'
            '<dialog id="delete-dialog" aria-labelledby="delete-title"><h2 id="delete-title">Удалить тайтл?</h2><p data-delete-name></p><p>Тайтл исчезнет со всех вкладок, проверки остановятся. История сохранится. При необходимости можно добавить его вручную заново.</p><form method="dialog" novalidate class="actions"><button type="submit" value="cancel" class="secondary" data-action="delete-cancel" data-delete-cancel>Отмена</button><button type="submit" value="delete" class="danger" data-action="delete-confirm" data-delete-confirm>Удалить</button></form></dialog>'
            '<script nonce="'+nonce+'">'+DASHBOARD_SCRIPT+'</script>')

def added_stamp(store,row,view):
    if view=='radar':return row.get('found_at') or ''
    table={'watch':('watchlist','added_at'),'new':('review_queue','added_at'),'ads':('promotions','updated_at')}.get(view)
    if table:
        with store.lock:r=store.db.execute('SELECT '+table[1]+' FROM '+table[0]+' WHERE title_id=?',(row['id'],)).fetchone()
        if r:return r[0] or ''
    return row.get('first_seen') or ''


def stamp_ru(stamp):
    try:return datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')
    except (ValueError,AttributeError):return 'не указана'


def dataset_rows(store,view,query='',mode='all',sort='added',kind='all',soon='all'):
    promoted={r['id'] for r in promotion_rows(store)}
    watched={r['id'] for r in store.watch_rows()}
    by_view={
        'catalog':[r for r in store.rows('title ASC','status!="watched"',10000) if r['id'] not in watched],
        'new':store.new_rows(),
        'signals':growth_rows(store,mode=='early'),
        'watch':store.watch_rows(),
        'ads':promotion_rows(store),
        'radar':radar_rows(store),
    }
    result={}
    for name,rows in by_view.items():
        ready=[]
        for r in rows:
            identity=radar_identity(store,r) if name=='radar' else r
            if not foreign_title(store,identity):continue
            if name!='ads' and identity['id'] in promoted:continue
            if name=='radar' and identity['id'].startswith('radar:') and deleted_name(store,r['title']):continue
            info=origin_info(store,identity)
            ready.append({**r,'display_kind':info.get('kind') or identity.get('type') or 'films','added_stamp':added_stamp(store,r,name)})
        result[name]=ready
    rows=result.get(view,[])
    if query:rows=[r for r in rows if query.casefold() in r['title'].casefold()]
    if kind in ('films','serials','cartoons'):rows=[r for r in rows if r['display_kind']==kind]
    def identity(r):return radar_identity(store,r) if view=='radar' else r
    def actual(r):
        ident=identity(r)
        if not ident['id'].startswith('radar:'):return ident
        return {**ident,'poster_date':None,'status':'watched','total_count':None,'dynamics_json':None}
    if mode in ('available','soon','unknown') and view=='catalog':rows=[r for r in rows if detail_for(store,actual(r))['availability']==mode]
    if mode=='growth' and view=='catalog':rows=[r for r in rows if signal_fresh(store,actual(r)) and actual(r).get('trend')=='растёт']
    if soon=='soon':rows=[r for r in rows if detail_for(store,actual(r))['availability']=='soon']
    if soon=='available':rows=[r for r in rows if detail_for(store,actual(r))['availability']=='available']
    if sort=='soon':rows=sorted(rows,key=lambda r:(poster_sort(actual(r).get('poster_date')),r['title'].casefold()))
    elif sort=='growth':rows=sorted(rows,key=lambda r:((demand_comparison(actual(r)) or {}).get('percent') is not None,(demand_comparison(actual(r)) or {}).get('percent') or 0,r['title']),reverse=True)
    elif sort=='demand' or mode=='top':rows=sorted(rows,key=lambda r:(actual(r).get('total_count') or 0,r['title']),reverse=True)
    elif sort=='title':rows=sorted(rows,key=lambda r:r['title'].casefold())
    else:
        # SQLite insertion order breaks ties when several additions share a second.
        with store.lock:order={r['title_id']:r['rowid'] for r in store.db.execute('SELECT rowid,title_id FROM watchlist')}
        rows=sorted(rows,key=lambda r:(r.get('added_stamp',''),order.get(r['id'],0),r['id']),reverse=True)
    return rows,result


def dataset_controls(view,query,mode,sort,kind,soon):
    esc=html.escape
    def select(name,label,value,options):
        return '<label>'+label+'<select name="'+name+'" aria-label="'+label+'">'+''.join('<option value="'+key+'" '+('selected' if key==value else '')+'>'+text+'</option>' for key,text in options)+'</select></label>'
    return ('<form novalidate class="dataset-controls" action="/" method="get" novalidate><input type="hidden" name="view" value="'+view+'"><input type="hidden" name="mode" value="'+mode+'">'
        +select('sort','Сортировка',sort,[('added','Сначала новые'),('soon','По дате появления'),('growth','По росту спроса'),('demand','По количеству запросов'),('title','По названию')])
        +select('kind','Тип',kind,[('all','Все типы'),('films','Фильмы'),('serials','Сериалы'),('cartoons','Мультфильмы')])
        +select('soon','Доступность',soon,[('all','Любая'),('soon','Скоро в TVOЁ'),('available','Есть в TVOЁ')])
        +'<label class="dataset-query">Название<input type="search" name="q" value="'+esc(query,quote=True)+'" placeholder="Поиск по названию" aria-label="Поиск по названию"></label><button type="submit" name="clear" value="1" class="secondary" data-clear-search aria-label="Очистить поиск">×</button><button type="submit" class="secondary">Показать</button><a class="button secondary" data-filter-reset href="/?view='+view+'">Сбросить</a></form>')

def dashboard_document(store, view, query, nonce, mode='all', csrf='', sort='added',kind='all',soon='all',limit=100):
    esc=html.escape;csrf_field=f'<input type="hidden" name="csrf" value="{esc(csrf,quote=True)}">'
    legacy={'soon':('catalog','soon'),'top':('catalog','top'),'growth':('catalog','growth')}
    view,mode=legacy.get(view,(view,mode));view=view if view in ('catalog','new','signals','watch','radar','ads') else 'catalog'
    if mode=='date':mode='all'
    rows,by_view=dataset_rows(store,view,query,mode,sort,kind,soon)
    all_rows=by_view['catalog'];new_rows=by_view['new'];watch_rows=by_view['watch'];ad_rows=by_view['ads'];signals=by_view['signals'];radar=by_view['radar']
    watched={r['id'] for r in store.watch_rows()};promoted={r['id'] for r in promotion_rows(store)}
    tabs=[('catalog','Каталог',len(all_rows)),('new','Новое',len(new_rows)),('signals','Сигналы',len(signals)),('watch','Следить',len(watch_rows)),('radar','Радар',len(radar)),('ads','Реклама',len(ad_rows))]
    links=''.join(f'<a class="tab {"active" if view==k else ""}" href="/?view={k}" {"aria-current=page" if view==k else ""}>{label}<small>{count}</small></a>' for k,label,count in tabs)
    headings={'catalog':'Каталог зарубежных тайтлов','new':'Новое в подписке','signals':'Сигналы спроса','watch':'Отслеживаемые фильмы и сериалы','radar':'Радар зарубежного контента','ads':'Фильмы и сериалы в рекламе'}
    descriptions={'catalog':'Только подтверждённо зарубежные произведения. Страна проверяется автоматически.','new':'Доступны в TVOЁ. Продолжаем проверять спрос до переноса в рекламу.','signals':'Спрос выше 50 000. Для зарубежных тайтлов из Радара также учитывается подтверждённый рост.','watch':'Последние добавленные показываются первыми. Российские и неопределённые тайтлы скрыты.','radar':'Зарубежные названия из публикаций. Wordstat проверяет спрос, а не число упоминаний.','ads':'Отмеченные вами зарубежные тайтлы. Предупреждения о подтверждённом падении спроса.'}
    filters=[('all','Все'),('available','Доступен'),('soon','Скоро'),('unknown','Не подтверждены'),('top','По спросу'),('growth','Растут')] if view=='catalog' else [('all','Все сигналы'),('early','Начинают расти')] if view=='signals' else []
    from urllib.parse import urlencode
    controls='<div class="modes">'+''.join(f'<a class="mode {"active" if mode==key else ""}" href="/?'+esc(urlencode({'view':view,'mode':key,'sort':sort,'kind':kind,'soon':soon,'q':query}),quote=True)+f'">{label}</a>' for key,label in filters)+'</div>' if filters else ''
    search=dataset_controls(view,query,mode,sort,kind,soon)
    add=''
    if view in ('watch','ads'):
        path='/watch' if view=='watch' else '/promotion'
        label='Добавить фильм или сериал' if view=='watch' else 'Добавить тайтл в рекламу'
        button='Следить' if view=='watch' else 'В рекламу'
        add=f'<form novalidate class="watch-add" action="{path}" method="post">{csrf_field}<input type="hidden" name="active" value="1"><label for="add-title">{label}</label><input id="add-title" name="title" maxlength="160" required placeholder="Введите название"><label>Год<input name="year" inputmode="numeric" maxlength="4" placeholder="Если известен"></label><label>Тип<select name="type" aria-label="Тип добавляемого тайтла"><option value="films">Фильм</option><option value="serials">Сериал</option><option value="cartoons">Мультфильм</option></select></label><button type="submit">{button}</button></form>'
    entries=''.join(radar_card(store,row,csrf_field) if view=='radar' else film_card(store,row,view,csrf_field,watched,promoted) for row in rows[:limit])
    empty={'catalog':'Каталог пока пуст. Коллекция TVOЁ обновляется автоматически.','new':'Нет опубликованных зарубежных тайтлов, ожидающих проверки. Будущие премьеры здесь не отображаются.','signals':'Нет отслеживаемых тайтлов со спросом выше 50 000 по выбранному фильтру. Тайтлы в рекламе и с остановленным слежением сюда не входят.','watch':'Введите название и нажмите «Следить». Проверка начнётся в фоне в пределах лимитов.','radar':'Подходящих публикаций пока нет. Состояние каждого источника указано выше.','ads':'Отметьте тайтл кнопкой «В рекламу» или добавьте название выше.'}
    if not entries:entries=f'<div class="empty"><strong>{"Ничего не найдено" if query else "Пока нет тайтлов"}</strong>{"Попробуйте другое название." if query else empty[view]}</div>'
    notices=''
    if view=='catalog' and not store.meta('catalog_feed_updated',''):notices='<p class="notice status-notice">Полный каталог TVOЁ пока не подключён. Сохраняем тайтлы из «Скоро», даже когда они исчезают из раздела. Доступность подтверждается отдельно.</p>'
    if view=='catalog':
        notices+=(f'<details class="analysis source-list"><summary>Загрузить фид каталога TVOЁ</summary><form novalidate class="watch-add" action="/catalog-feed" method="post" enctype="multipart/form-data">{csrf_field}<label for="catalog-feed">Файл каталога TVOЁ · YML/XML</label><input id="catalog-feed" name="feed" type="file" accept=".xml,.yml,text/xml" required><button type="submit">Загрузить каталог</button></form><p class="notice">Первый импорт создаёт базовый каталог без массовых уведомлений. В дальнейшем новые доступные тайтлы попадают в «Новое». Используйте фид TVOЁ, где available означает фактическую доступность для просмотра.</p></details>')
        if store.meta('catalog_feed_error',''):notices+='<p class="notice status-notice">Не удалось обновить фид каталога. Последние данные сохранены.</p>'
    if view=='ads':notices+='<p class="notice">Предупреждение: падение не менее 30% на двух разных последовательных датах данных. Повторная загрузка одной даты не считается подтверждением. Даже при спросе ниже 50 000. Реклама автоматически не отключается.</p>'
    if view=='radar':notices+=source_status(store)+'<p class="notice">Читаем доступные публичные ленты, без входа в закрытые каналы и без обхода защиты. Страны: Wikidata. Новые названия проходят проверку идентичности перед Wordstat.</p>'
    queued_top=int(store.meta('wordstat_top_queue','0'));queued_dyn=int(store.meta('wordstat_dynamics_queue','0'))
    if queued_top or queued_dyn:notices+=f'<p class="notice status-notice">В очереди Wordstat: спрос - {queued_top}, динамика - {queued_dyn}. Проверки продолжаются автоматически в пределах суточных лимитов.</p>'
    pending_origin=sum(origin_info(store,r)['status']=='unknown' for r in store.rows('title ASC','1=1',10000))
    notices+=f'<p class="notice status-notice">Только зарубежные тайтлы. Страна уточняется у {pending_origin} записей - до подтверждения они скрыты. Россия и совместное производство с Россией исключены.</p>'
    if store.meta('origin_error',''):notices+='<p class="notice status-notice">Источник стран временно недоступен. Неопределённые тайтлы скрыты; ранее проверенные сохранены.</p>'
    if store.meta('last_errors',''):notices+='<p class="notice status-notice">Часть данных не обновилась. Последние сохранённые значения доступны в карточках.</p>'
    updated=esc(short_date(store.meta('last_tvoe','')))
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{headings[view]} · TVOЁ</title><style nonce="{nonce}">{DASHBOARD_CSS}</style></head><body><header><div class="head"><a class="brand" href="/">TVOЁ<small>Аналитика спроса</small></a><div class="stamp">Коллекция проверена<strong>{updated}</strong></div></div></header><main><section class="hero"><div><h1>Спрос на фильмы и сериалы</h1><p>Спрос в России, публикации и ваши рекламные решения.</p></div><div class="hero-facts"><span>TVOЁ каждый час<br>Wordstat: кеш 24 часа<br>Новые - в ближайшем цикле</span><form novalidate action="/refresh" method="post">{csrf_field}<button type="submit">↻ Обновить</button></form></div></section><nav aria-label="Разделы">{links}</nav><div class="section-head"><h2>{headings[view]}</h2><p>{descriptions[view]}</p></div>{add}<div class="controls">{controls}{search}</div>{notices}<div class="list">{entries}</div>{('<a class="button secondary load-more" data-load-more href="/?'+esc(urlencode({'view':view,'q':query,'mode':mode,'sort':sort,'kind':kind,'soon':soon,'limit':min(10000,limit+100)}),quote=True)+'">Показать ещё</a>') if len(rows)>limit else ''}<p class="health-line">Wordstat показывает поисковые запросы, а не просмотры фильма. Решение о рекламе принимаете вы.</p></main>{interaction_shell(nonce)}</body></html>'''


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
            if path in ('/refresh','/review','/ad','/watch','/unwatch','/promotion','/film','/radar-confirm','/radar-dismiss','/catalog-feed','/delete'):
                if not self.authorized():
                    return self.respond(403)
                try:
                    size = int(self.headers.get('Content-Length','0'))
                    if size < 0 or size > (8_100_000 if path=='/catalog-feed' else 16384 if path == '/ad' else 4096):
                        return self.respond(413)
                    raw=self.rfile.read(size)
                    if path=='/catalog-feed':
                        from email.parser import BytesParser
                        from email.policy import default
                        content_type=self.headers.get('Content-Type','')
                        if not content_type.startswith('multipart/form-data;'):return self.respond(400)
                        message=BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                        fields={}
                        for part in message.iter_parts():
                            name=part.get_param('name',header='content-disposition')
                            if name in ('csrf','feed'):fields[name]=[(part.get_payload(decode=True) or b'').decode('utf-8-sig')]
                    else:
                        fields = parse_qs(raw.decode('utf-8'))
                except (ValueError,UnicodeError):
                    return self.respond(400)
                if not hmac.compare_digest(fields.get('csrf',[''])[0], self.csrf_token()):
                    return self.respond(403)
                if path in ('/promotion','/film','/radar-confirm','/radar-dismiss','/unwatch','/catalog-feed','/delete'):
                    fields={k:v[0] for k,v in fields.items()}
                    ident=fields.get('id','')
                    refresh_id=None
                    try:
                        if path=='/delete':
                            if not ident:ident,_=store.add_watch(fields.get('title',''))
                            pause_tracking(store,ident,deleted=True)
                            target='/?saved=deleted'
                        elif path=='/catalog-feed':
                            try:import_catalog_feed(store,fields.get('feed',''))
                            except ET.ParseError:raise ValueError('Не удалось прочитать XML. Проверьте файл.')
                            store.set_meta('catalog_feed_error','')
                            if app is not None:
                                try:app.send_new_alerts()
                                except RuntimeError:pass
                                app.refresh_async(notify=False)
                            target='/?view=catalog&saved=feed'
                        elif path=='/promotion':
                            active=fields.get('active')
                            if active not in ('0','1'): raise ValueError('Выберите действие.')
                            if not ident:
                                ident,added=add_manual_title(store,fields.get('title',''),fields.get('type','films'),fields.get('year',''))
                                refresh_id=ident if added else None
                            radar_id=fields.get('radar_id','')
                            if radar_id:
                                with store.lock,store.db:
                                    radar=store.db.execute('SELECT title FROM radar_items WHERE id=?',(radar_id,)).fetchone()
                                    if not radar:raise ValueError('Публикация не найдена.')
                                    store.db.execute('UPDATE radar_items SET confirmed_id=? WHERE id=?',(ident,radar_id))
                                    radar_kind=store.meta('radar_kind:'+radar_id,'')
                                    if radar_kind in ('films','serials'):store.db.execute('UPDATE titles SET type=? WHERE id=? AND status="watched"',(radar_kind,ident))
                            set_promotion(store,ident,active=='1')
                            if active=='1': refresh_id=ident
                            target='/?view=ads&saved=promotion'
                        elif path=='/unwatch':
                            with store.lock,store.db:
                                store.db.execute('DELETE FROM watchlist WHERE title_id=?',(ident,))
                            target='/?view=watch&saved=unwatch'
                        elif path=='/film':
                            with store.lock,store.db:
                                row=store.db.execute('SELECT * FROM titles WHERE id=?',(ident,)).fetchone()
                                if not row: raise ValueError('Фильм не найден.')
                                kind=fields.get('type','films');query=fields.get('query','').strip()
                                if kind not in ('films','serials','cartoons') or len(query)>180 or any(ord(c)<32 for c in query):raise ValueError('Проверьте тип и запрос.')
                                old=detail_for(store,dict(row)).get('query_override') or ''
                                set_availability(store,ident,fields.get('availability','unknown'),year=fields.get('year','').strip())
                                if fields.get('availability')=='available' and not row['measured_at']:refresh_id=ident
                                store.db.execute('UPDATE titles SET type=? WHERE id=?',(kind,ident))
                                store.db.execute('UPDATE film_details SET query_override=? WHERE title_id=?',(query,ident))
                                if old!=query or row['type']!=kind:
                                    store.db.execute('UPDATE titles SET measured_at=NULL,total_count=NULL,dynamics_json=NULL,trend=NULL,delta=NULL,ambiguous=0 WHERE id=?',(ident,))
                                    store.set_meta('dynamics_checked:'+ident,'')
                                    store.set_meta('growth_alert:'+ident,'quiet')
                                    store.set_meta('radar_alert:'+ident,'quiet')
                                    store.db.execute('DELETE FROM decline_state WHERE title_id=?',(ident,))
                                    refresh_id=ident
                            if app is not None:
                                try: app.send_new_alerts()
                                except RuntimeError: pass
                            view=fields.get('return','catalog')
                            if view not in ('catalog','new','signals','watch','ads'):view='catalog'
                            target='/?view='+view+'&saved=film'
                        elif path=='/radar-confirm':
                            refresh_id,added=confirm_radar(store,ident,fields.get('title',''),fields.get('year','').strip(),fields.get('type','films'))
                            target='/?view=watch&saved=radar'
                        else:
                            with store.lock,store.db:store.db.execute('UPDATE radar_items SET dismissed=1 WHERE id=?',(ident,))
                            target='/?view=radar&saved=hidden'
                        if refresh_id and app is not None:app.refresh_async(notify=False,watch_id=refresh_id)
                    except (ValueError,OverflowError) as exc:
                        return self.respond(400,('<!doctype html><html lang="ru"><meta name="viewport" content="width=device-width,initial-scale=1"><p>'+html.escape(str(exc))+'</p><p><a href="/">Вернуться к списку</a></p></html>').encode())
                elif path == '/ad':
                    try:
                        store.save_ad(fields.get('id',[''])[0], {k:v[0] for k,v in fields.items()})
                    except (ValueError,OverflowError):
                        return self.respond(400,'Некорректные данные. Проверьте период и неотрицательные показатели; вернитесь назад.'.encode())
                    target = '/'
                elif path == '/watch':
                    try:
                        if not fields.get('id'):
                            ident,added=add_manual_title(store,fields.get('title',[''])[0],fields.get('type',['films'])[0],fields.get('year',[''])[0])
                        else:ident,added=store.add_watch(ident=fields['id'][0])
                        with store.lock,store.db:store.db.execute('UPDATE watchlist SET added_at=? WHERE title_id=?',(now(),ident))
                        radar_id=fields.get('radar_id',[''])[0]
                        if radar_id:
                            with store.lock,store.db:
                                store.db.execute('UPDATE radar_items SET confirmed_id=? WHERE id=?',(ident,radar_id))
                                kind=store.meta('radar_kind:'+radar_id,'')
                                if kind in ('films','serials'):store.db.execute('UPDATE titles SET type=? WHERE id=? AND status="watched"',(kind,ident))
                    except ValueError as exc:
                        return self.respond(400,html.escape(str(exc)).encode())
                    if app is not None and added:
                        app.refresh_async(notify=False,watch_id=ident)
                    target = '/?view=watch&watch=saved'
                elif path == '/review':
                    store.mark_reviewed(fields.get('id',[''])[0])
                    target = '/?view=new'
                else:
                    if app is None:
                        return self.respond(503)
                    app.refresh_async()
                    target = '/?refresh=started'
                if self.headers.get('X-TVOE-UI')=='async':
                    messages={'/watch':'Добавлено в «Следить». Страна и спрос проверяются автоматически.','/delete':'Тайтл удалён со всех вкладок. Проверки остановлены.','/promotion':'Рекламный статус сохранён.','/film':'Данные тайтла сохранены.','/refresh':'Обновление запущено.','/radar-confirm':'Тайтл принят на отслеживание.','/radar-dismiss':'Публикация скрыта.','/unwatch':'Тайтл убран из списка «Следить».','/catalog-feed':'Каталог загружен. Страны уточняются автоматически.'}
                    return self.respond(200,json.dumps({'ok':True,'message':messages.get(path,'Изменения сохранены.')},ensure_ascii=False).encode(),'application/json; charset=utf-8')
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
                query = '' if params.get('clear')==['1'] else params.get('q',[''])[0][:100].strip()
                sort=params.get('sort',['added'])[0];kind=params.get('kind',['all'])[0];soon=params.get('soon',['all'])[0]
                try:limit=max(100,min(10000,int(params.get('limit',['100'])[0])))
                except ValueError:limit=100
                with store.lock:document = dashboard_document(store,view,query,nonce,mode,self.csrf_token(),sort,kind,soon,limit)
                if params.get('saved'):
                    document=document.replace('<main>','<main><p class="notice status-notice" role="status">Изменения сохранены.</p>',1)
                if params.get('watch') == ['saved']:
                    document = document.replace('<main>', '<main><p role="status">Фильм сохранён. Проверка Wordstat выполняется в фоне в пределах лимитов. Обновите страницу через некоторое время, чтобы увидеть результат.</p>', 1)
                if params.get('refresh') == ['started']:
                    document = document.replace('<main>', '<main><p class="notice status-notice" role="status">Обновление запрошено. Результат придёт в Telegram. После завершения перезагрузите страницу.</p>', 1)
                body = document.encode('utf-8')
            else:
                body = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Вход · TVOЁ</title><style nonce="{nonce}">{DASHBOARD_CSS}</style></head><body><main class="lock"><h1>Личный обзор TVOЁ</h1><p>Откройте бота в Telegram и отправьте <code>/web</code>. Он пришлёт временную ссылку для входа.</p></main><script nonce="{nonce}">if(location.hash.startsWith('#login=')){{const token=location.hash.slice(7);history.replaceState(null,'','/');fetch('/auth',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token}})}}).then(r=>{{if(r.ok)location.reload();}});}}</script></body></html>'''.encode('utf-8')
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; form-action 'self'; img-src https:; base-uri 'none'")
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
            message = '🆕 Уже доступно в TVOЁ:\n' + '\n'.join(
                f'{r["title"]} - доступность подтверждена' for r in batch)
            self.bot.send(message + '\nСписок сохранён в разделе «Новое» до переноса в рекламу.', MENU)
            with self.store.lock, self.store.db:
                self.store.db.executemany('UPDATE review_queue SET notified_at=? WHERE title_id=?', [(now(),r['id']) for r in batch])

    def send_decline_alerts(self):
        for row in pending_declines(self.store):
            c=row['comparison']
            message=(f'📉 В рекламе: {row["title"]}\n'
                     f'Спрос снизился на {abs(c["percent"]):.1f}%: {c["older"]:,} → {c["newer"]:,} запросов.\n'
                     f'Два полных периода по 7 дней, по {c["to"]}. Снижение подтверждено на двух разных датах данных.\n'
                     'Проверьте кампанию: возможно, стоит сократить продвижение или убрать тайтл. Реклама автоматически не отключается.')
            self.bot.send(message,MENU)
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE decline_state SET notified=1 WHERE title_id=?',(row['id'],))

    def send_growth_alerts(self, alerts):
        radar=radar_tracked_ids(self.store)
        for row in growth_alert_rows(self.store):
            c=demand_comparison(row)
            growing=bool(c and signal_fresh(self.store,row,c) and row['trend']=='растёт' and c['percent'] is not None and not self.store.meta('dynamics_error:'+row['id'],''))
            key=('growth_alert:' if growing else 'demand_alert:')+row['id']
            if self.store.meta(key)=='sent':continue
            # A high-demand notice already includes growth when both happen together.
            count=f'{row["total_count"]:,}'.replace(',',' ')
            message=(('📈 Спрос растёт: ' if growing else '🔎 Спрос выше 50 000: ')+row['title']+'\n'
                     f'Wordstat: {count} запросов за 30 дней.\n')
            if growing:message+=f'Рост {c["percent"]:.1f}%: {c["older"]:,} → {c["newer"]:,}, два полных периода по 7 дней.\n'
            else:message+='Рост пока не подтверждён.\n'
            message+=f'Запрос: {row["wordstat_query"]}\n'
            if row['total_count']>50000 or (growing and row['id'] in radar):message+='Добавлен в «Сигналы».\n'
            else:message+='Остаётся на отслеживании.\n'
            message+='Доступность в TVOЁ и результаты кампании проверяйте перед продвижением.'
            self.bot.send(message,MENU)
            self.store.set_meta(key,'sent')
            if row['id'] in radar and row['total_count']>50000:self.store.set_meta('radar_alert:'+row['id'],'sent')

    def status_message(self,text,keyboard=None):
        try: self.bot.send(text,keyboard)
        except RuntimeError: pass  # A Telegram outage must not prevent a site refresh.

    def refresh_async(self, notify=True, watch_id=None):
        if watch_id is None and self.analyzer.lock.locked():
            if notify: self.status_message('Обновление уже выполняется.')
            return
        if notify: self.status_message('🔄 Обновление запущено.')
        def run():
            if watch_id is not None and origin_info(self.store,watch_id)['status']=='unknown':
                try:refresh_origins(self.store,budget=1,target_id=watch_id)
                except Exception as exc:print('Country metadata pending: '+type(exc).__name__,flush=True)
            if watch_id is None:
                try:refresh_radar(self.store)
                except Exception as exc:print('Radar refresh error: '+type(exc).__name__,flush=True)
            # A manual addition waits behind an in-flight refresh instead of being lost.
            while True:
                result = self.analyzer.refresh(watch_id=watch_id)
                if watch_id is None or not result.get('busy'): break
                time.sleep(1)
            if result.get('busy'):
                if notify: self.status_message('Обновление уже выполняется.')
                return
            if notify:
                message = f'Обновление завершено. Новых: {len(result["new"])}; Wordstat: {result["measured"]}; динамика: {result["dynamics"]}.'
                if result['errors']:
                    message += '\nЧасть данных не обновилась. Сохранённые значения доступны.'
                self.status_message(message, MENU)
            for send in (lambda:self.send_growth_alerts(result['alerts']),self.send_decline_alerts):
                try:send()
                except RuntimeError:print('Telegram notification pending retry',flush=True)
        worker=threading.Thread(target=run, daemon=True)
        worker.start()
        return worker

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
            self.bot.send('🎬 TVOЁ - спрос на фильмы и сериалы. Каталог, радар и реклама доступны через /web.', MENU)
        elif command in ('top','growth','soon','new'):
            try: page = int(raw.split(':',1)[1]) if ':' in raw else 0
            except ValueError: page = 0
            message, keyboard = render_list(self.store, command, page)
            self.bot.send(message, keyboard)
        elif command in ('ads','watch'):
            rows=promotion_rows(self.store) if command=='ads' else self.store.watch_rows()
            rows=[r for r in rows if foreign_title(self.store,r)]
            heading='🎯 В рекламе' if command=='ads' else '👁 Отслеживаемые фильмы'
            for offset in range(0,max(1,len(rows)),10):
                batch=rows[offset:offset+10]
                message=heading+'\n'+('\n'.join(f'{r["title"]}: {r["total_count"] if r["total_count"] is not None else "ожидает проверки"} · {r["trend"] or "нет динамики"}' for r in batch) or 'Список пока пуст. Добавьте тайтл на /web.')
                self.bot.send(message[:4000],MENU)
        elif command == 'refresh':
            self.refresh_async()
        elif command == 'status':
            count = len(self.store.rows(limit=10000))
            checked = len(self.store.rows(where='status="active" AND total_count IS NOT NULL',limit=10000))
            self.bot.send(f'Коллекция: {self.store.meta("last_tvoe","ещё не обновлялась")}\nТайтлов: {count}; проверено Wordstat: {checked}\nПолный анализ: {self.store.meta("last_full") or "ещё нет"}\nОбновление: {"есть неполные данные" if self.store.meta("last_errors","") else "без ошибок"}', MENU)
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
                link = ('https://tvoe.live' + row['url']) if (row.get('url') or '').startswith('/') else (row.get('url') or '')
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
                print('Bot polling error: '+(str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('Telegram ') else type(exc).__name__), flush=True)
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
                        app.send_decline_alerts()
                        next_analysis = time.monotonic() + 3600
                elif analyzer.lock.acquire(blocking=False):
                    try:
                        refresh_catalog_feed(store)
                        store.sync(fetch_tvoe_items(analyzer.transport))
                        app.send_new_alerts()
                    finally:
                        analyzer.lock.release()
            except Exception as exc:
                print('Scheduled refresh failed: '+type(exc).__name__, flush=True)
            time.sleep(3600)
    def radar_schedule():
        while True:
            try:
                added=refresh_radar(store)
                if added:app.refresh_async(notify=False)
            except Exception as exc: print('Radar refresh error: '+type(exc).__name__,flush=True)
            time.sleep(1800)
    def origin_schedule():
        while True:
            try:
                if refresh_origins(store):
                    try:app.send_new_alerts()
                    except RuntimeError:pass
                    app.refresh_async(notify=False)
            except Exception as exc:print('Country metadata error: '+type(exc).__name__,flush=True)
            time.sleep(300)
    threading.Thread(target=origin_schedule,daemon=True).start()
    threading.Thread(target=radar_schedule,daemon=True).start()
    threading.Thread(target=schedule,daemon=True).start()
    app.loop()


if __name__ == '__main__':
    main()




