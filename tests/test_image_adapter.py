"""Resource and provenance contracts; synthetic fixtures are not image-quality gold."""
from pathlib import Path
import os
import stat
import struct
import sys
import unittest
import zlib
from unittest.mock import patch

from navigator.contracts import APIError, LIMITS, digest
from navigator import image_adapter as image


def png(width=20, height=20):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff' * width * 3) * height)) + chunk(b'IEND', b'')


def claim(raw):
    return dict(raw=raw, sha=digest(raw), asset='a' * 32, version=1, meta=dict(media_type='image/png'))


def bmp():
    # Two top-down pixels: red then blue. BMP padding is excluded from RGB hash.
    pixels = b'\0\0\xff\xff\0\0\0\0'
    return b'BM' + struct.pack('<IHHI', 54 + len(pixels), 0, 0, 54) + struct.pack('<IiiHHIIIIII', 40, 2, -1, 1, 24, 0, len(pixels), 0, 0, 0, 0) + pixels


def tsv(words=()):
    header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    page = '1\t1\t0\t0\t0\t0\t0\t0\t20\t20\t-1\t\n'
    rows = ''.join('5\t1\t1\t1\t1\t%d\t1\t2\t10\t5\t%s\t%s\n' % (i, confidence, quote) for i, (quote, confidence) in enumerate(words, 1))
    return (header + page + rows).encode()


