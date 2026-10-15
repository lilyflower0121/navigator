"""Bounded inspection-checklist layout from native PDF word coordinates.

Assignment windows are inferred, not detected ruling lines. Empty text-layer
cells do not establish visually blank cells, completed inspection, or site pass.
"""
import math
import os
import re
import selectors
import signal
import shutil
import statistics
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from .contracts import (APIError, SCHEMAS, array, canonical, digest, integer,
                        obj, string, validate)
from .store import uid

PROCESSOR = 'native-checklist-word-layout-v1'
MAX_BYTES, MAX_XML, MAX_PAGES = 4 * 1024 * 1024, 1024 * 1024, 32
TIMEOUT = 15
HEADERS = ['点検事項', '点検の内容', '良否', '是正内容', '確認']
NOTICE = ('点検チェックリストの原PDF文字座標から推定した列・行の割当窓です。'
          '実罫線の検出、一般表の理解、OCR、画像・手書きの有無判定ではありません。'
          'empty_text_layerは文字未検出であり、未記入認定や現場合格を意味しません。')

NUMBER = dict(type='number', minimum=0, maximum=14400)
BOX = array(NUMBER, 4, 4)
WORD = obj(dict(text=string(8192), bbox=BOX))
COLUMN = obj(dict(index=integer(0, 4), name=string(40), header_quote=string(200),
                  header_bbox=BOX, assignment_x=array(NUMBER, 2, 2)))
CELL = obj(dict(column=integer(0, 4), text=string(8192, 0),
    state=dict(type='string', enum=['empty_text_layer', 'text_present']),
    words=array(WORD, 512)))
ROW = obj(dict(number=integer(1, 50), assignment_y=array(NUMBER, 2, 2),
               cells=array(CELL, 5, 5)))
TABLE_INPUT = obj(dict(asset_id=SCHEMAS['Evidence']['properties']['asset_id'],
    source_version=integer(1, 100), page=integer(1, MAX_PAGES),
    table_kind=dict(type='string', const='inspection-checklist')))
TABLE_EXTRACTION = obj(dict(id=SCHEMAS['Evidence']['properties']['asset_id'],
    **TABLE_INPUT['properties'], source_sha256=string(64, 64),
    extraction_sha256=string(64, 64), processor_version=dict(type='string', const=PROCESSOR),
    status=dict(type='string', enum=['extracted', 'hold']), reason=string(100),
    page_width=NUMBER, page_height=NUMBER,
    coordinate_unit=dict(type='string', const='pdf_points_top_left'),
    geometry_kind=dict(type='string', const='inferred_from_native_word_positions'),
    geometry_review_required=dict(type='boolean', const=True),
    columns=array(COLUMN, 5), rows=array(ROW, 50),
    column_count=integer(0, 5), row_count=integer(0, 50),
    model_used=dict(type='null'), business_approval=dict(type='string', const='unassessed'),
    notice=string(500)))
TABLE_SCHEMAS = dict(TableInput=TABLE_INPUT, TableExtraction=TABLE_EXTRACTION)


def _run(argv, maximum, deadline):
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, shell=False,
            close_fds=True,start_new_session=True)
    except OSError:
        raise APIError(503, 'table_capability_unavailable') from None
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise APIError(408, 'table_cli_timeout')
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, maximum + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > maximum:
                            raise APIError(413, 'table_output_limit')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIError(408, 'table_cli_timeout')
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise APIError(408, 'table_cli_timeout') from None
            if code:
                raise APIError(422, 'table_pdf_invalid')
        return bytes(output)
    finally:
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def _native_xml(raw, page):
    if not raw or len(raw) > MAX_BYTES:
        raise APIError(413, 'source_byte_limit')
    pdfinfo, pdftotext = shutil.which('pdfinfo'), shutil.which('pdftotext')
    if not pdfinfo or not pdftotext:
        raise APIError(503, 'table_capability_unavailable')
    deadline = time.monotonic() + TIMEOUT
    with tempfile.TemporaryDirectory(prefix='navigator-table-') as directory:
        os.chmod(directory, 0o700)
        path = os.path.join(directory, 'input.pdf')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        info = _run([pdfinfo, '-enc', 'UTF-8', path], 65536, deadline)
        match = re.search(rb'^Pages:\s+(\d+)\s*$', info, re.MULTILINE)
        if not match:
            raise APIError(422, 'table_pdf_invalid')
        pages = int(match[1])
        if not 1 <= pages <= MAX_PAGES:
            raise APIError(413, 'pdf_page_limit')
        if page > pages:
            raise APIError(422, 'table_page_not_found')
        return _run([pdftotext, '-f', str(page), '-l', str(page),
                     '-bbox-layout', '-enc', 'UTF-8', path, '-'], MAX_XML, deadline)


