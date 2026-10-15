"""Synthetic-only independent review of policy-v2/parser-v3 changes.

Imports only prior synthetic ZIP helpers, never experiment or answer-key data.
"""
import base64
from decimal import Decimal
import unittest

from navigator.api import API, Auth
from navigator.contracts import APIError, digest
from navigator.parsing import parse_source
from navigator.store import Store
from navigator.xlsx_ingest import MEDIA
from tests.test_spreadsheet_boundaries import workbook, simple, replace_package_parts


class PolicyV2IndependentReview(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.auth = Auth({'owner': dict(tenant='policy-v2-review', actor='owner', revision=1,
            scopes=['read', 'write', 'delete'], actor_type='human')})
        self.api = API(self.store, self.auth)
        self.n = 0

    def tearDown(self):
        self.store.close()

    def call(self, method, path, body=None, key=None):
        self.n += 1
        return self.api.dispatch(method, path, 'owner', body,
            key or ('policy-v2-independent-' + str(self.n) if method in ('POST', 'DELETE') else None))[1]

    def ingest(self, sheets, changes=None):
        raw = workbook(sheets)
        if changes: raw = replace_package_parts(raw, changes)
        value = self.call('POST', '/v1/assets', dict(name='Synthetic policy-v2', media_type=MEDIA,
            content_base64=base64.b64encode(raw).decode()))
        self.assertTrue(self.api.library.run_once())
        self.assertEqual(self.call('GET', '/v1/jobs/' + value['job']['id'])['state'], 'succeeded')
        return value['asset_id']

    def body(self, assets, **task):
        return dict(query='Target Value', asset_ids=assets,
            task=dict(kind='table_lookup', selectors=[dict(row='Target', column='Value')], **task))

    def create(self, assets, body=None, key=None):
        return self.call('POST', '/v1/evidence-tasks', body or self.body(assets), key)

    def observe(self, value, supplied=None, status='provided'):
        return self.call('POST', '/v1/evidence-tasks/' + value['id'] + '/observations',
            dict(expected_revision=value['revision'], observations=[dict(key=value['confirmation']['key'], status=status, value=supplied)]))

    def assert_hold(self, value):
        self.assertNotEqual(value['state'], 'answered_calculation', dict(state=value['state'], reason=value['reason'], claims=[c['value'] for c in value['claims']]))
        self.assertEqual(value['claims'], [])

    def assert_claims(self, value, expected, asset):
        self.assertEqual(value['state'], 'answered_calculation', value)
        self.assertEqual([Decimal(c['value']) for c in value['claims']], [Decimal(str(x)) for x in expected])
        for claim in value['claims']:
            for operand in claim['operands']:
                support = operand['support_evidence']
                self.assertEqual(support['evidence']['asset_id'], asset)
                with self.store.tx() as db:
                    original = self.api.library.resolve(db, 'policy-v2-review', support['evidence'])
                self.assertEqual(support['quote'], original['quote'])
                self.assertEqual(support['source_sha256'], original['source_sha256'])
                self.assertEqual(support['quote_context_sha256'], digest(original['quote'].encode()))

    def period_sheet(self, label='2020年基準', sheet='SheetA', value=42):
        return [(sheet, [(1, [('A', 'text', label), ('B', 'text', 'Value')]),
                        (2, [('A', 'text', 'Target'), ('B', 'number', value)])])]

    def test_literal_base_period_positive_and_narrow_scope(self):
        asset = self.ingest(self.period_sheet())
        value = self.create([asset], self.body([asset], as_of='2020年基準'))
        self.assert_claims(value, [42], asset)
        applicability = value['stages']['applicability']
        self.assertEqual(applicability['verification_kind'], 'literal_dataset_base_period_only')
        self.assertEqual(applicability['source_validity'], 'unassessed')
        self.assertTrue(applicability['supports'])

    def test_wrong_base_year_and_arbitrary_date_hold(self):
        asset = self.ingest(self.period_sheet())
        for period in ('2021年基準', '2020-01-01', '2020年基準 extra', '２０２０年基準'):
            with self.subTest(period=period):
                self.assert_hold(self.create([asset], self.body([asset], as_of=period)))

    def test_sheet_name_cannot_substitute_for_displayed_base_period(self):
        asset = self.ingest(simple(42, sheet='2020年基準'))
        self.assert_hold(self.create([asset], self.body([asset], as_of='2020年基準')))

    def test_base_year_substring_inside_five_digit_year_is_not_match(self):
        asset = self.ingest(self.period_sheet(label='12020年基準'))
        self.assert_hold(self.create([asset], self.body([asset], as_of='2020年基準')))

    def test_missing_row_one_in_target_sheet_cannot_verify_base_period(self):
        sheets = self.period_sheet(sheet='DeclaredSheet') + [('TargetSheet', [
            (2, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
            (3, [('A', 'text', 'Target'), ('B', 'number', 99)])])]
        asset = self.ingest(sheets)
        body = self.body([asset], as_of='2020年基準'); body['task']['selectors'][0]['sheet'] = 'TargetSheet'
        self.assert_hold(self.create([asset], body))

    def test_mismatched_base_period_in_second_sheet_holds(self):
        asset = self.ingest(self.period_sheet(sheet='SheetA') + self.period_sheet(label='2021年基準', sheet='SheetB'))
        self.assert_hold(self.create([asset], self.body([asset], as_of='2020年基準')))

    def test_alias_requires_source_selection_even_single_source(self):
        asset = self.ingest(simple())
        body = self.body([asset]); body['task']['selectors'][0]['source_title'] = 'UntrustedAlias'
        key = 'policy-v2-alias-replay'
        pending = self.create([asset], body, key)
        self.assertEqual(pending['state'], 'needs_confirmation')
        self.assertEqual(pending['confirmation']['key'], 'source_reference')
        self.assertEqual(pending['confirmation']['options'], [asset])
        self.assert_hold(pending)
        option = pending['stages']['source_options'][0]
        self.assertEqual(option['asset_id'], asset)
        self.assertEqual(option['support_evidence']['evidence']['asset_id'], asset)
        answered = self.observe(pending, asset)
        self.assert_claims(answered, [42], asset)
        self.assertEqual(self.create([asset], body, key), pending)

    def test_alias_selection_prevents_wrong_source_and_scope_injection(self):
        a, b = self.ingest(simple(42)), self.ingest(simple(999))
        outside = self.ingest(simple(777))
        body = self.body([a, b]); body['task']['selectors'][0]['source_title'] = 'Alias'
        pending = self.create([a, b], body)
        self.assert_claims(self.observe(pending, b), [999], b)
        pending = self.create([a, b], body)
        conflict = self.observe(pending, outside)
        self.assertEqual(conflict['state'], 'conditions_conflict'); self.assert_hold(conflict)
        pending = self.create([a, b], body)
        unknown = self.observe(pending, None, 'unknown')
        self.assertEqual(unknown['state'], 'context_unknown'); self.assert_hold(unknown)

    def test_multi_identity_lookup_outputs_all_selectors_with_correct_support(self):
        asset = self.ingest([('SheetA', [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
            (2, [('A', 'text', 'Target'), ('B', 'number', '0.123456789123456789')]),
            (3, [('A', 'text', 'Other'), ('B', 'number', 99)])])])
        for mode in ('typed', 'flat'):
            body = self.body([asset], operation='identity'); body['memory_mode'] = mode
            body['task']['selectors'] += [dict(row='Other', column='Value')]
            value = self.create([asset], body)
            self.assert_claims(value, ['0.123456789123456789', 99], asset)
            self.assertEqual([c['operation'] for c in value['claims']], ['lookup', 'lookup'])

    def test_multi_identity_precision_matches_single_identity_precision(self):
        asset = self.ingest([('SheetA', [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
            (2, [('A', 'text', 'Target'), ('B', 'number', '1.235')]),
            (3, [('A', 'text', 'Other'), ('B', 'number', '2.345')])])])
        body = self.body([asset], operation='identity', precision=2)
        single = self.create([asset], body)
        self.assert_claims(single, ['1.24'], asset)
        body['task']['selectors'] += [dict(row='Other', column='Value')]
        self.assert_claims(self.create([asset], body), ['1.24', '2.35'], asset)

    def hierarchy(self, parent='Group', leaf='Value', parent_row=1, leaf_row=2, merge='B1:C1'):
        rows = [(parent_row, [('B', 'text', parent)]), (leaf_row, [('A', 'text', 'Category'), ('B', 'text', leaf)]),
                (4, [('A', 'text', 'Target'), ('B', 'number', 42)])]
        raw = workbook([('SheetA', rows)])
        # Insert only a source merge definition; no implementation override.
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            sheet = archive.read('xl/worksheets/sheet1.xml').decode()
        return replace_package_parts(raw, {'xl/worksheets/sheet1.xml': sheet.replace('</worksheet>', f'<mergeCells><mergeCell ref="{merge}"/></mergeCells></worksheet>')})

    def ingest_raw(self, raw):
        value = self.call('POST', '/v1/assets', dict(name='Synthetic hierarchy', media_type=MEDIA, content_base64=base64.b64encode(raw).decode()))
        self.assertTrue(self.api.library.run_once())
        self.assertEqual(self.call('GET', '/v1/jobs/' + value['job']['id'])['state'], 'succeeded')
        return value['asset_id']

    def test_structural_merged_parent_positive(self):
        asset = self.ingest_raw(self.hierarchy())
        for mode in ('typed', 'flat'):
            body = self.body([asset]); body['memory_mode'] = mode
            self.assert_claims(self.create([asset], body), [42], asset)

    def test_vertical_merge_overlapping_leaf_or_parent_after_leaf_hold(self):
        for args in (dict(merge='B1:B1'), dict(merge='B1:C2'), dict(parent_row=2, leaf_row=1, merge='B2:C2')):
            with self.subTest(args=args):
                asset = self.ingest_raw(self.hierarchy(**args))
                self.assert_hold(self.create([asset]))

    def test_plain_stale_duplicate_and_late_headers_remain_hold(self):
        for header_row, text in ((2, 'Value'), (2, 'OtherMetric'), (10, 'OtherMetric')):
            rows = [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
                (header_row, [('B', 'text', text)]),
                (header_row + 1, [('A', 'text', 'Target'), ('B', 'number', 999)])]
            asset = self.ingest([('SheetA', rows)])
            for mode in ('typed', 'flat'):
                body = self.body([asset]); body['memory_mode'] = mode
                self.assert_hold(self.create([asset], body))

    def test_display_text_excludes_inline_and_shared_phonetics(self):
        ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        raw = workbook(simple())
        changes = {'xl/sharedStrings.xml': f'<sst xmlns="{ns}"><si><r><t>Target</t></r><rPh sb="0" eb="6"><t>phantom</t></rPh></si></sst>',
            'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><r><t>Category</t></r><rPh><t>hidden</t></rPh></is></c><c r="B1" t="inlineStr"><is><t>Value</t><rPh><t>ghost</t></rPh></is></c></row><row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2"><v>42</v></c></row></sheetData></worksheet>'}
        raw = replace_package_parts(raw, changes)
        extraction = parse_source(dict(raw=raw, sha=digest(raw), asset='a' * 32, version=1, meta=dict(media_type=MEDIA)))
        self.assertEqual(extraction['segments'][1]['fields']['labels'], ['Target'])
        self.assertNotIn('phantom', str(extraction['segments']))
        self.assertNotIn('ghost', str(extraction['segments']))
        asset = self.ingest_raw(raw)
        self.assert_claims(self.create([asset]), [42], asset)

    def test_arithmetic_precision_rounding_zero_denominator_and_large_input_bounds(self):
        asset = self.ingest([('SheetA', [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
            (2, [('A', 'text', 'Target'), ('B', 'number', 1)]),
            (3, [('A', 'text', 'Denominator'), ('B', 'number', 3)]),
            (4, [('A', 'text', 'Zero'), ('B', 'number', 0)]),
            (5, [('A', 'text', 'Huge'), ('B', 'number', '1e1000000')])])])
        body = self.body([asset], operation='ratio', precision=2)
        body['task']['kind'] = 'table_calculation'
        body['task']['selectors'] += [dict(row='Denominator', column='Value')]
        value = self.create([asset], body)
        self.assert_claims(value, ['0.33'], asset)
        self.assertTrue(value['claims'][0]['rounded'])
        body['task']['selectors'][1]['row'] = 'Zero'
        self.assert_hold(self.create([asset], body))
        body = self.body([asset], precision=2); body['task']['selectors'][0]['row'] = 'Huge'
        self.assert_hold(self.create([asset], body))

    def test_merged_parent_header_does_not_propagate_to_wrong_column(self):
        rows = [(1, [('B', 'text', 'Group')]),
            (2, [('A', 'text', 'Category'), ('B', 'text', 'Value'), ('D', 'text', 'OtherMetric')]),
            (3, [('A', 'text', 'Target'), ('B', 'number', 42), ('D', 'number', 999)])]
        raw = workbook([('SheetA', rows)])
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            sheet = archive.read('xl/worksheets/sheet1.xml').decode()
        raw = replace_package_parts(raw, {'xl/worksheets/sheet1.xml': sheet.replace('</worksheet>', '<mergeCells><mergeCell ref="B1:C1"/></mergeCells></worksheet>')})
        asset = self.ingest_raw(raw)
        self.assert_claims(self.create([asset]), [42], asset)
        body = self.body([asset]); body['task']['selectors'][0]['column'] = 'Group'
        self.assert_hold(self.create([asset], body))

    def test_adjacent_generic_parent_requires_distinct_leaf_and_peer(self):
        for peer, parent, expected in ((True, 'Group', True), (False, 'Group', False), (True, 'Value', False)):
            with self.subTest(peer=peer, parent=parent):
                leaf = [('B', 'text', 'Value')]
                if peer: leaf.insert(0, ('A', 'text', 'Category'))
                asset = self.ingest([('SheetA', [(1, [('B', 'text', parent)]), (2, leaf),
                    (3, [('A', 'text', 'Target'), ('B', 'number', 42)])])])
                for mode in ('typed', 'flat'):
                    body = self.body([asset]); body['memory_mode'] = mode
                    value = self.create([asset], body)
                    if expected: self.assert_claims(value, [42], asset)
                    else: self.assert_hold(value)

    def test_unit_scale_conversion_operations_hold_until_plan_exists(self):
        asset = self.ingest([('SheetA', [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
            (2, [('A', 'text', 'Target'), ('B', 'number', '0.15')]),
            (3, [('A', 'text', 'Other'), ('B', 'number', '0.10')])])])
        for operation in ('ratio', 'percentage_point'):
            body = self.body([asset], operation=operation, unit='percent points', precision=2)
            body['task']['selectors'] += [dict(row='Other', column='Value')]
            value = self.create([asset], body)
            self.assertEqual(value['state'], 'semantic_unavailable')
            self.assertEqual(value['reason'], 'unit_conversion_plan_not_implemented')
            self.assert_hold(value)


if __name__ == '__main__':
    unittest.main()
