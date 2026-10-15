"""Synthetic native-PDF contract fixtures, not original-document quality or model evaluation.

The small PDFs below are constructed using only stdlib. Real installed Poppler
executables extract their actual bytes. Mocks cover expensive/error boundaries.
CLI text output alone is not an independently reviewed source-quality gold set.
"""
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

from navigator.contracts import APIError, LIMITS, digest
from navigator import pdf_adapter as pdf


def native_pdf(pages):
    """Return a valid uncompressed ASCII PDF containing the supplied page lines."""
    objects = [b'', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    page_ids = []
    for lines in pages:
        page_id, content_id = len(objects) + 1, len(objects) + 2
        page_ids.append(page_id)
        objects.append(('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                        '/Resources << /Font << /F1 3 0 R >> >> '
                        '/Contents %d 0 R >>' % content_id).encode())
        content = b'BT /F1 12 Tf 18 TL 36 740 Td\n'
        for index, line in enumerate(lines):
            escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            content += (('T*\n' if index else '') + '(' + escaped + ') Tj\n').encode('ascii')
        content += b'ET\n'
        objects.append(b'<< /Length ' + str(len(content)).encode() +
                       b' >>\nstream\n' + content + b'endstream')
    objects[0] = b'<< /Type /Catalog /Pages 2 0 R >>'
    objects[1] = ('<< /Type /Pages /Count %d /Kids [%s] >>' %
                  (len(page_ids), ' '.join('%d 0 R' % i for i in page_ids))).encode()
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(('%d 0 obj\n' % number).encode() + value + b'\nendobj\n')
    xref = len(data)
    data.extend(('xref\n0 %d\n0000000000 65535 f \n' % len(offsets)).encode())
    for offset in offsets[1:]:
        data.extend(('%010d 00000 n \n' % offset).encode())
    data.extend(('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' %
                 (len(offsets), xref)).encode())
    return bytes(data)


def claim(raw):
    return dict(raw=raw, sha=digest(raw), asset='a' * 32, version=1,
                meta=dict(media_type='application/pdf'))


class PDFAdapterTests(unittest.TestCase):
    def assert_error(self, code, action, status=None):
        with self.assertRaises(APIError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def require_tools(self):
        if not all(pdf._cli_paths()):
            self.skipTest('Installed pdfinfo and pdftotext are required for native fixture execution')

    def test_real_native_pages_numbers_source_hash_and_evidence(self):
        self.require_tools()
        raw = native_pdf([['Synthetic native fixture', 'Pressure: 0.68 MPa', 'Flow: 19.25'],
                          ['Second page actual number: 31.7']])
        source = claim(raw)
        result = pdf.parse_pdf(source)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['processor_version'], 'native-pdf-text-v1')
        self.assertIsNone(result['model_used'])
        self.assertEqual(result['interpretation'], pdf.INTERPRETATION)
        self.assertEqual(len(result['segments']), 2)
        first, second = result['segments']
        self.assertIn('Pressure: 0.68 MPa', first['quote'])
        self.assertIn('Flow: 19.25', first['quote'])
        self.assertIn('31.7', second['quote'])
        self.assertEqual([s['locator']['page'] for s in result['segments']], [1, 2])
        self.assertEqual(pdf.parse_pdf(source), result)
        for segment in result['segments']:
            self.assertEqual(segment['source_sha256'], digest(raw))
            self.assertEqual(segment['fields'], {})
            self.assertEqual(segment['evidence'], dict(asset_id=source['asset'], source_version=1,
                analysis_version=result['analysis_version'], segment_id=segment['id']))
            expected_id = digest(dict(asset=source['asset'], version=1,
                analysis=result['analysis_version'], location=segment['locator']))[:32]
            self.assertEqual(segment['id'], expected_id)
        self.assertEqual(source['raw'], raw)

    def test_layout_lines_blanks_spaces_and_blocks_are_preserved(self):
        source = claim(native_pdf([['contract fixture']]))
        extracted = b'  Item       Value\n\n  Pressure   0.68\n  Flow       19.25\n\f'
        with patch.object(pdf, '_run_cli', side_effect=[b'Pages: 1\n', extracted]), \
             patch.object(pdf, '_cli_paths', return_value=('pdfinfo', 'pdftotext')):
            result = pdf.parse_pdf(source)
        segment = result['segments'][0]
        self.assertEqual(segment['quote'], extracted[:-2].decode())
        self.assertEqual(segment['locator'], dict(kind='pdf_page', page=1,
                                                  line_start=1, line_end=4))
        lines = ['x' * 4096, 'y' * 4096, 'z' * 3]
        with patch.object(pdf, '_run_cli', side_effect=[b'Pages: 1\n',
              ('\n'.join(lines) + '\n\f').encode()]), \
             patch.object(pdf, '_cli_paths', return_value=('pdfinfo', 'pdftotext')):
            segments = pdf.parse_pdf(source)['segments']
        self.assertEqual([(s['locator']['line_start'], s['locator']['line_end'])
                          for s in segments], [(1, 1), (2, 3)])
        self.assertEqual('\n'.join(s['quote'] for s in segments), '\n'.join(lines))

    def test_empty_text_layer_blocks_ocr_and_keeps_real_page_number(self):
        self.require_tools()
        result = pdf.parse_pdf(claim(native_pdf([[], []])))
        self.assertEqual((result['state'], result['error']), ('blocked', 'ocr_unavailable'))
        result = pdf.parse_pdf(claim(native_pdf([[], ['Actual page two 42']])))
        self.assertEqual(result['segments'][0]['locator']['page'], 2)

    def test_corrupted_pdf_has_fixed_error_without_source_or_path(self):
        self.require_tools()
        self.assert_error('pdf_invalid', lambda: pdf.parse_pdf(claim(b'%PDF-private-broken-path')), 422)

    def test_whole_original_source_byte_and_integrity_limits(self):
        self.assert_error('source_byte_limit', lambda: pdf.parse_pdf(claim(b'x' * (pdf.MAX_PDF_BYTES + 1))), 413)
        self.assert_error('source_byte_limit', lambda: pdf.parse_pdf(claim(b'')), 413)
        source = claim(native_pdf([['contract fixture']]))
        source['sha'] = '0' * 64
        self.assert_error('source_integrity_failed', lambda: pdf.parse_pdf(source), 409)

    def test_page_limit_rejects_whole_original_without_extracting_subset(self):
        self.require_tools()
        source = claim(native_pdf([['Contract fixture']] * 33))
        self.assert_error('pdf_page_limit', lambda: pdf.parse_pdf(source), 413)
        with patch.object(pdf, '_run_cli', return_value=b'Pages: 33\n') as run, \
             patch.object(pdf, '_cli_paths', return_value=('pdfinfo', 'pdftotext')):
            self.assert_error('pdf_page_limit', lambda: pdf.parse_pdf(source))
        self.assertEqual(run.call_count, 1)

    def test_exact_boundaries_accept_complete_source_pages_and_line(self):
        self.require_tools()
        source = claim(native_pdf([['Contract fixture']] * 32))
        self.assertEqual(len(pdf.parse_pdf(source)['segments']), 32)
        raw = native_pdf([['Contract fixture']])
        source = claim(raw + b' ' * (pdf.MAX_PDF_BYTES - len(raw)))
        seen = []
        def run(argv, maximum=pdf.MAX_OUTPUT_BYTES):
            path = Path(argv[-1] if argv[0] == 'pdfinfo' else argv[-2])
            seen.append(path.read_bytes())
            return b'Pages: 1\n' if argv[0] == 'pdfinfo' else b'x' * 8192 + b'\n\f'
        with patch.object(pdf, '_run_cli', side_effect=run), \
             patch.object(pdf, '_cli_paths', return_value=('pdfinfo', 'pdftotext')):
            result = pdf.parse_pdf(source)
        self.assertEqual(len(result['segments'][0]['quote']), 8192)
        self.assertEqual(seen, [source['raw'], source['raw']])
        self.assertEqual(result['segments'][0]['source_sha256'], digest(source['raw']))

    def test_missing_tools_and_spawn_failure_are_blocked(self):
        source = claim(native_pdf([['Contract fixture']]))
        with patch.object(pdf, '_cli_paths', return_value=(None, 'pdftotext')):
            self.assertEqual(pdf.parse_pdf(source)['error'], 'capability_unavailable')
        with patch.object(pdf, '_cli_paths', return_value=('/missing-pdfinfo', '/missing-pdftotext')):
            self.assertEqual(pdf.parse_pdf(source)['state'], 'blocked')

    def test_cli_output_and_timeout_are_bounded_without_stderr_leaks(self):
        self.assertEqual(pdf.CLI_TIMEOUT_SECONDS, 5)
        self.assert_error('pdf_output_limit', lambda: pdf._run_cli(
            [sys.executable, '-c', 'import os;os.write(1,b"x"*1025)'], 1024), 413)
        with patch.object(pdf, 'CLI_TIMEOUT_SECONDS', 0.05):
            self.assert_error('pdf_cli_timeout', lambda: pdf._run_cli(
                [sys.executable, '-c', 'import time;time.sleep(1)']), 408)
        self.assert_error('pdf_invalid', lambda: pdf._run_cli([sys.executable, '-c',
            'import sys;sys.stderr.write("private source body /private/path");sys.exit(1)']), 422)

    def test_private_temporary_original_is_complete_and_removed(self):
        source = claim(native_pdf([['Contract fixture']]))
        paths = []
        def run(argv, maximum=pdf.MAX_OUTPUT_BYTES):
            path = Path(argv[-1] if argv[0] == 'pdfinfo' else argv[-2])
            paths.append(path)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), source['raw'])
            return b'Pages: 1\n' if argv[0] == 'pdfinfo' else b'Contract fixture\n\f'
        with patch.object(pdf, '_run_cli', side_effect=run), \
             patch.object(pdf, '_cli_paths', return_value=('pdfinfo', 'pdftotext')):
            pdf.parse_pdf(source)
        self.assertFalse(any(path.exists() for path in paths))

    def test_invalid_page_output_line_and_segment_limits(self):
        source = claim(native_pdf([['Contract fixture']]))
        with patch.object(pdf, '_cli_paths', return_value=('pdfinfo', 'pdftotext')):
            for text, code in [(b'x\fmore\f', 'pdf_page_output_mismatch'),
                               (b'\x00\f', 'pdf_text_invalid'),
                               (b'\xff\f', 'pdf_text_invalid'),
                               (b'x' * 8193 + b'\f', 'extraction_limit')]:
                with self.subTest(code=code), patch.object(pdf, '_run_cli',
                        side_effect=[b'Pages: 1\n', text]):
                    self.assert_error(code, lambda: pdf.parse_pdf(source))
            with patch.dict(LIMITS, segments=2), patch.object(pdf, '_run_cli',
                    side_effect=[b'Pages: 3\n', b'one\ftwo\fthree\f']):
                self.assert_error('extraction_limit', lambda: pdf.parse_pdf(source), 413)
            with patch.object(pdf, '_run_cli', return_value=b'No pages metadata'):
                self.assert_error('pdf_invalid', lambda: pdf.parse_pdf(source), 422)

    def test_serialized_extraction_also_obeys_output_limit(self):
        source = claim(native_pdf([['Contract fixture']]))
        with patch.object(pdf, '_cli_paths', return_value=('pdfinfo', 'pdftotext')), \
             patch.object(pdf, '_run_cli', side_effect=[b'Pages: 1\n', b'Contract fixture\n\f']), \
             patch.object(pdf, 'MAX_OUTPUT_BYTES', 100):
            self.assert_error('extraction_limit', lambda: pdf.parse_pdf(source), 413)


if __name__ == '__main__':
    unittest.main()
