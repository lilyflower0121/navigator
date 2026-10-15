"""Read-only, bounded worksheet-row extraction; no Office or formula execution.

The report adapter keeps its stricter editable-template policy. Its ZIP bounds,
coordinates and XML helpers are reused here; inert printer settings and stored
formula syntax are permitted only by this observational source reader.
"""
import io
import math
import posixpath
import re
import stat
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from .contracts import APIError, LIMITS, MAX_XLSX_SOURCE, SCHEMAS, canonical, digest, validate
from . import xlsx_adapter as layout

MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
MAX_SOURCE_BYTES = MAX_XLSX_SOURCE
PROCESSOR = 'xlsx-source-rows-v3'
NS, RELNS = layout.NS, layout.RELNS


def _fail(code='xlsx_invalid', status=422):
    raise APIError(status, code)


def _parts(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_SOURCE_BYTES:
        _fail('xlsx_byte_limit', 413)
    # The established validator remains the fast path for compatible packages.
    if len(raw) <= layout.MAX_XLSX_BYTES:
        try:
            return layout._parts(raw)
        except APIError as error:
            if error.code not in ('xlsx_feature_unsupported', 'xlsx_formula_unsupported'):
                raise
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > layout.MAX_PARTS or sum(i.file_size for i in infos) > layout.MAX_EXPANDED_BYTES:
                _fail('xlsx_expansion_limit', 413)
            parts, seen = {}, set()
            for info in infos:
                name = info.filename
                if (name in seen or name.startswith('/') or '\\' in name or '..' in name.split('/')
                        or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16)):
                    _fail()
                seen.add(name)
                if info.file_size > layout.MAX_PART_BYTES or info.file_size > max(4096, info.compress_size * 100):
                    _fail('xlsx_expansion_limit', 413)
                lower = name.lower()
                if any(x in lower for x in ('vbaproject', 'externallink', 'oleobject', 'embeddings/', 'activex/')):
                    _fail('xlsx_feature_unsupported')
                content = archive.read(info)  # CRC validation also applies to inert parts.
                if re.fullmatch(r'xl/printerSettings/printerSettings[1-9][0-9]*\.bin', name):
                    continue
                if not name.endswith(('.xml', '.rels')):
                    _fail('xlsx_feature_unsupported')
                if b'\x00' in content or b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
                    _fail()
                root = ET.fromstring(content)
                for node in root.iter():
                    local = layout._local(node.tag)
                    if local == 'Relationship':
                        target = node.get('Target', '')
                        if node.get('TargetMode', '').lower() == 'external' or ':' in target or '\\' in target:
                            _fail('xlsx_external_link_unsupported')
                    if local == 'Override' and any(x in node.get('ContentType', '').lower() for x in ('macroenabled', 'vba')):
                        _fail('xlsx_feature_unsupported')
                parts[name] = root
            if '[Content_Types].xml' not in parts or 'xl/workbook.xml' not in parts:
                _fail()
            return parts
    except (zipfile.BadZipFile, ET.ParseError, OSError, RuntimeError, ValueError, NotImplementedError):
        _fail()


def _boolean(value):
    if value not in (None, '0', '1', 'false', 'true'):
        _fail()
    return value in ('1', 'true')


def _display_text(node):
    """Visible OOXML string text, excluding rPh and other non-display metadata.

    A shared-string item and inline string have the same text layout: direct t
    children or rich-text r/t runs. Descendant-wide iteration also includes the
    phonetic guide's t nodes, which are not part of the cell's displayed value.
    Keep document order and original whitespace without reading metadata text.
    """
    if node is None:
        return ''
    fragments = []
    for child in node:
        if child.tag == f'{{{NS}}}t':
            fragments.append(child.text or '')
        elif child.tag == f'{{{NS}}}r':
            fragments.extend(text.text or '' for text in child if text.tag == f'{{{NS}}}t')
    return ''.join(fragments)


def _literal(kind, raw, cell, strings):
    if kind == 'inlineStr':
        inline = cell.find(f'{{{NS}}}is')
        return 'text', _display_text(inline)
    if raw is None:
        if kind not in ('n', 's', 'str', 'b', 'e', 'd'):
            _fail('xlsx_feature_unsupported')
        return 'blank', None
    if kind == 's':
        if not re.fullmatch(r'[0-9]+', raw) or int(raw) >= len(strings):
            _fail()
        return 'text', strings[int(raw)]
    if kind in ('str', 'e', 'd'):
        return {'str':'text', 'e':'error', 'd':'date'}[kind], raw
    if kind == 'b':
        if raw not in ('0', '1'):
            _fail()
        return 'boolean', raw == '1'
    if kind == 'n':
        if raw == '':
            return 'blank', None
        if len(raw) > 128:
            _fail('xlsx_number_limit', 413)
        if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', raw):
            _fail()
        try:
            number = Decimal(raw)
            if not number.is_finite():
                _fail()
            numeric = float(number)
            if not math.isfinite(numeric) or numeric == 0 and number != 0:
                # Preserve exact spelling even when JSON numbers cannot represent it.
                return 'number', raw
            if number == number.to_integral_value() and abs(number) <= 2**53:
                return 'number', int(number)
            if Decimal(str(numeric)) != number:
                return 'number', raw
            return 'number', numeric
        except (InvalidOperation, ValueError, OverflowError):
            _fail()
    _fail('xlsx_feature_unsupported')


