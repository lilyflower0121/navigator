"""Native transfer, package preservation, adversarial XML and process boundaries."""
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

from navigator.contracts import APIError, digest
from navigator import xlsx_adapter as xlsx


def tamper(raw, part, transform):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, 'w') as result:
        for info in source.infolist():
            content = source.read(info)
            result.writestr(info, transform(content) if info.filename == part else content)
    return output.getvalue()


def change_cell(raw, address, value):
    def transform(content):
        root = ET.fromstring(content)
        cell = root.find(f'.//{{{xlsx.NS}}}c[@r="{address}"]')
        cell.set('t', 'str')
        for child in list(cell):
            cell.remove(child)
        ET.SubElement(cell, f'{{{xlsx.NS}}}v').text = value
        return ET.tostring(root)
    return tamper(raw, 'xl/worksheets/sheet1.xml', transform)


class XLSXTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not xlsx.worker_available():
            raise unittest.SkipTest('Licensed XLSX worker not enabled; see DEPENDENCIES.md')
        with tempfile.TemporaryDirectory(prefix='navigator-test-xlsx-') as temporary:
            Path(temporary, 'request.json').write_text(json.dumps(dict(mode='fixture')))
            xlsx._run_worker(temporary)
            cls.original = Path(temporary, 'output.xlsx').read_bytes()
        cls.cells = [dict(sheet='Fixture', cell='B2', value='transferred literal')]
        cls.output, cls.manifest = xlsx.render_xlsx(cls.original, cls.cells)

    def assert_error(self, code, operation, status=None):
        with self.assertRaises(APIError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def test_native_transfer_and_immutable_package_parts(self):
        self.assertEqual(self.manifest['original_sha256'], digest(self.original))
        self.assertEqual(self.manifest['output_sha256'], digest(self.output))
        self.assertEqual(self.manifest['mapped_cell_values'], self.cells)
        self.assertEqual(xlsx._snapshot(self.output)['Fixture']['cells']['B2']['value'], 'transferred literal')
        with zipfile.ZipFile(io.BytesIO(self.original)) as source, zipfile.ZipFile(io.BytesIO(self.output)) as output:
            for name in source.namelist():
                if name != 'xl/worksheets/sheet1.xml':
                    self.assertEqual(source.read(name), output.read(name), name)
        self.assertTrue(self.manifest['preserved_sheet_layout'])

    def test_formula_like_text_is_literal_and_typed_values_preserved(self):
        for value in ('=2+3', '+2', '@SUM(A1)', '-2', "'=2+3", '', None, False, 2.5):
            cells = [dict(sheet='Fixture', cell='B2', value=value)]
            output, result = xlsx.render_xlsx(self.original, cells)
            self.assertEqual(result['mapped_cell_values'], cells)
            self.assertFalse(any(xlsx._local(n.tag) == 'f' for root in xlsx._parts(output).values() for n in root.iter()))

    def test_unmapped_or_mapped_tamper_rejected_even_with_new_hash(self):
        self.assert_error('xlsx_preservation_failed', lambda: xlsx.validate_output(self.original, change_cell(self.output, 'A2', 'coherent mutation'), self.cells))
        self.assert_error('xlsx_mapped_value_mismatch', lambda: xlsx.validate_output(self.original, change_cell(self.output, 'B2', 'fabricated'), self.cells))

    def test_style_and_theme_and_margins_tamper_rejected(self):
        for part, old, new in [('xl/styles.xml', b'FF123456', b'FF654321'),
                               ('xl/worksheets/sheet1.xml', b'left="0.7"', b'left="0.8"')]:
            bad = tamper(self.output, part, lambda content: content.replace(old, new))
            self.assertNotEqual(bad, self.output)
            self.assert_error('xlsx_preservation_failed', lambda: xlsx.validate_output(self.original, bad, self.cells))

    def test_merge_and_row_changes_rejected(self):
        for old, new in [(b'A1:C1', b'A1:B1'), (b'ht="24"', b'ht="25"')]:
            bad = tamper(self.output, 'xl/worksheets/sheet1.xml', lambda content: content.replace(old, new))
            self.assert_error('xlsx_preservation_failed', lambda: xlsx.validate_output(self.original, bad, self.cells))

    def test_mapping_bounds_non_anchor_paths_duplicate_and_unknown_fields(self):
        bad_cases = [([], 'xlsx_mapping_limit'),
            ([dict(sheet='Fixture', cell='A1:C1', value='x')], 'xlsx_cell_invalid'),
            ([dict(sheet='Fixture', cell='B1', value='x')], 'xlsx_merged_cell_non_anchor'),
            ([dict(sheet='../../input', cell='B2', value='x')], 'xlsx_sheet_invalid'),
            ([dict(sheet='Fixture', cell='ZZZ1', value='x')], 'xlsx_extent_limit'),
            ([dict(sheet='Fixture', cell='D2', value='x')], 'xlsx_cell_missing'),
            (self.cells * 2, 'xlsx_duplicate_cell'),
            ([dict(self.cells[0], path='/tmp/pwn')], 'xlsx_mapping_invalid'),
            ([dict(self.cells[0], value='x' * 8193)], 'xlsx_value_invalid'),
            ([dict(self.cells[0], value='\x00')], 'xlsx_value_invalid'),
            ([dict(self.cells[0], value=float('nan'))], 'xlsx_value_invalid'),
            ([dict(self.cells[0], value={'formula': '=2'})], 'xlsx_value_invalid')]
        for cells, code in bad_cases:
            self.assert_error(code, lambda: xlsx.render_xlsx(self.original, cells))

    def test_zip_bounds_unsafe_parts_entities_and_formula(self):
        self.assert_error('xlsx_byte_limit', lambda: xlsx.validate_xlsx(b'x' * (xlsx.MAX_XLSX_BYTES + 1)), 413)
        self.assert_error('xlsx_invalid', lambda: xlsx.validate_xlsx(b'not a zip'))
        for prefix in (b'<!DOCTYPE x [<!ENTITY payload "evil">]>', b'<!ENTITY payload "evil">', b'\x00'):
            bad = tamper(self.original, 'xl/worksheets/sheet1.xml', lambda c: prefix + c)
            self.assert_error('xlsx_invalid', lambda: xlsx.validate_xlsx(bad))
        bad = tamper(self.original, 'xl/worksheets/sheet1.xml', lambda c: c.replace(b'<x:v>before', b'<x:f>1+1</x:f><x:v>before'))
        self.assert_error('xlsx_formula_unsupported', lambda: xlsx.validate_xlsx(bad))
        for old, new in ((b'r="1"', b'r="10001"'), (b'max="1"', b'max="257"')):
            bad = tamper(self.original, 'xl/worksheets/sheet1.xml', lambda c: c.replace(old, new, 1))
            self.assert_error('xlsx_extent_limit', lambda: xlsx.validate_xlsx(bad), 413)
        bad = tamper(self.original, 'xl/worksheets/sheet1.xml', lambda c: c.replace(b'<x:v>0</x:v>', b'<x:v>NaN</x:v>', 1))
        self.assert_error('xlsx_invalid', lambda: xlsx.validate_xlsx(bad))
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('bomb.xml', b'x' * (xlsx.MAX_PART_BYTES + 1))
        self.assert_error('xlsx_expansion_limit', lambda: xlsx.validate_xlsx(output.getvalue()), 413)

    def test_external_and_macro_parts_rejected(self):
        bad = tamper(self.original, 'xl/_rels/workbook.xml.rels', lambda c: c.replace(b'Target=', b'TargetMode="External" Target='))
        self.assert_error('xlsx_external_link_unsupported', lambda: xlsx.validate_xlsx(bad))
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.original)) as original, zipfile.ZipFile(output, 'w') as archive:
            for info in original.infolist():
                archive.writestr(info, original.read(info))
            archive.writestr('xl/vbaProject.bin', b'x')
        self.assert_error('xlsx_feature_unsupported', lambda: xlsx.validate_xlsx(output.getvalue()))

    def test_server_only_layout_exception_cannot_broaden(self):
        for adjustments in ([], [dict(sheet='Fixture', row=7, height=180)], [dict(sheet='完了検査成績書', row=7, height=181)]):
            self.assert_error('xlsx_layout_adjustment_invalid', lambda: xlsx.render_xlsx(self.original, self.cells, row_heights=adjustments))


    def test_private_source_cleanup_and_missing_output(self):
        paths = []
        def worker(directory):
            paths.append(Path(directory))
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(Path(directory, 'input.xlsx').stat().st_mode), 0o600)
            self.assertEqual(Path(directory, 'input.xlsx').read_bytes(), self.original)
            Path(directory, 'output.xlsx').write_bytes(self.output)
        with patch.object(xlsx, '_run_worker', side_effect=worker):
            xlsx.render_xlsx(self.original, self.cells)
        self.assertFalse(any(path.exists() for path in paths))
        with patch.object(xlsx, '_run_worker', return_value=None):
            self.assert_error('xlsx_output_missing', lambda: xlsx.render_xlsx(self.original, self.cells))

    def test_timeout_native_failure_stdout_limit_and_thread_cap(self):
        real_popen = subprocess.Popen
        def native(code):
            return lambda argv, **kwargs: real_popen([sys.executable, '-c', code], **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(xlsx.subprocess, 'Popen', side_effect=native('import time;time.sleep(1)')), patch.object(xlsx, 'CLI_TIMEOUT_SECONDS', 0.05):
                self.assert_error('xlsx_cli_timeout', lambda: xlsx._run_worker(directory), 408)
            with patch.object(xlsx.subprocess, 'Popen', side_effect=native('import sys;sys.stderr.write("private path");sys.exit(1)')):
                self.assert_error('xlsx_cli_failed', lambda: xlsx._run_worker(directory))
            with patch.object(xlsx.subprocess, 'Popen', side_effect=native('import os;os.write(1,b"x"*17000)')):
                self.assert_error('xlsx_worker_output_limit', lambda: xlsx._run_worker(directory), 413)
            with patch.object(xlsx.subprocess, 'Popen', side_effect=native('import os;assert os.environ["OMP_THREAD_LIMIT"]=="1";assert os.environ["UV_THREADPOOL_SIZE"]=="1"')):
                xlsx._run_worker(directory)


if __name__ == '__main__':
    unittest.main()
