"""Bounded local pixel OCR, never a caption, diagram inference, or verified quote.

Only existing sips and Tesseract/eng installations are used. Every word remains
an OCR observation requiring review, including words with high confidence.
"""
import csv
import hashlib
import io
import math
import os
import re
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time

from .contracts import APIError, LIMITS, SCHEMAS, canonical, digest, validate

PROCESSOR = 'tesseract-image-v1'
MODEL = 'local:tesseract:eng'
INTERPRETATION = 'ocr_text_only_review_required_no_visual_semantics'
MAX_IMAGE_BYTES = 1024 * 1024
MAX_PIXELS = 2_000_000
MAX_OUTPUT_BYTES = 256 * 1024
MAX_DECODED_BYTES = MAX_PIXELS * 4 + 4096
CLI_TIMEOUT_SECONDS = 15
MIN_CONFIDENCE = 40
PSM = 11


class _CapabilityUnavailable(Exception):
    pass


def _cli_paths():
    return shutil.which('sips'), shutil.which('tesseract')


def _run_cli(argv, maximum=MAX_OUTPUT_BYTES, *, deadline=None, output_path=None):
    """Bound execution, pipe bytes and a native-decoder output file; discard stderr."""
    deadline = deadline if deadline is not None else time.monotonic() + CLI_TIMEOUT_SECONDS
    environment = os.environ.copy()
    environment['OMP_THREAD_LIMIT'] = '1'
    environment['OMP_NUM_THREADS'] = '1'
    environment['VECLIB_MAXIMUM_THREADS'] = '1'
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
            close_fds=True, start_new_session=True, env=environment)
    except OSError:
        raise _CapabilityUnavailable() from None
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise APIError(408, 'image_cli_timeout')
                if output_path and os.path.exists(output_path) and os.stat(output_path).st_size > MAX_DECODED_BYTES:
                    raise APIError(413, 'image_decode_limit')
                for key, _ in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fd, min(65536, maximum + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > maximum:
                            raise APIError(413, 'image_output_limit')
            if process.returncode:
                raise APIError(422, 'image_cli_failed')
            if output_path and os.stat(output_path).st_size > MAX_DECODED_BYTES:
                raise APIError(413, 'image_decode_limit')
        return bytes(output)
    finally:
        # Includes descendants inheriting the pipe, even after the leader exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def _dimensions(raw, media):
    if media == 'image/png':
        if len(raw) < 33 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[8:16] != b'\x00\x00\x00\rIHDR':
            raise APIError(422, 'image_invalid')
        width, height = struct.unpack('>II', raw[16:24])
    elif media == 'image/jpeg':
        if raw[:2] != b'\xff\xd8':
            raise APIError(422, 'image_invalid')
        index, found = 2, None
        while index < len(raw):
            if raw[index] != 255:
                raise APIError(422, 'image_invalid')
            while index < len(raw) and raw[index] == 255:
                index += 1
            if index >= len(raw):
                break
            marker = raw[index]
            index += 1
            if marker in (0xda, 0xd9):
                break
            if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7:
                continue
            if index + 2 > len(raw):
                raise APIError(422, 'image_invalid')
            length = int.from_bytes(raw[index:index + 2], 'big')
            if length < 2 or index + length > len(raw):
                raise APIError(422, 'image_invalid')
            if marker in (0xc0, 0xc1, 0xc2):
                if length < 8 or found is not None:
                    raise APIError(422, 'image_invalid')
                height, width = struct.unpack('>HH', raw[index + 3:index + 7])
                found = width, height
            index += length
        if found is None:
            raise APIError(422, 'image_invalid')
        width, height = found
    else:
        raise APIError(415, 'image_media_unsupported')
    if not width or not height:
        raise APIError(422, 'image_invalid')
    if width * height > MAX_PIXELS:
        raise APIError(413, 'image_pixel_limit')
    return width, height


def _pixel_metadata(raw, expected, deadline=None):
    """Hash actual RGB and average 16x16 luma regions; no semantic inference."""
    if len(raw) > MAX_DECODED_BYTES:
        raise APIError(413, 'image_decode_limit')
    if len(raw) < 54 or raw[:2] != b'BM':
        raise APIError(422, 'image_decode_invalid')
    offset, dib = struct.unpack_from('<II', raw, 10)
    width, signed_height, planes, bits, compression = struct.unpack_from('<iiHHI', raw, 18)
    height = abs(signed_height)
    if dib < 40 or offset < 14 + dib or (width, height) != expected or planes != 1 or bits not in (24, 32) or compression != 0:
        raise APIError(422, 'image_decode_invalid')
    stride = ((width * bits + 31) // 32) * 4
    if offset + stride * height > len(raw):
        raise APIError(422, 'image_decode_invalid')
    result = hashlib.sha256(b'navigator-rgb-v1\0' + struct.pack('>II', width, height))
    # For dimensions below 16, repeat nearest available source pixels. At larger
    # sizes every source pixel contributes to precisely one region average.
    xspans = [(i * width // 16, min(width, max(i * width // 16 + 1, (i + 1) * width // 16))) for i in range(16)]
    yspans = [(i * height // 16, min(height, max(i * height // 16 + 1, (i + 1) * height // 16))) for i in range(16)]
    sums, counts = [0] * 256, [0] * 256
    rows = range(height - 1, -1, -1) if signed_height > 0 else range(height)
    for y, row in enumerate(rows):
        if deadline is not None and time.monotonic() > deadline:
            raise APIError(408, 'image_cli_timeout')
        pixels = raw[offset + row * stride:offset + row * stride + width * (bits // 8)]
        rgb = bytearray(width * 3)
        step = bits // 8
        for column in range(width):
            source = column * step
            rgb[column * 3:column * 3 + 3] = pixels[source:source + 3][::-1]
        result.update(rgb)
        gray = [(299 * rgb[i] + 587 * rgb[i + 1] + 114 * rgb[i + 2] + 500) // 1000 for i in range(0, len(rgb), 3)]
        for gy, (y0, y1) in enumerate(yspans):
            if y0 <= y < y1:
                for gx, (x0, x1) in enumerate(xspans):
                    index = gy * 16 + gx
                    sums[index] += sum(gray[x0:x1])
                    counts[index] += x1 - x0
    signature = [(value + count // 2) // count for value, count in zip(sums, counts)]
    return dict(pixel_sha256=result.hexdigest(), visual_signature=dict(format='rgb-grid16-v1', values=signature))


def _pixel_digest(raw, expected):
    return _pixel_metadata(raw, expected)['pixel_sha256']


def compute_image_signature(raw, media):
    """Bounded OCR-free query processing using the existing local sips decoder."""
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise APIError(413, 'source_byte_limit')
    width, height = _dimensions(raw, media)
    sips, _ = _cli_paths()
    if not sips:
        return dict(state='blocked', error='capability_unavailable')
    deadline = time.monotonic() + CLI_TIMEOUT_SECONDS
    try:
        with tempfile.TemporaryDirectory(prefix='navigator-image-query-') as temporary:
            os.chmod(temporary, 0o700)
            path = os.path.join(temporary, 'input.jpg' if media == 'image/jpeg' else 'input.png')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as source:
                source.write(raw)
            decoded = os.path.join(temporary, 'pixels.bmp')
            _run_cli([sips, '-s', 'format', 'bmp', path, '--out', decoded], 8192,
                     deadline=deadline, output_path=decoded)
            with open(decoded, 'rb') as pixels:
                metadata = _pixel_metadata(pixels.read(MAX_DECODED_BYTES + 1), (width, height), deadline)
    except _CapabilityUnavailable:
        return dict(state='blocked', error='capability_unavailable')
    return dict(state='succeeded', processor_version='local-pixel-grid-v1', model_used=None,
                source_sha256=digest(raw), image_width=width, image_height=height, **metadata)


def _installed_model(tesseract, deadline):
    version = _run_cli([tesseract, '--version'], 8192, deadline=deadline).decode('utf-8').splitlines()[0]
    languages = _run_cli([tesseract, '--list-langs'], 16384, deadline=deadline).decode('utf-8')
    match = re.search(r'List of available languages in "([^"\n]+)"', languages)
    if not match or 'eng' not in languages.splitlines():
        raise _CapabilityUnavailable()
    directory = match.group(1)
    path = os.path.join(directory, 'eng.traineddata')
    try:
        with open(path, 'rb') as model:
            info = os.fstat(model.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 32 * 1024 * 1024:
                raise _CapabilityUnavailable()
            hasher = hashlib.sha256()
            size = 0
            while block := model.read(65536):
                size += len(block)
                if size > 32 * 1024 * 1024 or time.monotonic() > deadline:
                    raise _CapabilityUnavailable()
                hasher.update(block)
    except OSError:
        raise _CapabilityUnavailable() from None
    return directory, dict(tesseract_version=version, traineddata_sha256=hasher.hexdigest(),
                          traineddata_bytes=size, language='eng', psm=PSM,
                          pixel_hash_format='navigator-rgb-v1', thread_limit=1,
                          confidence_threshold=MIN_CONFIDENCE)


def _words(tsv, width, height):
    try:
        text = tsv.decode('utf-8')
        if '\x00' in text:
            raise ValueError()
        reader = csv.DictReader(io.StringIO(text), delimiter='\t', quoting=csv.QUOTE_NONE)
        if reader.fieldnames != ['level', 'page_num', 'block_num', 'par_num', 'line_num', 'word_num', 'left', 'top', 'width', 'height', 'conf', 'text']:
            raise ValueError()
        words, rejected, saw_page = [], 0, False
        for row in reader:
            level = int(row['level'])
            if level not in range(1, 6) or int(row['page_num']) != 1:
                raise ValueError()
            if level == 1:
                if saw_page:
                    raise ValueError()
                saw_page = True
                if int(row['width']) != width or int(row['height']) != height or int(row['page_num']) != 1:
                    raise ValueError()
            if level != 5:
                continue
            quote = row['text']
            confidence = float(row['conf'])
            bbox = [int(row[key]) for key in ('left', 'top', 'width', 'height')]
            x, y, w, h = bbox
            if not math.isfinite(confidence) or not 0 <= confidence <= 100 or x < 0 or y < 0 or w < 1 or h < 1 or x + w > width or y + h > height or len(quote) > 8192:
                raise ValueError()
            if confidence < MIN_CONFIDENCE or not any(character.isalnum() for character in quote) or len(quote.strip()) < 2:
                rejected += 1
                continue
            words.append((quote, confidence, bbox))
            if len(words) > LIMITS['segments']:
                raise APIError(413, 'extraction_limit')
        if not saw_page:
            raise ValueError()
        return words, rejected
    except (UnicodeError, ValueError, TypeError, KeyError, csv.Error):
        raise APIError(422, 'image_ocr_output_invalid') from None


def _blocked(error, **metadata):
    return dict(state='blocked', error=error, segments=[], capability='local_image_ocr',
                processor_version=PROCESSOR, model_used=None,
                interpretation=INTERPRETATION, **metadata)


def parse_image(claim):
    raw = claim['raw']
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise APIError(413, 'source_byte_limit')
    if digest(raw) != claim['sha']:
        raise APIError(409, 'source_integrity_failed')
    media = claim['meta']['media_type']
    width, height = _dimensions(raw, media)
    sips, tesseract = _cli_paths()
    if not sips or not tesseract:
        return _blocked('capability_unavailable')
    deadline = time.monotonic() + CLI_TIMEOUT_SECONDS
    try:
        directory, details = _installed_model(tesseract, deadline)
        with tempfile.TemporaryDirectory(prefix='navigator-image-') as temporary:
            os.chmod(temporary, 0o700)
            path = os.path.join(temporary, 'input.jpg' if media == 'image/jpeg' else 'input.png')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as source:
                source.write(raw)
            decoded = os.path.join(temporary, 'pixels.bmp')
            _run_cli([sips, '-s', 'format', 'bmp', path, '--out', decoded], 8192,
                     deadline=deadline, output_path=decoded)
            with open(decoded, 'rb') as pixels:
                pixel_metadata = _pixel_metadata(pixels.read(MAX_DECODED_BYTES + 1), (width, height), deadline)
            extracted = _run_cli([tesseract, decoded, 'stdout', '--tessdata-dir', directory,
                '-l', 'eng', '--psm', str(PSM), 'tsv'], deadline=deadline)
    except _CapabilityUnavailable:
        return _blocked('capability_unavailable')
    words, rejected = _words(extracted, width, height)
    details.update(rejected_word_count=rejected, accepted_word_count=len(words))
    metadata = dict(**pixel_metadata, extracted_sha256=digest(extracted), processor_detail=details,
                    ocr_state='observations' if words else 'unknown', image_width=width, image_height=height)
    analysis = PROCESSOR + ':' + digest(dict(source_sha256=claim['sha'], processor=PROCESSOR, **metadata))
    segments = []
    for quote, confidence, bbox in words:
        locator = dict(kind='image_region', bbox=bbox, image_width=width, image_height=height)
        identifier = digest(dict(asset=claim['asset'], version=claim['version'], analysis=analysis, location=locator))[:32]
        segments.append(dict(id=identifier, quote=quote, locator=locator, fields={},
            evidence=dict(asset_id=claim['asset'], source_version=claim['version'], analysis_version=analysis, segment_id=identifier),
            source_sha256=claim['sha'], claim_kind='ocr_observation', model_used=MODEL,
            confidence=confidence, review_required=True))
    result = dict(state='succeeded', analysis_version=analysis, processor_version=PROCESSOR,
                  model_used=MODEL, interpretation=INTERPRETATION, segments=segments, **metadata)
    if len(canonical(result).encode('utf-8')) > MAX_OUTPUT_BYTES:
        raise APIError(413, 'extraction_limit')
    validate({key: value for key, value in result.items() if key != 'state'}, SCHEMAS['Extraction'])
    return result