def _column(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _physical_coordinate(address):
    if not isinstance(address,str) or not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}',address):_fail('xlsx_cell_invalid')
    m=re.fullmatch(r'([A-Z]+)([0-9]+)',address);col=0
    for c in m[1]:col=col*26+ord(c)-64
    row=int(m[2])
    if col>16384 or row>1048576:_fail('xlsx_extent_limit',413)
    return row,col


def selection_ranges(selection):
    from .contracts import XLSX_SELECTION
    validate(selection,XLSX_SELECTION)
    boxes=[];cells=0;rows=set()
    for address in selection['ranges']:
        first,last=[layout._coordinate(x) for x in address.split(':')]
        if first[0]>last[0] or first[1]>last[1]:_fail('xlsx_selection_invalid')
        if any(not(last[0]<a[0] or first[0]>b[0] or last[1]<a[1] or first[1]>b[1]) for a,b in boxes):_fail('xlsx_selection_overlap')
        cells+=(last[0]-first[0]+1)*(last[1]-first[1]+1)
        if cells>layout.MAX_WORKBOOK_CELLS:_fail('xlsx_selection_cell_limit',413)
        rows.update(range(first[0],last[0]+1))
        if len(rows)>LIMITS['segments']:_fail('xlsx_selection_row_limit',413)
        boxes.append((first,last))
    return boxes


