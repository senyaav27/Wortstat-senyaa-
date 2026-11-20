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