def _box(element, width, height):
    try:
        bbox = [float(element.attrib[k]) for k in ('xMin', 'yMin', 'xMax', 'yMax')]
        if (not all(math.isfinite(n) for n in bbox) or
                not 0 <= bbox[0] < bbox[2] <= width or
                not 0 <= bbox[1] < bbox[3] <= height):
            raise ValueError('invalid_bbox')
        return bbox
    except (KeyError, ValueError, TypeError):
        raise APIError(422, 'table_layout_invalid') from None


def _clean(text):
    # Annotation suffixes are not column names. Preserve their original words
    # in header_quote; this normalization is used only for this explicit grammar.
    return re.sub(r'[（(]注[0-9]+[）)]?', '', re.sub(r'\s+', '', text))


def parse_layout(xml):
    if len(xml) > MAX_XML:
        raise APIError(413, 'table_output_limit')
    if b'<!ENTITY' in xml.upper():
        raise APIError(422, 'table_layout_invalid')
    try:
        document = ET.fromstring(xml)
        pages = list(document.iter('{http://www.w3.org/1999/xhtml}page'))
        if len(pages) != 1:
            raise ValueError('page_count')
        page = pages[0]
        width, height = float(page.attrib['width']), float(page.attrib['height'])
        if not all(math.isfinite(x) and 0 < x <= 14400 for x in (width, height)):
            raise ValueError('page_size')
    except (ET.ParseError, KeyError, ValueError, TypeError):
        raise APIError(422, 'table_layout_invalid') from None
    words = []
    for element in page.iter('{http://www.w3.org/1999/xhtml}word'):
        text = element.text or ''
        if not text.strip() or '\x00' in text or len(text) > 8192:
            raise APIError(422, 'table_layout_invalid')
        words.append(dict(text=text, bbox=_box(element, width, height)))
        if len(words) > 4096:
            raise APIError(413, 'table_word_limit')
    base = dict(page_width=width, page_height=height, columns=[], rows=[],
                column_count=0, row_count=0, status='hold', reason='table_header_not_supported')
    groups = []
    for word in sorted(words, key=lambda w: (w['bbox'][1], w['bbox'][0])):
        if not groups or abs(groups[-1][0]['bbox'][1] - word['bbox'][1]) > 1.5:
            groups.append([])
        groups[-1].append(word)
    headers = []
    for group in groups:
        ordered = sorted(group, key=lambda w: w['bbox'][0])
        if ''.join(_clean(w['text']) for w in ordered) != ''.join(HEADERS):
            continue
        chunks, current, index = [], [], 0
        for word in ordered:
            if index == len(HEADERS):
                # A separately emitted trailing annotation has no normalized
                # header letters, but its original quote still belongs here.
                chunks[-1].append(word)
                continue
            if not _clean(word['text']) and not current and chunks:
                chunks[-1].append(word)
                continue
            current.append(word)
            text = ''.join(_clean(w['text']) for w in current)
            if text == HEADERS[index]:
                chunks.append(current)
                current, index = [], index + 1
        if len(chunks) == len(HEADERS) and not current:
            headers.append(chunks)
    if len(headers) != 1:
        if len(headers) > 1:
            base['reason'] = 'table_header_ambiguous'
        return base
    chunks = headers[0]
    boxes = [[min(w['bbox'][0] for w in chunk), min(w['bbox'][1] for w in chunk),
              max(w['bbox'][2] for w in chunk), max(w['bbox'][3] for w in chunk)]
             for chunk in chunks]
    header_bottom = max(b[3] for b in boxes)
    # Midpoints between header glyph boxes provide assignment windows only.
    # They are deliberately labelled as inferred, never as PDF ruling lines.
    boundaries = [0.0] + [(boxes[i][2] + boxes[i + 1][0]) / 2
                           for i in range(len(boxes) - 1)] + [width]
    if any(boxes[i][2]>boxes[i+1][0] for i in range(len(boxes)-1)) or any(a>=b for a,b in zip(boundaries,boundaries[1:])):
        base['reason']='table_column_geometry_ambiguous'
        return base
    body = [w for w in words if w['bbox'][1] > header_bottom]
    first = [w for w in body if w['bbox'][0] < boundaries[1]]
    numbered = []
    for word in sorted(first, key=lambda w: (w['bbox'][1], w['bbox'][0])):
        match = re.match(r'^([1-9][0-9]?)\s*(?=\D|$)', word['text'])
        if match:
            numbered.append((int(match[1]), word))
    if not numbered or len(numbered) > 50 or [n for n, _ in numbered] != list(range(1, len(numbered) + 1)):
        base['reason'] = 'table_numbered_rows_not_supported'
        return base
    # A number within a first-column line (e.g. a legal article reference)
    # cannot become a row anchor at a different horizontal position.
    if max(w['bbox'][0] for _, w in numbered) - min(w['bbox'][0] for _, w in numbered) > 3:
        base['reason'] = 'table_row_anchor_ambiguous'
        return base
    # Latin row numbers and Japanese text have slightly different font ascents;
    # the row starts at the whole baseline cluster, not the digit glyph's top.
    starts = [min(candidate['bbox'][1] for candidate in first
                  if abs(candidate['bbox'][1] - word['bbox'][1]) <= 3)
              for _, word in numbered]
    typical_height = statistics.median(w['bbox'][3] - w['bbox'][1] for _, w in numbered)
    last_label_words = [w for w in first if w['bbox'][1] >= starts[-1]]
    bottom = min(height, max(w['bbox'][3] for w in last_label_words) + typical_height * 2)
    ys = [(header_bottom + starts[0]) / 2]
    for previous, next_start in zip(starts, starts[1:]):
        prior = [w['bbox'][3] for w in first if previous <= w['bbox'][1] < next_start]
        ys.append((max(prior) + next_start) / 2)
    ys.append(bottom)
    if any(a >= b for a, b in zip(ys, ys[1:])):
        base['reason'] = 'table_row_geometry_ambiguous'
        return base
    columns = [dict(index=i, name=name, header_quote=' '.join(w['text'] for w in chunks[i]),
                    header_bbox=boxes[i], assignment_x=boundaries[i:i + 2])
               for i, name in enumerate(HEADERS)]
    rows = []
    for index, (number, _) in enumerate(numbered):
        cells = []
        for column in range(len(columns)):
            cell_words = [w for w in body if
                ys[index] <= (w['bbox'][1] + w['bbox'][3]) / 2 < ys[index + 1] and
                boundaries[column] <= (w['bbox'][0] + w['bbox'][2]) / 2 < boundaries[column + 1]]
            cell_words.sort(key=lambda w: (w['bbox'][1], w['bbox'][0]))
            if len(cell_words) > 512:
                raise APIError(413, 'table_cell_word_limit')
            line_groups = []
            for word in cell_words:
                if not line_groups or abs(line_groups[-1][0]['bbox'][1] - word['bbox'][1]) > 3:
                    line_groups.append([])
                line_groups[-1].append(word)
            text = '\n'.join(' '.join(w['text'] for w in sorted(group, key=lambda w: w['bbox'][0]))
                             for group in line_groups)
            if len(text) > 8192:
                raise APIError(413, 'table_cell_text_limit')
            cells.append(dict(column=column, text=text, words=cell_words,
                              state='text_present' if cell_words else 'empty_text_layer'))
        rows.append(dict(number=number, assignment_y=ys[index:index + 2], cells=cells))
    return dict(base, columns=columns, rows=rows, column_count=len(columns),
                row_count=len(rows), status='extracted', reason='scoped_native_word_layout')


