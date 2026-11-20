"""Offline tests with synthetic data; no client data or API credentials."""
import io
import sqlite3
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET

from campaigns_wordstat import (
    parse_campaigns_xlsx, setup_campaign_schema, import_ya_campaigns,
    director_campaign_rows, director_pending_queries, director_set_query,
    director_save_measurement,
)

N = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
HEADERS = ['Название кампании', '№ Кампании', 'Расход, ₽', 'Клики',
           'Конверсии', 'CR, %', 'CPA, ₽', 'Показы']


def fixture():
    root = ET.Element('{%s}worksheet' % N)
    data = ET.SubElement(root, '{%s}sheetData' % N)
    records = [
        ['Период: 21.10.2026 - 19.11.2026'], HEADERS,
        ['МК/Тестовый фильм/20.11.2026', '123', '1000.5', '100', '5', '5', '200.1', '10000'],
        ['Поиск/Тестовый фильм/20.11.2026', '124', '500', '50', '3', '6', '166.67', '5000'],
        ['ТК/Фильмы/20.11.2026', '125', '200', '20', '0', '0', '-', '3000'],
    ]
    for row_number, values in enumerate(records, 1):
        row = ET.SubElement(data, '{%s}row' % N, r=str(row_number))
        for col, value in enumerate(values):
            cell = ET.SubElement(row, '{%s}c' % N, r=chr(65+col)+str(row_number), t='inlineStr')
            inline = ET.SubElement(cell, '{%s}is' % N)
            ET.SubElement(inline, '{%s}t' % N).text = value
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('xl/worksheets/sheet1.xml', ET.tostring(root, encoding='utf-8'))
    return stream.getvalue()


class Store:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        setup_campaign_schema(self)


class CampaignImportTests(unittest.TestCase):
    def test_xlsx_grouping_and_missing_cpa(self):
        info = parse_campaigns_xlsx(fixture())
        self.assertEqual(len(info['campaigns']), 3)
        self.assertEqual(info['title_groups'], 1)
        self.assertEqual(info['matched_campaigns'], 2)
        self.assertEqual(info['generic_campaigns'], 1)
        self.assertIsNone(info['campaigns'][2]['cpa'])

    def test_reimport_preserves_manual_queries(self):
        store = Store()
        info = import_ya_campaigns(store, fixture())
        self.assertEqual(len(director_pending_queries(store)), 1)
        self.assertEqual(info['title_groups'], 1)
        director_set_query(store, '', 'фильмы онлайн', campaign_id='125')
        director_save_measurement(store, 'тестовыйфильм', 12345)
        self.assertEqual(len(director_pending_queries(store)), 1)
        import_ya_campaigns(store, fixture())
        rows = director_campaign_rows(store)
        self.assertEqual(len(rows), 3)
        self.assertEqual(next(r for r in rows if r['campaign_id']=='125')['group_key'], 'custom:125')
        self.assertEqual(next(r for r in rows if r['campaign_id']=='123')['total_count'], 12345)

    def test_reject_invalid_upload(self):
        with self.assertRaises(ValueError):
            parse_campaigns_xlsx(b'not an Excel workbook')


if __name__ == '__main__':
    unittest.main()
