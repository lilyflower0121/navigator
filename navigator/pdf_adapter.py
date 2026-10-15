"""Bounded native PDF text extraction with the installed Poppler CLIs.

Quotes are exact contiguous lines of ``pdftotext -layout`` output. They are
text-layer evidence, not OCR, table-cell interpretation, or visual recognition.
No original is truncated and no dependency or model is fetched by this adapter.
"""
import os
import re
import selectors
import signal
import shutil
import subprocess
import tempfile
import time

from .contracts import APIError, LIMITS, SCHEMAS, canonical, digest, validate

PROCESSOR = 'native-pdf-text-v1'
MAX_PDF_BYTES = 4 * 1024 * 1024
MAX_PAGES = 32
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_QUOTE_CHARS = 8192
CLI_TIMEOUT_SECONDS = 5
INTERPRETATION = 'native_pdf_text_only_no_ocr_table_cells_or_visual_semantics'


class _CapabilityUnavailable(Exception):
    pass


def _cli_paths():
    return shutil.which('pdfinfo'), shutil.which('pdftotext')


def _run_cli(argv, maximum=MAX_OUTPUT_BYTES):
    """Bound pipe reads and execution time; never retain or return stderr."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False,
                                   stdin=subprocess.DEVNULL, close_fds=True,start_new_session=True)
    except OSError:
        raise _CapabilityUnavailable() from None
    deadline = time.monotonic() + CLI_TIMEOUT_SECONDS
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise APIError(408, 'pdf_cli_timeout')
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, maximum + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > maximum:
                        raise APIError(413, 'pdf_output_limit')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIError(408, 'pdf_cli_timeout')
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise APIError(408, 'pdf_cli_timeout') from None
            if returncode:
                raise APIError(422, 'pdf_invalid')
        return bytes(output)
    finally:
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def _blocked(error):
    return dict(state='blocked', error=error, segments=[],
                capability='native_pdf_text', processor_version=PROCESSOR,
                model_used=None)


def parse_pdf(claim):
    raw = claim['raw']
    if not raw or len(raw) > MAX_PDF_BYTES:
        raise APIError(413, 'source_byte_limit')
    if digest(raw) != claim['sha']:
        raise APIError(409, 'source_integrity_failed')
    pdfinfo, pdftotext = _cli_paths()
    if not pdfinfo or not pdftotext:
        return _blocked('capability_unavailable')
    try:
        # Private per-operation directory and file, never a shared source path.
        with tempfile.TemporaryDirectory(prefix='navigator-pdf-') as directory:
            os.chmod(directory, 0o700)
            path = os.path.join(directory, 'input.pdf')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as original:
                original.write(raw)
            info = _run_cli([pdfinfo, '-enc', 'UTF-8', path], 65536)
            match = re.search(rb'^Pages:\s+(\d+)\s*$', info, re.MULTILINE)
            if not match:
                raise APIError(422, 'pdf_invalid')
            page_count = int(match.group(1))
            if not 1 <= page_count <= MAX_PAGES:
                raise APIError(413, 'pdf_page_limit')
            extracted = _run_cli([pdftotext, '-layout', '-enc', 'UTF-8', path, '-'])
    except _CapabilityUnavailable:
        return _blocked('capability_unavailable')
    try:
        text = extracted.decode('utf-8')
    except UnicodeError:
        raise APIError(422, 'pdf_text_invalid') from None
    if '\x00' in text:
        raise APIError(422, 'pdf_text_invalid')
    pages = text.split('\f')
    if pages[-1] == '':
        pages.pop()
    if len(pages) != page_count:
        raise APIError(422, 'pdf_page_output_mismatch')
    analysis = PROCESSOR + ':' + digest(dict(source_sha256=claim['sha'],
                                              media_type='application/pdf',
                                              processor=PROCESSOR,extracted_sha256=digest(extracted)))
    segments = []

    def add(lines, page, start):
        quote = '\n'.join(lines)
        if not quote.strip():
            return
        if len(segments) >= LIMITS['segments']:
            raise APIError(413, 'extraction_limit')
        locator = dict(kind='pdf_page', page=page, line_start=start,
                       line_end=start + len(lines) - 1)
        identifier = digest(dict(asset=claim['asset'], version=claim['version'],
                                 analysis=analysis, location=locator))[:32]
        segments.append(dict(id=identifier, quote=quote, locator=locator, fields={},
            evidence=dict(asset_id=claim['asset'], source_version=claim['version'],
                          analysis_version=analysis, segment_id=identifier),
            source_sha256=claim['sha'], claim_kind='source_quote', model_used=None))

    for page_number, page in enumerate(pages, 1):
        lines = page.split('\n')
        if lines[-1] == '':
            lines.pop()  # terminal newline has no further physical source line
        block, length, start = [], 0, 1
        for line_number, line in enumerate(lines, 1):
            if len(line) > MAX_QUOTE_CHARS:
                raise APIError(413, 'extraction_limit')
            if block and length + 1 + len(line) > MAX_QUOTE_CHARS:
                add(block, page_number, start)
                block, length = [], 0
            if not block:
                start = line_number
            length += len(line) + bool(block)
            block.append(line)
        add(block, page_number, start)
    if not segments:
        return _blocked('ocr_unavailable')
    result = dict(state='succeeded', analysis_version=analysis,
                  processor_version=PROCESSOR, model_used=None, segments=segments,
                  interpretation=INTERPRETATION,extracted_sha256=digest(extracted))
    if len(canonical(result).encode('utf-8')) > MAX_OUTPUT_BYTES:
        raise APIError(413, 'extraction_limit')
    try:
        validate({key: value for key, value in result.items() if key != 'state'},
                 SCHEMAS['Extraction'])
    except APIError:
        raise APIError(413, 'extraction_contract_limit') from None
    return result