def source_snapshot(raw,selection=None):
    """Original lexical values plus explicit absent/blank/formula distinctions."""
    boxes=selection_ranges(selection) if selection is not None else None
    coordinate=_physical_coordinate if boxes is not None else layout._coordinate
    def selected(row,col):return any(a[0]<=row<=b[0] and a[1]<=col<=b[1] for a,b in boxes)
    parts = _parts(raw)
    node_count = 0
    for root in parts.values():
        pending = [(root, 0)]
        while pending:
            node, depth = pending.pop()
            node_count += 1
            if depth > 32 or node_count > 200000:
                _fail('xlsx_xml_limit', 413)
            pending.extend((child, depth + 1) for child in node)
    strings = [_display_text(n) for n in parts.get('xl/sharedStrings.xml', [])]
    styles = parts.get('xl/styles.xml')
    style_count = len(styles.findall(f'{{{NS}}}cellXfs/{{{NS}}}xf')) if styles is not None else 1
    relationships = parts.get('xl/_rels/workbook.xml.rels')
    if relationships is None:
        _fail()
    targets = {}
    for relation in relationships:
        key = relation.get('Id')
        if not key or key in targets:
            _fail()
        targets[key] = relation.get('Target')
    sheets = parts['xl/workbook.xml'].findall(f'{{{NS}}}sheets/{{{NS}}}sheet')
    if selection is not None and selection['sheet'] not in [d.get('name') for d in sheets]:_fail('xlsx_selection_sheet_not_found')
    if not 1 <= len(sheets) <= layout.MAX_SHEETS:
        _fail('xlsx_sheet_limit', 413)
    result, names, total, row_count, output_cells = [], set(), 0, 0, 0
    for definition in sheets:
        name, state = definition.get('name'), definition.get('state', 'visible')
        target = targets.get(definition.get(f'{{{RELNS}}}id'))
        if not name or len(name) > 31 or name in names or not target or state not in ('visible', 'hidden', 'veryHidden'):
            _fail()
        names.add(name)
        path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if path not in parts or not path.startswith('xl/worksheets/'):
            _fail()
        sheet = parts[path]
        if sheet.tag != f'{{{NS}}}worksheet':
            _fail()
        dimension = sheet.find(f'{{{NS}}}dimension')
        if dimension is not None:
            for bound in dimension.get('ref', '').split(':'):
                coordinate(bound)
        hidden_columns = set()
        for column in sheet.findall(f'{{{NS}}}cols/{{{NS}}}col'):
            try:
                first, last = int(column.get('min')), int(column.get('max'))
            except (TypeError, ValueError):
                _fail()
            # Office applies formatting through XFD even on five-column tables.
            # Bound actual cells/dimensions with the existing 256-column policy;
            # retain formatting ranges without expanding the unused columns.
            if not 1 <= first <= last <= 16384:
                _fail('xlsx_extent_limit', 413)
            if _boolean(column.get('hidden')):
                hidden_columns.update(range(first, min(last, 256) + 1))
        merges = []
        for node in sheet.findall(f'{{{NS}}}mergeCells/{{{NS}}}mergeCell'):
            address = node.get('ref', '')
            bounds = address.split(':')
            if len(bounds) not in (1, 2):
                _fail()
            first, last = coordinate(bounds[0]), coordinate(bounds[-1])
            if first[0] > last[0] or first[1] > last[1] or address in [m[0] for m in merges]:
                _fail()
            if len(merges) >= 256:
                _fail('xlsx_merge_limit', 413)
            for _, a, b, _ in merges:
                if not (last[0] < a[0] or first[0] > b[0] or last[1] < a[1] or first[1] > b[1]):
                    _fail('xlsx_merge_overlap')
            merges.append((address, first, last, bounds[0]))
        active=selection is None or name==selection['sheet']
        if boxes is not None and active:
            for address,first,last,_ in merges:
                overlaps=any(not(last[0]<a[0] or first[0]>b[0] or last[1]<a[1] or first[1]>b[1]) for a,b in boxes)
                if overlaps and not any(a[0]<=first[0]<=last[0]<=b[0] and a[1]<=first[1]<=last[1]<=b[1] for a,b in boxes):_fail('xlsx_selection_partial_merge')
        rows, seen_rows = [], set()
        for row in sheet.findall(f'{{{NS}}}sheetData/{{{NS}}}row'):
            address = row.get('r', '')
            if not re.fullmatch(r'[1-9][0-9]{0,6}', address) or int(address) > (1048576 if boxes is not None else 10000):
                _fail('xlsx_extent_limit', 413)
            index = int(address)
            if index in seen_rows:
                _fail()
            seen_rows.add(index)
            keep_row=active and (boxes is None or any(a[0]<=index<=b[0] for a,b in boxes))
            if keep_row:
                row_count+=1
                if row_count>LIMITS['segments']:_fail('extraction_limit',413)
            row_hidden = _boolean(row.get('hidden'))
            seen_columns=set()
            cells = {}
            for cell in row.findall(f'{{{NS}}}c'):
                cell_address = cell.get('r')
                cell_row, col = coordinate(cell_address)
                if cell_row != index or col in seen_columns:
                    _fail('xlsx_cell_invalid')
                seen_columns.add(col)
                total += 1
                if total > layout.MAX_WORKBOOK_CELLS:
                    _fail('xlsx_cell_limit', 413)
                if not keep_row or boxes is not None and not selected(index,col):continue
                nodes = cell.findall(f'{{{NS}}}v')
                formulas = cell.findall(f'{{{NS}}}f')
                if len(nodes) > 1 or len(formulas) > 1:
                    _fail()
                raw_value = (nodes[0].text or '') if nodes else None
                kind, value = _literal(cell.get('t', 'n'), raw_value, cell, strings)
                try:
                    style_id = int(cell.get('s', '0'))
                except ValueError:
                    _fail()
                if not 0 <= style_id < style_count:
                    _fail()
                formula = formulas[0] if formulas else None
                cells[col] = dict(address=cell_address,column=_column(col),column_index=col,
                    kind='formula' if formula is not None else kind,value=None if formula is not None else value,
                    raw_value=raw_value,formula=(formula.text or '') if formula is not None else None,
                    formula_attributes=dict(formula.attrib) if formula is not None else {},
                    cached_value=value if formula is not None else None,
                    cache_state=('present' if nodes else 'missing') if formula is not None else 'not_formula',
                    stored=True,merged_range=None,merged_anchor=None,row_hidden=row_hidden,
                    column_hidden=col in hidden_columns,style_id=style_id)
            if not keep_row:continue
            row_merges = [m for m in merges if m[1][0] <= index <= m[2][0] and (boxes is None or selected(index,m[1][1]))]
            columns = set(cells)
            for _, first, last, _ in row_merges:
                columns.update(range(first[1], last[1] + 1))
            if boxes is not None:
                columns={c for a,b in boxes if a[0]<=index<=b[0] for c in range(a[1],b[1]+1)}
            elif columns:
                columns.update(range(min(columns), max(columns) + 1))
            output_cells += len(columns)
            if output_cells > layout.MAX_WORKBOOK_CELLS:
                _fail('xlsx_cell_limit', 413)
            for col in sorted(columns):
                if col not in cells:
                    cells[col] = dict(address=f'{_column(col)}{index}',column=_column(col),column_index=col,
                        kind='blank',value=None,raw_value=None,formula=None,formula_attributes={},cached_value=None,
                        cache_state='not_formula',stored=False,merged_range=None,merged_anchor=None,
                        row_hidden=row_hidden,column_hidden=col in hidden_columns,style_id=0)
                for merge, first, last, anchor in row_merges:
                    if first[1] <= col <= last[1]:
                        cells[col]['merged_range'], cells[col]['merged_anchor'] = merge, anchor
            rows.append(dict(row=index,row_hidden=row_hidden,cells=[cells[c] for c in sorted(cells)],
                             merges=[m[0] for m in row_merges]))
        if not active:continue
        result.append(dict(sheet=name,sheet_state=state,rows=sorted(rows,key=lambda r:r['row']),
                           column_definitions=[dict(c.attrib) for c in sheet.findall(f'{{{NS}}}cols/{{{NS}}}col')]))
    if boxes is not None:
        actual={r['row'] for sheet in result for r in sheet['rows']}
        wanted={r for a,b in boxes for r in range(a[0],b[0]+1)}
        if not wanted<=actual:_fail('xlsx_selection_missing_row')
    return dict(sheets=result,style_sha256=digest(layout._tree(styles)),package_xml_sha256=digest(
        {name:layout._tree(root) for name,root in sorted(parts.items())}))


