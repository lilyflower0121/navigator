"""Bounded local XLSX literal transfer through the installed Artifact Tool.

No client paths or subprocess arguments are accepted. Original bytes are copied
into a private temporary directory. Successful output must pass semantic checks.
"""
import io
import math
import os
from pathlib import Path
import posixpath
import re
import selectors
import signal
import stat
import shutil
import subprocess
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from .contracts import APIError, canonical, digest

MAX_XLSX_BYTES = 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024
MAX_PART_BYTES = 2 * 1024 * 1024
MAX_PARTS = 256
MAX_SHEETS = 10
MAX_CELLS = 64
MAX_WORKBOOK_CELLS = 10000
CLI_TIMEOUT_SECONDS = 30
PROCESSOR = 'artifact-tool-xlsx-literal-v1'
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
RELNS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
CELL = re.compile(r'^[A-Z]{1,3}[1-9][0-9]{0,5}$')
NODE = Path(os.environ.get('NAVIGATOR_NODE_PATH') or shutil.which('node') or 'node-unavailable')
BUILDER = Path(__file__).resolve().parent / 'workers' / 'xlsx_builder.mjs'


def _error(code='xlsx_invalid', status=422):
    raise APIError(status, code)


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _parts(raw, *, allow_formulas=False):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_XLSX_BYTES:
        _error('xlsx_byte_limit', 413)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_PARTS or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
                _error('xlsx_expansion_limit', 413)
            result = {}
            for info in infos:
                name = info.filename
                if (name in result or name.startswith('/') or '\\' in name or
                        '..' in name.split('/') or info.flag_bits & 1 or
                        stat.S_ISLNK(info.external_attr >> 16)):
                    _error()
                if info.file_size > MAX_PART_BYTES or info.file_size > max(4096, info.compress_size * 100):
                    _error('xlsx_expansion_limit', 413)
                lower = name.lower()
                if any(token in lower for token in ('vbaproject', 'externallink', 'oleobject', 'embeddings/', 'activex/')):
                    _error('xlsx_feature_unsupported')
                # Binary/drawing features are deliberately outside this first adapter.
                if not (name.endswith('.xml') or name.endswith('.rels')):
                    _error('xlsx_feature_unsupported')
                content = archive.read(info)
                if b'\x00' in content or b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
                    _error()
                root = ET.fromstring(content)
                if allow_formulas:
                    pending=[(root,0)];nodes=0
                    while pending:
                        node,depth=pending.pop();nodes+=1
                        if depth>32 or nodes>200000:_error('xlsx_xml_limit',413)
                        pending.extend((child,depth+1) for child in node)
                for node in root.iter():
                    local = _local(node.tag)
                    if local == 'Relationship':
                        target = node.get('Target', '')
                        if node.get('TargetMode', '').lower() == 'external' or ':' in target or '\\' in target:
                            _error('xlsx_external_link_unsupported')
                    if local in ('formula', 'formula1', 'formula2') or local == 'f' and not allow_formulas:
                        _error('xlsx_formula_unsupported')
                    if local in ('drawing', 'legacyDrawing', 'tableParts', 'pivotTable', 'conditionalFormatting',
                                 'dataValidations', 'sheetProtection', 'definedName'):
                        _error('xlsx_feature_unsupported')
                    if local == 'workbookProtection' and node.attrib:
                        _error('xlsx_feature_unsupported')
                    if local == 'Override' and any(x in node.get('ContentType', '').lower() for x in ('macroenabled', 'vba')):
                        _error('xlsx_feature_unsupported')
                result[name] = root
            if '[Content_Types].xml' not in result or 'xl/workbook.xml' not in result:
                _error()
            return result
    except (zipfile.BadZipFile, ET.ParseError, OSError, RuntimeError, ValueError, NotImplementedError):
        _error()


def _number(value):
    try:
        return str(Decimal(value).normalize())
    except (InvalidOperation, TypeError):
        return value


def _tree(node):
    if node is None:
        return None
    return (node.tag, tuple(sorted(node.attrib.items())), node.text or '', tuple(_tree(c) for c in node))


