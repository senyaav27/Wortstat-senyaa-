"""Private TVOЁ + Yandex Wordstat analytics bot. Configure environment variables, then run this file."""
"""TVOЁ collection, Wordstat measurements, persistence, and trend calculations."""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
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
        direction, delta = trend(points or [])
        serialized = json.dumps(points, ensure_ascii=False) if points is not None else None
        with self.lock, self.db:
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
        stats = {'new': [], 'measured': 0, 'dynamics': 0, 'errors': []}
        try:
            items = fetch_tvoe_items(self.transport)
            stats['new'] = self.store.sync(items)
            rows = self.store.rows('first_seen DESC', limit=10000)
            candidates = []
            # Refresh never measured entries first, then stale measurements.
            rows.sort(key=lambda r: r['measured_at'] or '')
            for row in rows[:self.top_budget]:
                try:
                    initial = self.wordstat.top(row['title'])
                    query, note, ambiguous = choose_query(row, initial)
                    top = self.wordstat.top(query) if query != row['title'] else initial
                    count = int(top['totalCount'])
                    candidates.append((count,row,query,note,ambiguous))
                    self.store.measured(row['id'],query,note,ambiguous,count,None)
                    stats['measured'] += 1
                except APIError as exc:
                    stats['errors'].append(str(exc))
                    if 'HTTP 429' in str(exc):
                        break
            candidates.sort(key=lambda entry: entry[0], reverse=True)
            for count,row,query,note,ambiguous in candidates:
                if stats['dynamics'] >= self.dynamics_budget:
                    break
                if ambiguous:
                    continue
                try:
                    points = self.wordstat.dynamics(query)
                    self.store.attach_dynamics(row['id'],points)
                    stats['dynamics'] += 1
                except APIError as exc:
                    stats['errors'].append(str(exc))
                    if 'HTTP 429' in str(exc):
                        break
            self.store.set_meta('last_full', now() if stats['measured'] == len(rows) and not stats['errors'] else self.store.meta('last_full',''))
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


class BotApp:
    def __init__(self, bot, store, analyzer):
        self.bot, self.store, self.analyzer = bot, store, analyzer

    def refresh_async(self, notify=True):
        if self.analyzer.lock.locked():
            self.bot.send('Обновление уже выполняется.')
            return
        self.bot.send('🔄 Обновление запущено.')
        def run():
            result = self.analyzer.refresh()
            if notify:
                message = f'Обновление завершено. Новых: {len(result["new"])}; Wordstat: {result["measured"]}; динамика: {result["dynamics"]}.'
                if result['errors']:
                    message += '\nОшибки: ' + '; '.join(dict.fromkeys(result['errors']))[:500]
                self.bot.send(message, MENU)
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
            self.bot.send(f'Коллекция: {self.store.meta("last_tvoe","ещё не обновлялась")}\nТайтлов: {count}; проверено Wordstat: {checked}\nПолный анализ: {self.store.meta("last_full","ещё нет")}\nОшибки: {self.store.meta("last_errors","нет") or "нет"}', MENU)
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
    def schedule():
        while True:
            try:
                result = analyzer.refresh()
                if result['new'] and store.meta('notified_once'):
                    bot.send('🆕 Новое в TVOЁ: '+', '.join(result['new'][:10]), MENU)
                store.set_meta('notified_once','1')
            except Exception as exc:
                print('Scheduled refresh failed: '+type(exc).__name__, flush=True)
            time.sleep(86400)
    threading.Thread(target=schedule,daemon=True).start()
    app.loop()


if __name__ == '__main__':
    main()
