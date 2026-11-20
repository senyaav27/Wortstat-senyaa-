"""Excel import for TVOE Wordstat. Only Python 3.12 standard library.

IMPORTANT: the Direct report contains no current campaign status. Imported rows
are report observations, not proof campaigns remain active.
"""
from __future__ import annotations

from io import BytesIO
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile

XML_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
CELL = re.compile(r'^([A-Z]+)\d+$')
PERIOD = re.compile(r'(\d{2}\.\d{2}\.\d{4})\s*[-–]\s*(\d{2}\.\d{2}\.\d{4})')
DATE = re.compile(r'^\d{1,2}[.,]\d{1,2}[.,]\d{2,4}$')
GENERICS = ('лендинг', 'special', 'фильмы', 'фильм_', 'фильм ', 'сериалы',
    'ключи', 'интересы', 'сайты', 'жанр', 'культовые', 'криминальные',
    'романтические', 'зарубежные', 'кино', 'новинки', 'спортивные', 'топ',
    '30 дней', 'авторизация', 'общие', 'конкуренты', 'бренд', 'фильм_сериалы')


def group_key(title):
    return re.sub(r'[^\w]+', '', str(title).casefold().replace('ё', 'е'))[:160]


def title_from_campaign(name):
    pieces = [p.strip() for p in name.split('/')]
    if len(pieces) < 2 or pieces[0].casefold() not in ('мк', 'поиск', 'медийная'):
        return ''
    title = pieces[1].strip(' _\u00a0')
    if not title or any(title.casefold().startswith(p) for p in GENERICS):
        return ''
    if len(title) < 3 or len(title) > 160 or DATE.fullmatch(title):
        return ''
    return title


def _cell_value(cell, shared):
    kind = cell.get('t', '')
    if kind == 'inlineStr':
        return ''.join((t.text or '') for t in cell.iter(XML_NS + 't')).strip()
    child = cell.find(XML_NS + 'v')
    value = child.text if child is not None else ''
    if kind == 's':
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            raise ValueError('Ошибка строк в файле Excel.') from None
    return value or ''


def _read_sheet(raw):
    if not raw or len(raw) > 8_000_000:
        raise ValueError('Файл XLSX пустой или слишком большой (максимум 8 МБ).')
    try:
        with ZipFile(BytesIO(raw)) as z:
            entries = z.infolist()
            if (len(entries) > 100 or sum(x.file_size for x in entries) > 25_000_000
                    or any(x.file_size > 12_000_000 for x in entries)):
                raise ValueError('Файл Excel превышает допустимый размер распаковки.')
            files = set(z.namelist())
            if 'xl/worksheets/sheet1.xml' not in files:
                raise ValueError('Не найден первый лист Excel.')
            shared = []
            if 'xl/sharedStrings.xml' in files:
                root = ET.fromstring(z.read('xl/sharedStrings.xml'))
                shared = [''.join(t.text or '' for t in si.iter(XML_NS + 't'))
                          for si in root.iter(XML_NS + 'si')]
            root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            rows = []
            for row in root.iter(XML_NS + 'row'):
                values = {}
                for cell in row.findall(XML_NS + 'c'):
                    m = CELL.match(cell.get('r', ''))
                    if not m:
                        continue
                    col = 0
                    for char in m.group(1):
                        col = col * 26 + ord(char) - 64
                    if col <= 32:
                        values[col - 1] = _cell_value(cell, shared)
                if values:
                    rows.append([values.get(i, '') for i in range(max(values) + 1)])
                if len(rows) > 10000:
                    raise ValueError('Слишком много строк в Excel.')
            return rows
    except (BadZipFile, ET.ParseError, EOFError):
        raise ValueError('Файл не является корректной выгрузкой XLSX.') from None


def _num(value, integer=False):
    s = str(value or '').replace('\u00a0', '').replace(' ', '').replace(',', '.').strip()
    if s in ('', '-', '—', '–'):
        return 0 if integer else None
    try:
        n = float(s)
    except ValueError:
        raise ValueError('В Excel обнаружено некорректное числовое значение.') from None
    if not (-1e10 < n < 1e10):
        raise ValueError('В Excel обнаружено недопустимое числовое значение.')
    return int(n) if integer else n