def _style_table(parts):
    styles = parts.get('xl/styles.xml')
    if styles is None:
        _error()
    groups = {}
    for name in ('fonts', 'fills', 'borders', 'cellXfs'):
        group = styles.find(f'{{{NS}}}{name}')
        groups[name] = list(group) if group is not None else []
    numfmts = {n.get('numFmtId'): n.get('formatCode') for n in styles.findall(f'{{{NS}}}numFmts/{{{NS}}}numFmt')}
    result = []
    for xf in groups['cellXfs']:
        try:
            components = tuple(_tree(groups[group][int(xf.get(key, '0'))]) for key, group in
                               (('fontId', 'fonts'), ('fillId', 'fills'), ('borderId', 'borders')))
        except (ValueError, IndexError):
            _error()
        number = xf.get('numFmtId', '0')
        # apply flags/index ordering are serialization details; actual components matter.
        other = {k: v for k, v in xf.attrib.items() if k not in
                 ('fontId', 'fillId', 'borderId', 'numFmtId', 'xfId', 'applyFont', 'applyFill', 'applyBorder', 'applyNumberFormat', 'applyAlignment', 'applyProtection')}
        result.append((components, numfmts.get(number, number), tuple(sorted(other.items())),
                       _tree(xf.find(f'{{{NS}}}alignment')), _tree(xf.find(f'{{{NS}}}protection'))))
    return result


def _coordinate(address):
    if not isinstance(address, str) or not CELL.fullmatch(address):
        _error('xlsx_cell_invalid')
    match = re.fullmatch(r'([A-Z]+)([0-9]+)', address)
    col = 0
    for character in match[1]:
        col = col * 26 + ord(character) - 64
    row = int(match[2])
    if col > 256 or row > 10000:
        _error('xlsx_extent_limit', 413)
    return row, col


def _snapshot(raw, *, allow_formulas=False):
    parts = _parts(raw,allow_formulas=allow_formulas)
    styles = _style_table(parts)
    strings = [''.join(t.text or '' for t in n.iter() if _local(t.tag) == 't')
               for n in parts.get('xl/sharedStrings.xml', [])]
    relationships = parts.get('xl/_rels/workbook.xml.rels')
    if relationships is None:
        _error()
    targets = {n.get('Id'): n.get('Target') for n in relationships}
    sheets = parts['xl/workbook.xml'].findall(f'{{{NS}}}sheets/{{{NS}}}sheet')
    if not 1 <= len(sheets) <= MAX_SHEETS:
        _error('xlsx_sheet_limit', 413)
    result, total = {}, 0
    for definition in sheets:
        name = definition.get('name')
        target = targets.get(definition.get(f'{{{RELNS}}}id'))
        if not name or len(name) > 31 or name in result or not target:
            _error()
        partname = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if partname not in parts or not partname.startswith('xl/worksheets/'):
            _error()
        sheet = parts[partname]
        dimension = sheet.find(f'{{{NS}}}dimension')
        if dimension is not None:
            for bound in dimension.get('ref', '').split(':'):
                _coordinate(bound)
        cells, rows = {}, {}
        sheet_rows = sheet.findall(f'{{{NS}}}sheetData/{{{NS}}}row')
        if len(sheet_rows) > 10000:
            _error('xlsx_extent_limit', 413)
        seen_rows = set()
        for column in sheet.findall(f'{{{NS}}}cols/{{{NS}}}col'):
            try:
                if not 1 <= int(column.get('min')) <= int(column.get('max')) <= 256:
                    _error('xlsx_extent_limit', 413)
            except (ValueError, TypeError):
                _error()
        for row in sheet_rows:
            row_address = row.get('r')
            if not row_address or not re.fullmatch(r'[1-9][0-9]{0,4}', row_address) or int(row_address) > 10000:
                _error('xlsx_extent_limit', 413)
            if row_address in seen_rows:
                _error()
            seen_rows.add(row_address)
            rowmeta = {k: _number(v) for k, v in row.attrib.items() if k not in ('r', 'spans') and v != '0'}
            if rowmeta:
                rows[row.get('r')] = rowmeta
            for cell in row:
                address = cell.get('r')
                _coordinate(address)
                if address in cells:
                    _error()
                total += 1
                if total > MAX_WORKBOOK_CELLS:
                    _error('xlsx_cell_limit', 413)
                value_node = cell.find(f'{{{NS}}}v')
                value = value_node.text if value_node is not None else None
                kind = cell.get('t', 'n')
                try:
                    if kind == 's':
                        value = strings[int(value)]
                        kind = 'text'
                    elif kind == 'inlineStr':
                        value = ''.join(n.text or '' for n in cell.iter() if _local(n.tag) == 't')
                        kind = 'text'
                    elif kind == 'str':
                        value = value or ''
                        kind = 'text'
                    elif kind == 'n' and value is not None:
                        try:
                            if not Decimal(value).is_finite():
                                _error()
                        except InvalidOperation:
                            _error()
                        value = _number(value)
                    elif kind == 'b' and value is not None:
                        value = value == '1'
                    elif kind not in ('n', 'b'):
                        _error('xlsx_feature_unsupported')
                    style = styles[int(cell.get('s', '0'))]
                except (ValueError, IndexError, TypeError):
                    _error()
                cells[address] = dict(value=value, kind=kind if value is not None else 'blank', style=style)
                if allow_formulas:cells[address]['formula']=_tree(cell.find(f'{{{NS}}}f'))
        merges = tuple(sorted(n.get('ref') for n in sheet.findall(f'{{{NS}}}mergeCells/{{{NS}}}mergeCell')))
        for merge in merges:
            for bound in merge.split(':'):
                _coordinate(bound)
        metadata = { _local(n.tag): _tree(n) for n in sheet if _local(n.tag) not in ('sheetData', 'dimension', 'mergeCells')}
        result[name] = dict(cells=cells, merges=merges, rows=rows, metadata=metadata, state=definition.get('state', 'visible'))
    return result


