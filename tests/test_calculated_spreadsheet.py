"""Independent selected-XLSX boundary checks: synthetic ZIPs, no gold/models."""
import base64
import io
import json
import unittest
import zipfile
from decimal import Decimal

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest
from navigator.parsing import parse_source
from navigator.store import Store
from navigator.xlsx_ingest import MEDIA, selection_ranges, source_snapshot
from tests.test_spreadsheet_boundaries import workbook, replace_package_parts


def column(index):
    out = ''
    while index:
        index, rem = divmod(index - 1, 26); out = chr(65 + rem) + out
    return out


class Phase8IndependentReview(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:'); self.n = 0
        self.auth = Auth({'owner': dict(tenant='phase8-review', actor='owner', revision=1, scopes=['read', 'write', 'delete']),
            'other': dict(tenant='phase8-review', actor='other', revision=1, scopes=['read']),
            'foreign': dict(tenant='phase8-other', actor='owner', revision=1, scopes=['read'])})
        self.api = API(self.store, self.auth)

    def tearDown(self): self.store.close()

    def call(self, method, path, body=None, token='owner', key=None):
        self.n += 1
        return self.api.dispatch(method, path, token, body,
            key or ('phase8-independent-' + str(self.n) if method in ('POST', 'DELETE') else None))[1]

    def selection(self, *ranges, sheet='Synthetic'): return dict(sheet=sheet, ranges=list(ranges))

    def raw(self):
        return workbook([('Synthetic', [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value'), ('C', 'text', 'Secret')]),
            (2, [('A', 'text', 'Target'), ('B', 'number', '1.235'), ('C', 'number', 999)]),
            (3, [('A', 'text', 'Other'), ('B', 'number', '2.345'), ('C', 'number', 888)])]),
            ('Outside', [(1, [('A', 'text', 'Unselected sheet secret')]), (2, [('B', 'number', 777)])])])

    def claim(self, raw, selection=None):
        meta = dict(media_type=MEDIA)
        if selection is not None: meta['xlsx_selection'] = selection
        return dict(raw=raw, sha=digest(raw), asset='a'*32, version=1, meta=meta)

    def ingest(self, raw=None, selection=None, *, asset=None, finish=True):
        raw = self.raw() if raw is None else raw
        body = dict(name='Independent synthetic selection', media_type=MEDIA, content_base64=base64.b64encode(raw).decode())
        if selection is not None: body['xlsx_selection'] = selection
        if asset: body['asset_id'] = asset
        value = self.call('POST', '/v1/assets', body)
        if finish:
            self.assertTrue(self.api.library.run_once())
            job = self.call('GET', '/v1/jobs/' + value['job']['id'])
            self.assertEqual(job['state'], 'succeeded', job['error'])
            value['final_job'] = job
        return value

    def task(self, assets, cells=('B2',), *, policy='conditional', **fields):
        return dict(query='Synthetic explicit cells', asset_ids=assets, retrieval_policy=policy,
            task=dict(kind='table_lookup', operation='identity', selectors=[dict(sheet='Synthetic', cell=c) for c in cells], **fields))

    def error(self, fn, codes=None):
        with self.assertRaises(APIError) as caught: fn()
        if codes: self.assertIn(caught.exception.code, codes)
        return caught.exception

    def assert_hold(self, result):
        self.assertNotEqual(result['state'], 'answered_calculation', dict(state=result['state'], claims=result['claims']))
        self.assertEqual(result['claims'], [])
        self.assertFalse(result['stages']['explicit_task_complete'])

    def test_selected_values_original_bytes_coordinates_and_coverage(self):
        raw = self.raw(); selected = self.selection('B2:B3')
        asset = self.ingest(raw, selected)
        job = asset['final_job']; result = job['result']
        self.assertEqual(job['processor_version'], 'xlsx-selected-rows-v1')
        self.assertEqual(result['processor_detail']['xlsx_selection'], selected)
        self.assertEqual(result['processor_detail']['coverage'], 'explicit_selected_ranges_only')
        self.assertEqual([s['locator']['row'] for s in result['segments']], [2, 3])
        self.assertEqual([[c['address'] for c in s['fields']['cells']] for s in result['segments']], [['B2'], ['B3']])
        self.assertNotIn('999', canonical(result)); self.assertNotIn('888', canonical(result)); self.assertNotIn('777', canonical(result))
        original = self.call('GET', '/v1/assets/' + asset['asset_id'] + '/versions/1/original')
        self.assertEqual(original, raw)
        value = self.call('POST', '/v1/evidence-tasks', self.task([asset['asset_id']], ('B2', 'B3'), scale='2', precision=2))
        self.assertEqual(value['state'], 'answered_calculation')
        self.assertEqual([Decimal(c['value']) for c in value['claims']], [Decimal('2.47'), Decimal('4.69')])
        self.assertEqual(value['stages']['source_coverage'], 'explicit_selected_ranges_only')
        for claim, address in zip(value['claims'], ('B2', 'B3')):
            operand = claim['operands'][0]
            self.assertEqual(operand['cell_address'], address)
            self.assertEqual(operand['support_evidence']['source_sha256'], digest(raw))
            self.assertEqual(operand['support_evidence']['locator']['sheet'], 'Synthetic')

    def test_unselected_column_row_and_sheet_never_supply_a_value(self):
        asset = self.ingest(selection=self.selection('B2:B2'))['asset_id']
        for selector in (dict(sheet='Synthetic', cell='C2'), dict(sheet='Synthetic', cell='B3'), dict(sheet='Outside', cell='B2')):
            body = self.task([asset]); body['task']['selectors'] = [selector]
            self.assert_hold(self.call('POST', '/v1/evidence-tasks', body))

    def test_disjoint_ranges_do_not_fill_or_leak_gap_cells(self):
        raw = workbook([('Synthetic', [(2, [('B', 'number', 4), ('C', 'number', 999), ('D', 'number', 6)])])])
        parsed = parse_source(self.claim(raw, self.selection('B2:B2', 'D2:D2')))
        cells = parsed['segments'][0]['fields']['cells']
        self.assertEqual([c['address'] for c in cells], ['B2', 'D2'])
        self.assertNotIn('C2', parsed['segments'][0]['quote'])
        self.assertNotIn('999', parsed['segments'][0]['quote'])

    def test_whole_profile_unchanged_and_selection_binds_analysis_identity(self):
        raw = workbook([('Synthetic', [(1, [('A', 'text', 'Header'), ('B', 'number', 4)]), (2, [('A', 'text', 'Target'), ('B', 'number', 5)])])])
        whole = parse_source(self.claim(raw))
        a = parse_source(self.claim(raw, self.selection('A1:B2')))
        b = parse_source(self.claim(raw, self.selection('B1:B2')))
        self.assertEqual(whole['processor_version'], 'xlsx-source-rows-v3')
        self.assertEqual(whole['segments'][1]['fields'], a['segments'][1]['fields'])
        self.assertNotEqual(whole['analysis_version'], a['analysis_version'])
        self.assertNotEqual(a['analysis_version'], b['analysis_version'])
        self.assertEqual(whole['segments'][1]['source_sha256'], a['segments'][1]['source_sha256'])

    def test_physical_wide_dimension_and_many_rows_do_not_expand_selected_scope(self):
        rows = [(i, [('A', 'number', i), ('B', 'number', i + 1000)]) for i in range(1, 301)]
        rows.append((310, [('IW', 'number', 999999)]))
        raw = workbook([('Synthetic', rows)])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive: xml = archive.read('xl/worksheets/sheet1.xml').decode()
        raw = replace_package_parts(raw, {'xl/worksheets/sheet1.xml': xml.replace('<sheetData>', '<dimension ref="A1:IW310"/><sheetData>')})
        self.error(lambda: parse_source(self.claim(raw)))
        parsed = parse_source(self.claim(raw, self.selection('B78:B78')))
        self.assertEqual(len(parsed['segments']), 1)
        self.assertEqual(parsed['segments'][0]['fields']['cells'][0]['address'], 'B78')
        self.assertEqual(parsed['segments'][0]['fields']['cells'][0]['value'], 1078)
        self.assertNotIn('999999', parsed['segments'][0]['quote'])

    def test_selection_range_budget_overlap_reverse_extent_and_count(self):
        for ranges, codes in ((('A1:A257',), ['xlsx_selection_row_limit']), (('A1:IV40',), ['xlsx_selection_cell_limit']),
            (('A1:B2', 'B2:C3'), ['xlsx_selection_overlap']), (('B2:A1',), ['xlsx_selection_invalid']),
            (('IW1:IW1',), ['xlsx_extent_limit']), (('A10001:A10001',), ['xlsx_extent_limit']),
            (tuple(f'A{i}:A{i}' for i in range(1, 10)), ['input_item_limit'])):
            with self.subTest(ranges=ranges): self.error(lambda: selection_ranges(self.selection(*ranges)), codes)
        self.assertEqual(len(selection_ranges(self.selection('A1:A256'))), 1)
        self.assertEqual(len(selection_ranges(self.selection('A1:IV39'))), 1)

    def test_global_stored_cell_budget_is_enforced_outside_selection(self):
        rows = [(i, [(column(c), 'number', c) for c in range(1, 41)]) for i in range(1, 252)]
        raw = workbook([('Synthetic', rows)])
        self.error(lambda: source_snapshot(raw, self.selection('A1:A1')), ['xlsx_cell_limit'])

    def test_partial_merge_is_rejected_and_full_merge_keeps_anchor_and_addresses(self):
        raw = workbook([('Synthetic', [(2, [('B', 'text', 'Merged'), ('D', 'number', 4)])])])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive: xml = archive.read('xl/worksheets/sheet1.xml').decode()
        raw = replace_package_parts(raw, {'xl/worksheets/sheet1.xml': xml.replace('</worksheet>', '<mergeCells><mergeCell ref="B2:C2"/></mergeCells></worksheet>')})
        self.error(lambda: source_snapshot(raw, self.selection('C2:D2')), ['xlsx_selection_partial_merge'])
        self.error(lambda: source_snapshot(raw, self.selection('B2:B2', 'C2:C2')), ['xlsx_selection_partial_merge'])
        cells = parse_source(self.claim(raw, self.selection('B2:D2')))['segments'][0]['fields']['cells']
        self.assertEqual([c['address'] for c in cells], ['B2', 'C2', 'D2'])
        self.assertEqual(cells[1]['merged_anchor'], 'B2'); self.assertIsNone(cells[1]['value'])

    def test_unselected_unsafe_zip_xml_and_external_relationship_still_reject(self):
        for changes, code in (({'../outside.xml': '<x/>'}, 'xlsx_invalid'),
            ({'outside.xml': '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>'}, 'xlsx_invalid'),
            ({'outside.rels': '<Relationships><Relationship Id="outside" TargetMode="External" Target="https://invalid.test/"/></Relationships>'}, 'xlsx_external_link_unsupported'),
            ({'xl/vbaProject.bin': b'unsafe'}, 'xlsx_feature_unsupported')):
            with self.subTest(code=code):
                self.error(lambda: source_snapshot(replace_package_parts(self.raw(), changes), self.selection('B2:B2')), [code])

    def test_invalid_selector_shapes_policy_and_coordinate_reject(self):
        asset = self.ingest(selection=self.selection('B2:B2'))['asset_id']
        for selector in (dict(cell='B2'), dict(sheet='Synthetic'), dict(sheet='Synthetic', cell='B2', row='Target'),
            dict(sheet='Synthetic', cell='B2', column='Value'), dict(sheet='Synthetic', cell='IW2')):
            body = self.task([asset]); body['task']['selectors'] = [selector]
            self.error(lambda: self.call('POST', '/v1/evidence-tasks', body))
        for policy in ('static', 'active'):
            self.error(lambda: self.call('POST', '/v1/evidence-tasks', self.task([asset], policy=policy)), ['cell_selector_requires_conditional'])

    def test_formula_blank_boolean_and_text_are_not_numeric_cell_operands(self):
        rows = [(2, [('B', 'formula', 42), ('C', 'text', '42')]), (3, [('B', 'number', 0)])]
        raw = workbook([('Synthetic', rows)])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive: xml = archive.read('xl/worksheets/sheet1.xml').decode()
        raw = replace_package_parts(raw, {'xl/worksheets/sheet1.xml': xml.replace('</row>', '<c r="E2" t="b"><v>1</v></c></row>', 1)})
        asset = self.ingest(raw, self.selection('B2:E3'))['asset_id']
        for address in ('B2', 'C2', 'D2', 'E2'):
            self.assert_hold(self.call('POST', '/v1/evidence-tasks', self.task([asset], (address,))))
        result = self.call('POST', '/v1/evidence-tasks', self.task([asset], ('B3',)))
        self.assertEqual(result['claims'][0]['value'], '0')

    def test_selection_metadata_tamper_rejects_uncached_and_same_transaction_cached_resolve(self):
        asset = self.ingest(selection=self.selection('B2:B2'))
        ref = asset['final_job']['result']['segments'][0]['evidence']
        with self.store.tx() as db:
            self.api.library.resolve(db, 'phase8-review', ref)
            row = db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=1', ('phase8-review', asset['asset_id'])).fetchone()
            meta = json.loads(row['meta']); meta['xlsx_selection'] = self.selection('C2:C2')
            db.execute('UPDATE sources SET meta=? WHERE tenant=? AND asset=? AND version=1', (canonical(meta), 'phase8-review', asset['asset_id']))
            self.error(lambda: self.api.library.resolve(db, 'phase8-review', ref), ['analysis_selection_integrity'])
        with self.store.tx() as db: self.error(lambda: self.api.library.resolve(db, 'phase8-review', ref), ['evidence_integrity_failed'])

    def test_parse_cache_alone_cannot_bypass_changed_selection(self):
        asset = self.ingest(selection=self.selection('B2:B2')); ref = asset['final_job']['result']['segments'][0]['evidence']
        with self.store.tx() as db:
            self.api.library.resolve(db, 'phase8-review', ref)
            self.store.extraction_cache.clear()
            row = db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=1', ('phase8-review', asset['asset_id'])).fetchone()
            meta = json.loads(row['meta']); meta['xlsx_selection'] = self.selection('C2:C2')
            db.execute('UPDATE sources SET meta=? WHERE tenant=? AND asset=? AND version=1', (canonical(meta), 'phase8-review', asset['asset_id']))
            self.error(lambda: self.api.library.resolve(db, 'phase8-review', ref))

    def test_selection_new_source_version_acl_creator_tenant_and_delete(self):
        asset = self.ingest(selection=self.selection('B2:B2'))['asset_id']; body = self.task([asset]); key = 'phase8-independent-replay'
        first = self.call('POST', '/v1/evidence-tasks', body, key=key); path = '/v1/evidence-tasks/' + first['id']
        for token in ('other', 'foreign'): self.error(lambda: self.call('GET', path, token=token))
        self.store.resource_access = lambda *args: False
        self.error(lambda: self.call('GET', path)); self.error(lambda: self.call('POST', '/v1/evidence-tasks', body, key=key))
        self.store.resource_access = self.api.governance.asset_allowed
        self.ingest(selection=self.selection('C2:C2'), asset=asset)
        self.error(lambda: self.call('GET', path), ['evidence_task_basis_changed'])
        self.call('DELETE', '/v1/assets/' + asset, {})
        self.error(lambda: self.call('POST', '/v1/evidence-tasks', body, key=key), ['deleted_dependency'])

    def test_cancelled_or_revoked_selected_job_never_publishes_values(self):
        asset = self.ingest(selection=self.selection('B2:B2'), finish=False)
        cancelled = self.call('POST', '/v1/jobs/' + asset['job']['id'] + '/cancel', dict(expected_revision=asset['job']['revision']))
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertFalse(self.api.library.run_once())
        self.assert_hold(self.call('POST', '/v1/evidence-tasks', self.task([asset['asset_id']])))
        queued = self.ingest(selection=self.selection('B2:B2'), finish=False)
        self.auth.revoke('owner'); self.assertFalse(self.api.library.run_once())
        with self.store.tx() as db:
            job = self.store.job(db, 'phase8-review', queued['job']['id'])
            self.assertNotEqual(job['state'], 'succeeded'); self.assertIsNone(job['result'])


if __name__ == '__main__': unittest.main()
