"""Actual archived checklist, geometry boundaries, and pinned-source replay."""
import base64
import json
from pathlib import Path
import shutil
import unittest
import xml.etree.ElementTree as ET

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest
from navigator.store import Store
from navigator.table_adapter import TableExtraction, _native_xml, parse_layout


def layout_fixture(numbers=(1, 2), filled=False, missing=False):
    namespace = 'http://www.w3.org/1999/xhtml'
    root = ET.Element('{' + namespace + '}html')
    page = ET.SubElement(root, '{' + namespace + '}page', width='600', height='800')
    def word(text, x, y, width=40):
        ET.SubElement(page, '{' + namespace + '}word', xMin=str(x), yMin=str(y),
                      xMax=str(x + width), yMax=str(y + 10)).text = text
    for name, x in [('点検事項', 30), ('点検の内容', 200), ('良否', 390), ('是正内容', 450), ('確認', 540)]:
        if missing and name == '確認':
            continue
        word(name, x, 100)
    for index, number in enumerate(numbers):
        y = 140 + index * 60
        word(str(number), 30, y + 1.9, 5)
        word('検査対象' + str(number), 45, y, 70)
        word('内容' + str(number), 210, y)
        if filled:
            word('○', 395, y, 10)
    return ET.tostring(root, encoding='utf-8')


class TableLayoutTests(unittest.TestCase):
    def assert_error(self, status, code, fn):
        with self.assertRaises(APIError) as error:
            fn()
        self.assertEqual((error.exception.status, error.exception.code), (status, code))

    def test_count_is_actual_and_font_baselines_do_not_move_next_row_label(self):
        result = parse_layout(layout_fixture(numbers=(1, 2, 3)))
        self.assertEqual(result['row_count'], 3)
        self.assertEqual(result['column_count'], 5)
        self.assertEqual([r['number'] for r in result['rows']], [1, 2, 3])
        for row in result['rows']:
            self.assertEqual(row['cells'][0]['text'], str(row['number']) + ' 検査対象' + str(row['number']))
            self.assertEqual(row['cells'][1]['text'], '内容' + str(row['number']))
            self.assertTrue(all(c['state'] == 'empty_text_layer' for c in row['cells'][2:]))

    def test_missing_header_and_noncontiguous_numbers_hold(self):
        result = parse_layout(layout_fixture(missing=True))
        self.assertEqual(result['status'], 'hold')
        self.assertEqual(result['rows'], [])
        result = parse_layout(layout_fixture(numbers=(1, 3)))
        self.assertEqual(result['reason'], 'table_numbered_rows_not_supported')
        self.assertEqual(result['rows'], [])

    def test_visible_text_in_result_cell_is_preserved_without_pass_interpretation(self):
        result = parse_layout(layout_fixture(filled=True))
        self.assertEqual(result['rows'][0]['cells'][2]['text'], '○')
        self.assertEqual(result['rows'][0]['cells'][2]['state'], 'text_present')
        self.assertNotIn('pass', canonical(result))

    def test_bounded_xml_coordinates_entities_and_source_size(self):
        for xml in (layout_fixture().replace(b'xMin="30"', b'xMin="NaN"'),
                    b'<!DOCTYPE html [<!ENTITY malicious "boom">]><html/>'):
            self.assert_error(422, 'table_layout_invalid', lambda: parse_layout(xml))
        self.assert_error(413, 'table_output_limit', lambda: parse_layout(b'x' * (1024 * 1024 + 1)))
        self.assert_error(413, 'source_byte_limit', lambda: _native_xml(b'x' * (4 * 1024 * 1024 + 1), 1))




if __name__ == '__main__':
    unittest.main()