def validate_xlsx(raw):
    """Read-only validation plus bounded sheet/coordinate information."""
    snapshot = _snapshot(raw)
    return dict(source_sha256=digest(raw), source_bytes=len(raw),
                sheets=[dict(name=name, cells=sorted(sheet['cells']), merges=list(sheet['merges'])) for name, sheet in snapshot.items()],
                limitations=['no_formulas', 'no_macros', 'no_external_links', 'simple_cell_layout_only'])


def validate_preserved_formulas(raw, snapshot, mappings):
    """Small inert template subset: constants or local SUM of numeric literals.

    Mapped values cannot affect retained formulas; their expression and cached
    value therefore remain exact. Other functions, links, arrays and formula
    dependencies are rejected before the authoring engine sees the template.
    """
    mapped={(m['sheet'],m['cell']) for m in mappings};count=0
    for name,sheet in snapshot.items():
        formulas={a:c for a,c in sheet['cells'].items() if c.get('formula')}
        for address,cell in formulas.items():
            count+=1
            if count>64:_error('xlsx_formula_limit',413)
            if (name,address) in mapped:_error('xlsx_target_formula_unsupported')
            tag,attrs,text,children=cell['formula']
            if attrs or children or len(text)>128 or cell['kind']!='n':_error('xlsx_formula_unsupported')
            match=re.fullmatch(r'SUM\((\$?[A-Z]{1,3}\$?[1-9][0-9]{0,4}):(\$?[A-Z]{1,3}\$?[1-9][0-9]{0,4})\)',text)
            if match:
                start,end=(_coordinate(x.replace('$','')) for x in match.groups())
                if start[0]>end[0] or start[1]>end[1] or (end[0]-start[0]+1)*(end[1]-start[1]+1)>10000:_error('xlsx_formula_unsupported')
                for row in range(start[0],end[0]+1):
                    for col in range(start[1],end[1]+1):
                        ref='';n=col
                        while n:n,k=divmod(n-1,26);ref=chr(65+k)+ref
                        ref+=str(row)
                        if (name,ref) in mapped:_error('xlsx_formula_depends_on_output')
                        other=sheet['cells'].get(ref)
                        if other is None or other.get('formula') or other['kind']!='n':_error('xlsx_formula_unsupported')
            elif not re.fullmatch(r'[0-9]+(?:[+*\-][0-9]+)+',text):_error('xlsx_formula_unsupported')
    parts=_parts(raw,allow_formulas=True)
    if sum(_local(n.tag)=='f' for root in parts.values() for n in root.iter())!=count:_error('xlsx_formula_unsupported')
    return count