def parse_xlsx(claim):
    raw = claim['raw']
    if digest(raw) != claim['sha']:
        _fail('source_integrity_failed', 409)
    selection=claim.get('meta',{}).get('xlsx_selection')
    snapshot = source_snapshot(raw,selection)
    processor=PROCESSOR if selection is None else 'xlsx-selected-rows-v1'
    snapshot_sha = digest(snapshot)
    analysis = processor + ':' + digest(dict(source_sha256=claim['sha'],snapshot_sha256=snapshot_sha,processor=processor,**(dict(selection=selection) if selection is not None else {})))
    segments = []
    for sheet in snapshot['sheets']:
        header_candidates = []
        for row in sheet['rows']:
            if len(segments) >= LIMITS['segments']:
                _fail('extraction_limit', 413)
            cells = row['cells']
            pieces = []
            for cell in cells:
                if cell['kind'] == 'formula':
                    display = '<formula:' + canonical(cell['formula']) + ';cache:' + cell['cache_state']
                    if cell['cache_state'] == 'present':
                        display += ';cached:' + canonical(cell['cached_value'])
                    display += '>'
                elif cell['kind'] == 'blank':
                    display = '<blank>' if cell['stored'] else '<absent>'
                else:
                    display = cell['raw_value'] if cell['kind'] == 'number' else canonical(cell['value'])
                pieces.append(cell['address'] + '=' + display)
            quote = sheet['sheet'] + ' row ' + str(row['row']) + ': ' + '\t'.join(pieces)
            if len(quote) > 8192:
                _fail('extraction_limit', 413)
            locator = dict(kind='xlsx_row',sheet=sheet['sheet'],row=row['row'])
            fields = dict(sheet=sheet['sheet'],row=row['row'],sheet_state=sheet['sheet_state'],
                row_hidden=row['row_hidden'],cells=cells,merges=row['merges'],
                labels=[c['value'] for c in cells if c['kind'] == 'text' and c['value']],
                headers=list(header_candidates),
                columns=[dict(column=c['column'],column_index=c['column_index'],hidden=c['column_hidden']) for c in cells])
            segment_id = digest(dict(asset=claim['asset'],version=claim['version'],analysis=analysis,location=locator))[:32]
            segments.append(dict(id=segment_id,quote=quote,locator=locator,fields=fields,
                evidence=dict(asset_id=claim['asset'],source_version=claim['version'],analysis_version=analysis,segment_id=segment_id),
                source_sha256=claim['sha'],claim_kind='source_quote',model_used=None))
            # Candidate status is explicit: structural extraction does not assert
            # that a text cell is a semantic table header. Never forget later headers.
            for cell in cells:
                if cell['kind'] == 'text' and cell['value']:
                    if len(header_candidates)>=256:_fail('xlsx_header_candidate_limit',413)
                    header_candidates.append(dict(address=cell['address'],row=row['row'],column=cell['column'],text=cell['value'],role='candidate',merged_range=cell['merged_range']))
    if not segments:
        _fail('no_extractable_content')
    result = dict(state='succeeded',analysis_version=analysis,processor_version=processor,model_used=None,
        segments=segments,interpretation='structural_extraction_only',processor_detail=dict(
            source_sha256=claim['sha'],snapshot_sha256=snapshot_sha,parser_version=processor,
            formula_execution=False,formula_cache_trust='stored_value_only',header_semantics='unverified_candidates',
            source_bytes=len(raw),sheets=len(snapshot['sheets'])))
    if selection is not None:result['processor_detail'].update(xlsx_selection=selection,coverage='explicit_selected_ranges_only',selection_sha256=digest(selection),outside_ranges_examined_for_answers=False)
    try:
        validate({k:v for k,v in result.items() if k != 'state'},SCHEMAS['Extraction'])
    except APIError as error:
        raise APIError(413,'extraction_contract_limit') from error
    return result