class ImageAdapterTests(unittest.TestCase):
    def assert_error(self, code, action, status=None):
        with self.assertRaises(APIError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def test_encoded_source_integrity_dimensions_and_limits_before_cli(self):
        self.assertEqual(image._dimensions(png(), 'image/png'), (20, 20))
        with patch.object(image, '_cli_paths') as cli:
            self.assert_error('source_byte_limit', lambda: image.parse_image(claim(b'')), 413)
            self.assert_error('source_byte_limit', lambda: image.parse_image(claim(b'x' * (image.MAX_IMAGE_BYTES + 1))), 413)
            source = claim(png()); source['sha'] = '0' * 64
            self.assert_error('source_integrity_failed', lambda: image.parse_image(source), 409)
            self.assert_error('image_pixel_limit', lambda: image.parse_image(claim(png(2001, 1000))), 413)
            self.assert_error('image_invalid', lambda: image.parse_image(claim(b'bad header')), 422)
            cli.assert_not_called()
        for raw in [b'\xff\xd8', b'\xff\xd8\xff\xc0\x00\x01', b'\xff\xd8\xff\xc0\x00\x08\x08\x00\x01\x00\x01\x01']:
            if len(raw) > 10:
                self.assertEqual(image._dimensions(raw, 'image/jpeg'), (1, 1))
            else:
                self.assert_error('image_invalid', lambda: image._dimensions(raw, 'image/jpeg'))

    def test_pixel_hash_is_dimensions_and_actual_top_down_rgb(self):
        expected = digest(b'navigator-rgb-v1\0' + struct.pack('>II', 2, 1) + b'\xff\0\0\0\0\xff')
        self.assertEqual(image._pixel_digest(bmp(), (2, 1)), expected)
        signature = image._pixel_metadata(bmp(), (2, 1))['visual_signature']
        self.assertEqual(signature['format'], 'rgb-grid16-v1')
        self.assertEqual(len(signature['values']), 256)
        self.assertEqual(signature['values'][:16], [76] * 8 + [29] * 8)
        self.assert_error('image_decode_invalid', lambda: image._pixel_digest(bmp(), (1, 2)))
        self.assert_error('image_decode_invalid', lambda: image._pixel_digest(bmp()[:-1], (2, 1)))
        with patch.object(image, 'MAX_DECODED_BYTES', 2):
            self.assert_error('image_decode_limit', lambda: image._pixel_digest(bmp(), (2, 1)), 413)

    def test_confidence_floor_does_not_turn_ocr_into_human_verification(self):
        words, rejected = image._words(tsv([('OCR42', 40), ('bad', 39.999), ('>', 99), ('x', 99)]), 20, 20)
        self.assertEqual(words, [('OCR42', 40.0, [1, 2, 10, 5])])
        self.assertEqual(rejected, 3)
        for output in [b'bad', tsv([('word', 'nan')]), tsv([('word', 101)]), tsv([('word', -1)]), tsv([('word', 44)]).replace(b'\t10\t5\t', b'\t999\t5\t')]:
            self.assert_error('image_ocr_output_invalid', lambda: image._words(output, 20, 20))
        with patch.dict(LIMITS, segments=0):
            self.assert_error('extraction_limit', lambda: image._words(tsv([('word', 40)]), 20, 20), 413)

    def test_cli_bytes_timeout_private_stderr_thread_limit_and_child_cleanup(self):
        self.assertEqual(image.CLI_TIMEOUT_SECONDS, 15)
        self.assertEqual(image._run_cli([sys.executable, '-c', 'import os;print(os.environ["OMP_THREAD_LIMIT"])']), b'1\n')
        self.assert_error('image_output_limit', lambda: image._run_cli([sys.executable, '-c', 'import os;os.write(1,b"x"*1025)'], 1024), 413)
        with patch.object(image, 'CLI_TIMEOUT_SECONDS', 0.05):
            self.assert_error('image_cli_timeout', lambda: image._run_cli([sys.executable, '-c', 'import time;time.sleep(1)']), 408)
            self.assert_error('image_cli_timeout', lambda: image._run_cli([sys.executable, '-c', 'import os,time;pid=os.fork();time.sleep(10) if pid==0 else os._exit(0)']), 408)
        self.assert_error('image_cli_failed', lambda: image._run_cli([sys.executable, '-c', 'import sys;sys.stderr.write("private-source /private/path");sys.exit(1)']), 422)

    def test_missing_capability_blocks_but_blank_pixels_have_ocr_unknown(self):
        with patch.object(image, '_cli_paths', return_value=(None, None)):
            self.assertEqual(image.parse_image(claim(png()))['error'], 'capability_unavailable')
        if not all(image._cli_paths()):
            self.skipTest('Existing sips and Tesseract required')
        result = image.parse_image(claim(png()))
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['ocr_state'], 'unknown')
        self.assertEqual(result['segments'], [])
        self.assertEqual(result['visual_signature']['values'], [255] * 256)
        self.assertEqual(result['processor_detail']['thread_limit'], 1)
        self.assertEqual(len(result['pixel_sha256']), 64)

    def test_evidence_pins_and_temporary_original_removed(self):
        # Stub only native boundaries. The real original-document run is recorded separately.
        source, paths = claim(png()), []
        details = dict(tesseract_version='tesseract 5.5.2', traineddata_sha256='b' * 64,
            traineddata_bytes=100, language='eng', psm=11, pixel_hash_format='navigator-rgb-v1',
            thread_limit=1, confidence_threshold=40)
        def run(argv, maximum=image.MAX_OUTPUT_BYTES, **kwargs):
            path = Path(argv[4] if argv[0] == 'sips' else argv[1])
            paths.append(path)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            if argv[0] == 'sips':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.read_bytes(), source['raw'])
                Path(kwargs['output_path']).write_bytes(b'bounded stub')
                return b''
            self.assertEqual(path.read_bytes(), b'bounded stub')
            return tsv([('Fixture42', 85)])
        pixels = dict(pixel_sha256='c' * 64, visual_signature=dict(format='rgb-grid16-v1', values=[0] * 256))
        with patch.object(image, '_cli_paths', return_value=('sips', 'tesseract')), patch.object(image, '_installed_model', return_value=('/installed', details)), patch.object(image, '_run_cli', side_effect=run), patch.object(image, '_pixel_metadata', return_value=pixels):
            result = image.parse_image(source)
        segment = result['segments'][0]
        self.assertEqual(segment['quote'], 'Fixture42')
        self.assertEqual(segment['claim_kind'], 'ocr_observation')
        self.assertTrue(segment['review_required'])
        self.assertEqual(segment['fields'], {})
        self.assertEqual(segment['source_sha256'], digest(source['raw']))
        self.assertEqual(segment['evidence'], dict(asset_id='a' * 32, source_version=1,
            analysis_version=result['analysis_version'], segment_id=segment['id']))
        self.assertEqual(segment['locator'], dict(kind='image_region', bbox=[1, 2, 10, 5], image_width=20, image_height=20))
        self.assertFalse(any(path.exists() for path in paths))


if __name__ == '__main__':
    unittest.main()