def _mappings(cells, snapshot):
    if not isinstance(cells, list) or not 1 <= len(cells) <= MAX_CELLS:
        _error('xlsx_mapping_limit', 413)
    seen = set()
    for item in cells:
        if not isinstance(item, dict) or set(item) != {'sheet', 'cell', 'value'}:
            _error('xlsx_mapping_invalid')
        sheet, address, value = item['sheet'], item['cell'], item['value']
        if not isinstance(sheet, str) or sheet not in snapshot:
            _error('xlsx_sheet_invalid')
        row, col = _coordinate(address)
        if address not in snapshot[sheet]['cells']:
            _error('xlsx_cell_missing')
        if (sheet, address) in seen:
            _error('xlsx_duplicate_cell')
        seen.add((sheet, address))
        for merge in snapshot[sheet]['merges']:
            bounds = merge.split(':')
            first, last = bounds[0], bounds[-1]
            r0, c0 = _coordinate(first)
            r1, c1 = _coordinate(last)
            if r0 <= row <= r1 and c0 <= col <= c1 and address != first:
                _error('xlsx_merged_cell_non_anchor')
        if isinstance(value, str):
            try:
                encoded = value.encode('utf-8')
            except UnicodeError:
                _error('xlsx_value_invalid')
            if len(encoded) > 8192 or any(ord(c) < 32 and c not in '\t\n\r' for c in value):
                _error('xlsx_value_invalid')
        elif value is not None and (not isinstance(value, (bool, int, float)) or
                                   abs(value) > 1e15 or not math.isfinite(value)):
            _error('xlsx_value_invalid')
    return cells


def _layout(row_heights, cells):
    if row_heights is None:
        return []
    expected = [dict(sheet='完了検査成績書', row=7, height=180)]
    if row_heights != expected or {(c['sheet'], c['cell']) for c in cells} != {('完了検査成績書', a) for a in ('B7', 'C7', 'D7', 'H7')}:
        _error('xlsx_layout_adjustment_invalid')
    return expected


def validate_output(original_bytes, output_bytes, cells, *, row_heights=None, allow_formulas=False):
    original, output = _snapshot(original_bytes,allow_formulas=allow_formulas), _snapshot(output_bytes,allow_formulas=allow_formulas)
    if allow_formulas:validate_preserved_formulas(original_bytes,original,cells)
    _mappings(cells, original)
    adjustments = _layout(row_heights, cells)
    mapped = {(c['sheet'], c['cell']): c['value'] for c in cells}
    # Exact passthrough for all package parts except mapped worksheets. This
    # includes themes (theme-indexed colors), styles, print names and properties.
    with zipfile.ZipFile(io.BytesIO(original_bytes)) as first, zipfile.ZipFile(io.BytesIO(output_bytes)) as second:
        if set(first.namelist()) != set(second.namelist()):
            _error('xlsx_preservation_failed')
        original_parts, output_parts = _parts(original_bytes,allow_formulas=allow_formulas), _parts(output_bytes,allow_formulas=allow_formulas)
        for partname in first.namelist():
            if partname.startswith('xl/worksheets/') and partname.endswith('.xml'):
                a, b = original_parts[partname], output_parts[partname]
                # Values were independently checked above/below. Mask their
                # storage representation to compare every remaining XML detail.
                sheet_definition = original_parts['xl/workbook.xml'].findall(f'{{{NS}}}sheets/{{{NS}}}sheet')
                rels = {n.get('Id'): n.get('Target') for n in original_parts['xl/_rels/workbook.xml.rels']}
                names = {posixpath.normpath(t.lstrip('/') if t.startswith('/') else 'xl/' + t): n.get('name')
                         for n in sheet_definition for t in [rels[n.get(f'{{{RELNS}}}id')]]}
                for name, address in mapped:
                    if names.get(partname) != name:
                        continue
                    for root in (a, b):
                        cell = root.find(f'.//{{{NS}}}c[@r="{address}"]')
                        if cell is None:
                            _error('xlsx_preservation_failed')
                        cell.attrib.pop('t', None)
                        for child in list(cell):
                            if _local(child.tag) in ('v', 'is'):
                                cell.remove(child)
                for adjustment in adjustments:
                    if names.get(partname) == adjustment['sheet']:
                        row = a.find(f'.//{{{NS}}}row[@r="7"]')
                        row.set('ht', '180')
                        row.set('customHeight', '1')
                if _tree(a) != _tree(b):
                    _error('xlsx_preservation_failed')
            elif first.read(partname) != second.read(partname):
                _error('xlsx_preservation_failed')
    if list(original) != list(output):
        _error('xlsx_preservation_failed')
    for name, before in original.items():
        after = output[name]
        for adjustment in adjustments:
            if adjustment['sheet'] == name:
                before['rows']['7'] = dict(before['rows'].get('7', {}), ht='1.8E+2', customHeight='1')
        if any(before[k] != after[k] for k in ('merges', 'rows', 'metadata', 'state')):
            _error('xlsx_preservation_failed')
        if set(before['cells']) != set(after['cells']):
            _error('xlsx_preservation_failed')
        for address, previous in before['cells'].items():
            current = after['cells'][address]
            if previous['style'] != current['style']:
                _error('xlsx_preservation_failed')
            if (name, address) not in mapped:
                if previous != current:
                    _error('xlsx_preservation_failed')
            else:
                value = mapped[name, address]
                expected_kind = 'blank' if value is None else 'text' if isinstance(value, str) else 'b' if isinstance(value, bool) else 'n'
                expected_value = _number(str(value)) if expected_kind == 'n' else value
                if (current['kind'], current['value']) != (expected_kind, expected_value):
                    _error('xlsx_mapped_value_mismatch')
    return dict(state='succeeded', processor_version=PROCESSOR, original_sha256=digest(original_bytes),
                output_sha256=digest(output_bytes), original_bytes=len(original_bytes), output_bytes=len(output_bytes),
                mapped_cell_values=cells, preserved_unmapped_cells=True, preserved_merges=True,
                preserved_cell_styles=True, preserved_sheet_layout=not adjustments,
                preserved_unrelated_layout=True, layout_adjustments=adjustments, formula_count=sum(bool(c.get('formula')) for sheet in original.values() for c in sheet['cells'].values()),
                model_used=None, validation='saved_ooxml_semantics')