def parse_campaigns_xlsx(raw):
    rows = _read_sheet(raw)
    header_index = next((i for i, row in enumerate(rows)
                         if 'Название кампании' in row and '№ Кампании' in row), None)
    if header_index is None:
        raise ValueError('Нужен отчет Директа со столбцами «Название кампании» и «№ Кампании».')
    header = rows[header_index]
    columns = {name: header.index(name) for name in ('Название кампании', '№ Кампании')}
    for label in ('Расход, ₽', 'Клики', 'Конверсии', 'CR, %', 'CPA, ₽', 'Показы'):
        if label not in header:
            raise ValueError('В файле отсутствует столбец «'+label+'».')
        columns[label] = header.index(label)
    period = next((PERIOD.search(str(cell)).groups() for row in rows[:header_index]
                   for cell in row if PERIOD.search(str(cell))), ('', ''))
    campaigns, seen = [], set()
    for row in rows[header_index + 1:]:
        def get(column):
            idx = columns[column]
            return str(row[idx]).strip() if idx < len(row) else ''
        campaign_id = get('№ Кампании')
        if not campaign_id.isdigit() or campaign_id in seen:
            continue
        name = get('Название кампании')
        if not name or len(name) > 512:
            continue
        seen.add(campaign_id)
        title = title_from_campaign(name)
        campaigns.append({
            'campaign_id': campaign_id, 'name': name,
            'title': title, 'group_key': group_key(title) if title else '',
            'spend': _num(get('Расход, ₽')),
            'clicks': _num(get('Клики'), integer=True),
            'conversions': _num(get('Конверсии'), integer=True),
            'cr': _num(get('CR, %')),
            'cpa': _num(get('CPA, ₽')),
            'impressions': _num(get('Показы'), integer=True),
        })
    if not campaigns:
        raise ValueError('Не найдено рекламных кампаний. Проверьте формат выгрузки.')
    if len(campaigns) > 5000:
        raise ValueError('Максимум 5000 кампаний за один импорт.')
    distinct = {c['group_key'] for c in campaigns if c['group_key']}
    return {'period_start': period[0], 'period_end': period[1],
            'campaigns': campaigns, 'title_groups': len(distinct),
            'matched_campaigns': sum(bool(c['group_key']) for c in campaigns),
            'generic_campaigns': sum(not c['group_key'] for c in campaigns)}