class TableExtraction:
    def __init__(self, store, library):
        self.store, self.library = store, library

    def _value(self, db, tenant, body):
        validate(body, TABLE_INPUT)
        raw, sha = self.library.original(db, tenant, body['asset_id'], body['source_version'])
        row = db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',
                         (tenant, body['asset_id'], body['source_version'])).fetchone()
        import json
        if json.loads(row['meta'])['media_type'] != 'application/pdf':
            raise APIError(415, 'table_pdf_required')
        xml = _native_xml(raw, body['page'])
        return dict(**body, **parse_layout(xml), source_sha256=sha,
            extraction_sha256=digest(xml), processor_version=PROCESSOR,
            coordinate_unit='pdf_points_top_left',
            geometry_kind='inferred_from_native_word_positions', geometry_review_required=True,
            model_used=None, business_approval='unassessed', notice=NOTICE)

    def extract(self, db, ctx, body):
        value = dict(id=uid(), **self._value(db, ctx['tenant'], body))
        validate(value, TABLE_EXTRACTION)
        assets = [body['asset_id']]
        self.store.save_record(db, ctx['tenant'], 'table_extractions', value, assets)
        return 201, value, assets

    def verify_saved(self, db, tenant, value):
        try:
            validate(value, TABLE_EXTRACTION)
            body = {key: value[key] for key in TABLE_INPUT['properties']}
            expected = self._value(db, tenant, body)
            if canonical({k: v for k, v in value.items() if k != 'id'}) != canonical(expected):
                raise APIError(409, 'table_extraction_integrity_failed')
        except APIError as error:
            if error.status == 422:
                raise APIError(409, 'table_extraction_integrity_failed') from error
            raise
        return value