def _preserve_original(original_bytes, authored_bytes, mappings, row_heights=None, *, allow_formulas=False):
    """Keep every original part, graft only Artifact Tool-authored value XML.

    Artifact Tool currently drops imported page margins and view metadata.
    This preservation pass does not create spreadsheet values or styles.
    It resolves authored shared strings before moving their XML into the source.
    """
    source_parts, generated_parts = _parts(original_bytes,allow_formulas=allow_formulas), _parts(authored_bytes,allow_formulas=allow_formulas)
    generated = _snapshot(authored_bytes,allow_formulas=allow_formulas)
    source_names, generated_names = {}, {}
    for parts, names in ((source_parts, source_names), (generated_parts, generated_names)):
        rels = {n.get('Id'): n.get('Target') for n in parts['xl/_rels/workbook.xml.rels']}
        for node in parts['xl/workbook.xml'].findall(f'{{{NS}}}sheets/{{{NS}}}sheet'):
            target = rels[node.get(f'{{{RELNS}}}id')]
            names[node.get('name')] = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
    shared = list(generated_parts.get('xl/sharedStrings.xml', []))
    changed = set()
    for mapping in mappings:
        name, address = mapping['sheet'], mapping['cell']
        source = source_parts[source_names[name]].find(f'.//{{{NS}}}c[@r="{address}"]')
        authored = generated_parts[generated_names[name]].find(f'.//{{{NS}}}c[@r="{address}"]')
        if source is None or authored is None:
            _error('xlsx_mapped_value_mismatch')
        # Check generated values before accepting them as authored source nodes.
        current = generated[name]['cells'].get(address)
        value = mapping['value']
        escaped_apostrophe = isinstance(value, str) and value.startswith("'=")
        if escaped_apostrophe and current is not None and current['kind'] == 'text' and current['value'] == "'" + value:
            # The worker doubled a literal leading apostrophe to keep Artifact
            # Tool from treating it as an input escape. Decode exactly one token.
            current = dict(current, value=value)
        kind = 'blank' if value is None else 'text' if isinstance(value, str) else 'b' if isinstance(value, bool) else 'n'
        expected = _number(str(value)) if kind == 'n' else value
        if current is None or (current['kind'], current['value']) != (kind, expected):
            _error('xlsx_mapped_value_mismatch')
        for child in list(source):
            if _local(child.tag) in ('v', 'is', 'f'):
                source.remove(child)
        source.attrib.pop('t', None)
        authored_kind = authored.get('t')
        if authored_kind == 's':
            value_node = authored.find(f'{{{NS}}}v')
            string = shared[int(value_node.text)]
            string_copy = ET.fromstring(ET.tostring(string))
            string_copy.tag = f'{{{NS}}}is'
            source.set('t', 'inlineStr')
            source.append(string_copy)
        else:
            if authored_kind:
                source.set('t', authored_kind)
            for child in authored:
                if _local(child.tag) in ('v', 'is'):
                    source.append(ET.fromstring(ET.tostring(child)))
        if escaped_apostrophe:
            text_nodes = [n for n in source.iter() if _local(n.tag) in ('v', 't') and n.text]
            if not text_nodes or not text_nodes[0].text.startswith("''="):
                _error('xlsx_mapped_value_mismatch')
            text_nodes[0].text = text_nodes[0].text[1:]
        changed.add(source_names[name])
    for adjustment in _layout(row_heights, mappings):
        name = adjustment['sheet']
        source_row = source_parts[source_names[name]].find(f'.//{{{NS}}}row[@r="7"]')
        authored_row = generated_parts[generated_names[name]].find(f'.//{{{NS}}}row[@r="7"]')
        if source_row is None or authored_row is None or _number(authored_row.get('ht')) != '1.8E+2':
            _error('xlsx_layout_adjustment_invalid')
        source_row.set('ht', authored_row.get('ht'))
        source_row.set('customHeight', '1')
        changed.add(source_names[name])
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original_bytes)) as original, zipfile.ZipFile(output, 'w') as result:
        for info in original.infolist():
            content = ET.tostring(source_parts[info.filename], encoding='utf-8', xml_declaration=True) if info.filename in changed else original.read(info)
            result.writestr(info, content)
    raw = output.getvalue()
    if len(raw) > MAX_XLSX_BYTES:
        _error('xlsx_output_limit', 413)
    return raw