def setup_campaign_schema(store):
    """Add independent campaign tables; never alter TVOE film/Wordstat data."""
    with store.lock, store.db:
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS director_campaigns (
                campaign_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', group_key TEXT NOT NULL DEFAULT '',
                spend REAL, clicks INTEGER, conversions INTEGER, cr REAL,
                cpa REAL, impressions INTEGER, period_start TEXT, period_end TEXT,
                imported_at TEXT NOT NULL, in_latest_import INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS director_queries (
                group_key TEXT PRIMARY KEY, title TEXT NOT NULL,
                query TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER, previous_count INTEGER, measured_at TEXT,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS director_query_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_key TEXT NOT NULL,
                query TEXT NOT NULL, measured_at TEXT NOT NULL, total_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_director_groups ON director_campaigns(group_key,in_latest_import);
        ''')


def import_ya_campaigns(store, raw):
    """Replace latest report snapshot by ID; preserve prior measurements and history."""
    from datetime import datetime, timezone
    parsed = parse_campaigns_xlsx(raw)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with store.lock, store.db:
        store.db.execute('UPDATE director_campaigns SET in_latest_import=0')
        for item in parsed['campaigns']:
            key = item['group_key']
            if key:
                store.db.execute('''INSERT OR IGNORE INTO director_queries(group_key,title,query)
                                   VALUES(?,?,?)''', (key, item['title'], item['title']))
            store.db.execute('''INSERT INTO director_campaigns(
                campaign_id,name,title,group_key,spend,clicks,conversions,cr,cpa,impressions,
                period_start,period_end,imported_at,in_latest_import)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(campaign_id) DO UPDATE SET
                name=excluded.name,title=excluded.title,group_key=excluded.group_key,
                spend=excluded.spend,clicks=excluded.clicks,conversions=excluded.conversions,
                cr=excluded.cr,cpa=excluded.cpa,impressions=excluded.impressions,
                period_start=excluded.period_start,period_end=excluded.period_end,
                imported_at=excluded.imported_at,in_latest_import=1''', (
                item['campaign_id'], item['name'], item['title'], key,
                item['spend'], item['clicks'], item['conversions'], item['cr'],
                item['cpa'], item['impressions'], parsed['period_start'],
                parsed['period_end'], stamp))
        store.db.execute('''INSERT INTO metadata(key,value) VALUES('director_imported_at',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (stamp,))
        store.db.execute('''INSERT INTO metadata(key,value) VALUES('director_report_period',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (
            parsed['period_start'] + ' - ' + parsed['period_end'],))
    return parsed


def director_campaign_rows(store, query='', limit=5000):
    with store.lock:
        rows = store.db.execute('''SELECT c.*, q.query, q.verified, q.total_count,
            q.previous_count, q.measured_at, q.error
            FROM director_campaigns c LEFT JOIN director_queries q ON q.group_key=c.group_key
            WHERE c.in_latest_import=1 ORDER BY c.spend DESC,c.campaign_id LIMIT ?''', (limit,)).fetchall()
    return [dict(r) for r in rows if not query or query.casefold() in (r['name']+' '+r['title']).casefold()]


def director_pending_queries(store):
    from datetime import datetime, timedelta, timezone
    stamp = datetime.now(timezone.utc) - timedelta(hours=24)
    with store.lock:
        rows = store.db.execute('''SELECT q.* FROM director_queries q WHERE
            EXISTS(SELECT 1 FROM director_campaigns c WHERE c.group_key=q.group_key
            AND c.in_latest_import=1) AND LENGTH(TRIM(q.query)) > 1
            ORDER BY CASE WHEN q.measured_at IS NULL THEN 0 ELSE 1 END,
            q.measured_at ASC, q.group_key ASC''').fetchall()
    result = []
    for row in rows:
        try:
            if row['measured_at'] and datetime.fromisoformat(row['measured_at']) > stamp:
                continue
        except (ValueError, TypeError):
            pass
        result.append(dict(row))
    return result


def director_save_measurement(store, group, count):
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with store.lock, store.db:
        old = store.db.execute('SELECT query,total_count FROM director_queries WHERE group_key=?', (group,)).fetchone()
        if not old:
            return
        store.db.execute('''UPDATE director_queries SET previous_count=?,total_count=?,
            measured_at=?,error='' WHERE group_key=?''', (old['total_count'], count, stamp, group))
        store.db.execute('''INSERT INTO director_query_history(group_key,query,measured_at,total_count)
            VALUES(?,?,?,?)''', (group, old['query'], stamp, count))


def director_set_query(store, key, phrase, campaign_id=''):
    phrase = phrase.strip()
    if not 2 <= len(phrase) <= 180 or any(ord(ch) < 32 for ch in phrase):
        raise ValueError('Укажите корректный поисковый запрос от 2 до 180 символов.')
    with store.lock, store.db:
        if campaign_id:
            row = store.db.execute('SELECT * FROM director_campaigns WHERE campaign_id=? AND in_latest_import=1', (campaign_id,)).fetchone()
            if not row or row['group_key']:
                raise ValueError('Кампания недоступна или уже привязана к тайтлу.')
            key = 'custom:' + campaign_id
            store.db.execute('''INSERT OR IGNORE INTO director_queries(group_key,title,query,verified)
                VALUES(?,?,?,1)''', (key, row['name'], phrase))
            store.db.execute('UPDATE director_campaigns SET group_key=? WHERE campaign_id=?', (key,campaign_id))
        else:
            if not store.db.execute('SELECT 1 FROM director_queries WHERE group_key=?', (key,)).fetchone():
                raise ValueError('Группа не найдена.')
        old = store.db.execute('SELECT query FROM director_queries WHERE group_key=?', (key,)).fetchone()
        changed = old['query'] != phrase
        store.db.execute('''UPDATE director_queries SET query=?, verified=1,
            total_count=CASE WHEN ? THEN NULL ELSE total_count END,
            previous_count=CASE WHEN ? THEN NULL ELSE previous_count END,
            measured_at=CASE WHEN ? THEN NULL ELSE measured_at END,
            error='' WHERE group_key=?''', (phrase, int(changed), int(changed), int(changed), key))
    return key


def render_campaign_section(store, csrf, query=''):
    """HTML fragment for existing 'Реклама' tab. Campaign rows stay distinct."""
    import html
    from collections import defaultdict
    esc = html.escape
    campaigns = director_campaign_rows(store, query)
    all_count = len(director_campaign_rows(store))
    grouped = defaultdict(list)
    generic = []
    for row in campaigns:
        (grouped[row['group_key']] if row['group_key'] else generic).append(row)
    parts = [
        '<section class="director-import"><details class="analysis source-list" open>',
        '<summary>Загрузить кампании Яндекс Директа · Excel (.xlsx)</summary>',
        '<form class="director-upload" action="/campaign-import" method="post" enctype="multipart/form-data">',
        csrf, '<label>Файл отчета Мастера отчетов<input type="file" name="campaign_file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required></label>',
        '<button type="submit">Импортировать кампании</button></form>',
        '<p class="notice">Из файла загружаются ID, названия и результаты кампаний. ',
        '<strong>В отчете нет статуса активности.</strong> Состав кампаний соответствует последней выгрузке; ',
        'это не подтверждение того, что они работают сейчас. ',
        'Спрос через Wordstat измеряется по названию тайтла или по уточненному запросу.</p></details>',
    ]
    if not all_count:
        parts.append('<p class="notice">Загрузите XLSX из Директа: пока нет импортированных кампаний.</p></section>')
        return ''.join(parts)
    with store.lock:
        latest = store.db.execute("SELECT value FROM metadata WHERE key='director_report_period'").fetchone()
    period = latest[0] if latest else 'не указан'
    parts.append('<h3>Кампании из последней выгрузки · '+str(all_count)+'</h3>')
    parts.append('<p class="notice">Период отчета: '+esc(period)+'. Данные по расходам и конверсиям из Excel; спрос из Wordstat, не из рекламного кабинета.</p>')
    for group, rows in grouped.items():
        first = rows[0]
        number = first['total_count']
        demand = ('<strong>'+f'{number:,}'.replace(',', ' ')+'</strong> запросов за ~30 дней' if number is not None else 'Ожидает проверки Wordstat')
        if first['error']:
            demand = 'Не удалось обновить. Сохраненная история не удалена.'
        stamp = esc((first['measured_at'] or '')[:16].replace('T',' '))
        mark = 'Подтвержденный запрос' if first['verified'] else 'Предварительный запрос - проверьте совпадение'
        parts.append('<article class="director-group" data-entry-id="director:'+esc(group,quote=True)+'">')
        parts.append('<h4>'+esc(first['title'])+' <small>'+str(len(rows))+' камп.</small></h4>')
        parts.append('<p class="director-demand">'+demand+'</p>')
        parts.append('<p class="detail">'+esc(mark)+' · '+esc(first['query'] or '')+(' · '+stamp if stamp else '')+'</p>')
        parts.append('<form class="director-query" action="/campaign-query" method="post">'+csrf+
            '<input type="hidden" name="group_key" value="'+esc(group,quote=True)+'">'+
            '<input name="phrase" aria-label="Запрос для Wordstat" maxlength="180" required value="'+esc(first['query'] or '',quote=True)+'">'+
            '<button class="secondary" type="submit">Уточнить запрос</button></form>')
        for row in rows:
            parts.append(_campaign_row_html(row))
        parts.append('</article>')
    if generic:
        parts.append('<details class="analysis director-generic"><summary>Общие кампании без названия конкретного фильма · '+str(len(generic))+'</summary>')
        parts.append('<p class="notice">Название кампании не всегда является поисковым запросом. Для проверки спроса здесь сначала укажите фразу вручную.</p>')
        for row in generic:
            parts.append(_campaign_row_html(row))
            parts.append('<form class="director-query" action="/campaign-query" method="post">'+csrf+
                '<input type="hidden" name="campaign_id" value="'+esc(row['campaign_id'],quote=True)+'">'+
                '<input name="phrase" aria-label="Указать запрос Wordstat" maxlength="180" required placeholder="Фраза для Wordstat">'+
                '<button class="secondary" type="submit">Отслеживать запрос</button></form>')
        parts.append('</details>')
    parts.append('</section>')
    return ''.join(parts)


def _campaign_row_html(row):
    import html
    esc=html.escape
    def num(key):
        value=row[key]
        return ('-' if value is None else f'{value:,.0f}'.replace(',',' '))
    cpa= '-' if row['cpa'] is None else f'{row["cpa"]:,.2f}'.replace(',',' ')
    return ('<div class="director-row"><div class="director-name">'+esc(row['name'])+
            '<small>ID '+esc(row['campaign_id'])+'</small></div>'+
            '<div>Расход <strong>'+num('spend')+' ₽</strong></div>'+
            '<div>Конверсии <strong>'+num('conversions')+'</strong></div>'+
            '<div>CPA <strong>'+cpa+' ₽</strong></div></div>')