def worker_available():
    """Opt-in licensed runtime only; importing the API never starts Node."""
    return bool(NODE.is_file() and BUILDER.is_file() and os.environ.get('NAVIGATOR_ENABLE_XLSX_WORKER')=='1')


def _run_worker(directory):
    if not worker_available():
        _error('xlsx_capability_unavailable', 503)
    environment = os.environ.copy()
    environment.update(OMP_NUM_THREADS='1', OMP_THREAD_LIMIT='1', VECLIB_MAXIMUM_THREADS='1', UV_THREADPOOL_SIZE='1')
    process = None
    try:
        process = subprocess.Popen([str(NODE), '--max-old-space-size=256', str(BUILDER), directory],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, close_fds=True, start_new_session=True, env=environment)
        deadline, count = time.monotonic() + CLI_TIMEOUT_SECONDS, 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _error('xlsx_cli_timeout', 408)
                output_path = Path(directory) / 'output.xlsx'
                if output_path.exists() and output_path.stat().st_size > MAX_XLSX_BYTES:
                    _error('xlsx_output_limit', 413)
                for key, _ in selector.select(min(remaining, 0.05)):
                    block = os.read(key.fd, 4096)
                    if not block:
                        selector.unregister(key.fileobj)
                    count += len(block)
                    if count > 16384:
                        _error('xlsx_worker_output_limit', 413)
        if process.returncode:
            _error('xlsx_cli_failed')
    except OSError:
        _error('xlsx_capability_unavailable', 503)
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()


def render_xlsx(original_bytes, cells, *, row_heights=None, allow_formulas=False):
    """Return ``(output_bytes, manifest)`` after native export and semantic validation."""
    original = _snapshot(original_bytes,allow_formulas=allow_formulas)
    _mappings(cells, original)
    if allow_formulas:validate_preserved_formulas(original_bytes,original,cells)
    adjustments = _layout(row_heights, cells)
    with tempfile.TemporaryDirectory(prefix='navigator-xlsx-') as directory:
        os.chmod(directory, 0o700)
        for name, content in (('input.xlsx', original_bytes), ('request.json', canonical(dict(mode='patch', cells=cells, rowHeights=adjustments)).encode())):
            fd = os.open(Path(directory) / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(content)
        _run_worker(directory)
        try:
            with open(Path(directory) / 'output.xlsx', 'rb') as handle:
                output = handle.read(MAX_XLSX_BYTES + 1)
        except OSError:
            _error('xlsx_output_missing')
        if len(output) > MAX_XLSX_BYTES:
            _error('xlsx_output_limit', 413)
        output = _preserve_original(original_bytes, output, cells, adjustments or None,allow_formulas=allow_formulas)
        manifest = validate_output(original_bytes, output, cells, row_heights=adjustments or None,allow_formulas=allow_formulas)
        manifest['preservation_method'] = 'original_package_with_artifact_authored_value_nodes'
        return output, manifest
