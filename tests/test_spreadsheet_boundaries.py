"""Independent synthetic-only Phase7 evidence task review.

No experiment runner, public original, gold, question or annotation is imported.
Assertions check actual values and pinned source support, beyond HTTP/schema success.
"""
import base64
import copy
import io
import json
import unittest
import zipfile
from decimal import Decimal
from xml.sax.saxutils import escape

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest
from navigator.parsing import parse_source
from navigator.store import Store
from navigator.xlsx_ingest import MEDIA, source_snapshot


def workbook(sheets):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    parts = {'[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'}
    definitions, relationships = [], []
    for i, (name, rows) in enumerate(sheets, 1):
        definitions.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="s{i}"/>')
        relationships.append(f'<Relationship Id="s{i}" Target="worksheets/sheet{i}.xml" Type="{rel}/worksheet"/>')
        rendered = []
        for row_index, cells in rows:
            values = []
            for col, kind, value in cells:
                attr = ' t="inlineStr"' if kind == 'text' else ''
                payload = '<is><t>' + escape(value) + '</t></is>' if kind == 'text' else '<v>' + escape(str(value)) + '</v>'
                if kind == 'formula':
                    payload = '<f>1+1</f><v>' + escape(str(value)) + '</v>'
                values.append(f'<c r="{col}{row_index}"{attr}>{payload}</c>')
            rendered.append(f'<row r="{row_index}">' + ''.join(values) + '</row>')
        parts[f'xl/worksheets/sheet{i}.xml'] = f'<worksheet xmlns="{ns}"><sheetData>' + ''.join(rendered) + '</sheetData></worksheet>'
    parts['xl/workbook.xml'] = f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>' + ''.join(definitions) + '</sheets></workbook>'
    parts['xl/_rels/workbook.xml.rels'] = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(relationships) + '</Relationships>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return out.getvalue()


def simple(value=42, sheet='SheetA'):
    return [(sheet, [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
                     (2, [('A', 'text', 'Target'), ('B', 'number', value)])])]


def replace_package_parts(raw, changes):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts.update(changes)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return out.getvalue()


class IndependentPhase7Review(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.auth = Auth({
            'owner': dict(tenant='review-A', actor='owner', revision=1, actor_type='human', scopes=['read', 'write', 'delete']),
            'other': dict(tenant='review-A', actor='other', revision=1, actor_type='human', scopes=['read']),
            'foreign': dict(tenant='review-B', actor='owner', revision=1, actor_type='human', scopes=['read']),
            'read-only': dict(tenant='review-A', actor='owner', revision=1, actor_type='human', scopes=['read']),
        })
        self.api = API(self.store, self.auth)
        self.n = 0

    def tearDown(self):
        self.store.close()

    def key(self):
        self.n += 1
        return 'phase7-independent-' + str(self.n)

    def call(self, method, path, body=None, token='owner', key=None):
        return self.api.dispatch(method, path, token, body, key or (self.key() if method in ('POST', 'DELETE') else None))

    def ingest(self, sheets, *, asset=None, finish=True):
        raw = workbook(sheets)
        body = dict(name='Independent synthetic workbook', media_type=MEDIA, content_base64=base64.b64encode(raw).decode())
        if asset:
            body['asset_id'] = asset
        _, result, _ = self.call('POST', '/v1/assets', body)
        if finish:
            self.assertTrue(self.api.library.run_once())
            job = self.call('GET', '/v1/jobs/' + result['job']['id'])[1]
            self.assertEqual(job['state'], 'succeeded', job)
        return result['asset_id']

    def body(self, assets, **task):
        return dict(query='Target Value', asset_ids=assets, task=dict(kind='table_lookup', selectors=[dict(row='Target', column='Value')], **task))

    def create(self, assets, *, body=None, key=None):
        return self.call('POST', '/v1/evidence-tasks', body or self.body(assets), key=key)[1]

    def error(self, fn, codes=None):
        with self.assertRaises(APIError) as caught:
            fn()
        if codes:
            self.assertIn(caught.exception.code, codes)
        return caught.exception

    def assert_answer(self, value, expected, asset):
        self.assertEqual(value['state'], 'answered_calculation', value)
        self.assertEqual(Decimal(value['claims'][0]['value']), Decimal(str(expected)))
        for operand in value['claims'][0]['operands']:
            support = operand['support_evidence']
            self.assertEqual(support['evidence']['asset_id'], asset)
            with self.store.tx() as db:
                resolved = self.api.library.resolve(db, 'review-A', support['evidence'])
            self.assertEqual(support['quote'], resolved['quote'])
            self.assertEqual(support['quote_context_sha256'], digest(resolved['quote'].encode()))
            self.assertEqual(support['source_sha256'], resolved['source_sha256'])
            self.assertEqual(support['locator'], resolved['locator'])

    def test_real_source_value_and_idempotent_replay(self):
        asset = self.ingest(simple())
        key = self.key()
        first = self.create([asset], key=key)
        self.assert_answer(first, 42, asset)
        self.assertEqual(self.create([asset], key=key), first)
        self.assertEqual(self.call('GET', '/v1/evidence-tasks/' + first['id'])[1], first)

    def test_explicit_scope_prevents_same_label_wrong_source(self):
        expected = self.ingest(simple(42))
        wrong = self.ingest(simple(999))
        self.assert_answer(self.create([expected]), 42, expected)
        self.assertNotEqual(expected, wrong)

    def test_two_sources_same_sheet_are_not_silently_conflated(self):
        a, b = self.ingest(simple(42)), self.ingest(simple(999))
        value = self.create([a, b])
        self.assertEqual(value['state'], 'conditions_conflict')
        self.assertEqual(value['claims'], [])

    def test_stale_header_cannot_bind_a_later_table(self):
        asset = self.ingest([('SheetA', [(1, [('A', 'text', 'FirstTable'), ('B', 'text', 'Value')]),
            (2, [('A', 'text', 'Unrelated'), ('B', 'number', 10)]),
            (4, [('A', 'text', 'SecondTable'), ('B', 'text', 'OtherMetric')]),
            (5, [('A', 'text', 'Target'), ('B', 'number', 999)])])])
        value = self.create([asset])
        self.assertNotEqual(value['state'], 'answered_calculation', 'Old B1 header must not certify B5 of a different table')
        self.assertEqual(value['claims'], [])

    def test_multiple_header_candidates_in_one_column_require_hold(self):
        asset = self.ingest([('SheetA', [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')]),
            (2, [('B', 'text', 'Value')]), (3, [('A', 'text', 'Target'), ('B', 'number', 42)])])])
        value = self.create([asset])
        self.assertNotEqual(value['state'], 'answered_calculation', 'Duplicate candidate headers have not been disambiguated')

    def test_header_change_after_initial_eight_rows_cannot_reuse_old_header(self):
        asset = self.ingest([('SheetA', [(1, [('A', 'text', 'FirstTable'), ('B', 'text', 'Value')]),
            (2, [('A', 'text', 'Unrelated'), ('B', 'number', 10)]),
            (10, [('A', 'text', 'SecondTable'), ('B', 'text', 'OtherMetric')]),
            (11, [('A', 'text', 'Target'), ('B', 'number', 999)])])])
        for mode in ('typed', 'flat'):
            with self.subTest(mode=mode):
                body = self.body([asset]); body['memory_mode'] = mode
                value = self.create([asset], body=body)
                self.assertNotEqual(value['state'], 'answered_calculation', 'Header candidate truncation cannot establish later table scope')
                self.assertEqual(value['claims'], [])

    def test_source_title_is_not_ignored(self):
        asset = self.ingest(simple())
        body = self.body([asset]); body['task']['selectors'][0]['source_title'] = 'DifferentWorkbook'
        value = self.create([asset], body=body)
        self.assertEqual(value['state'], 'needs_confirmation')
        self.assertEqual(value['confirmation']['key'], 'source_reference')
        self.assertEqual(value['claims'], [])

    def test_confirmation_replay_and_user_cannot_inject_a_numeric_operand(self):
        asset = self.ingest(simple(42, 'SheetA') + simple(999, 'SheetB'))
        key = self.key(); pending = self.create([asset], key=key)
        self.assertEqual(pending['state'], 'needs_confirmation')
        self.assertEqual(self.create([asset], key=key), pending)
        body = dict(expected_revision=1, observations=[dict(key=pending['confirmation']['key'], status='provided', value='SheetA')])
        update_key = self.key()
        answered = self.call('POST', '/v1/evidence-tasks/' + pending['id'] + '/observations', body, key=update_key)[1]
        self.assert_answer(answered, 42, asset)
        self.assertEqual(self.call('POST', '/v1/evidence-tasks/' + pending['id'] + '/observations', body, key=update_key)[1], answered)
        self.assertEqual(self.create([asset], key=key), pending)
        self.error(lambda: self.call('POST', '/v1/evidence-tasks/' + pending['id'] + '/observations', dict(expected_revision=2, observations=[dict(key='value', status='provided', value='777')])), ['unrequested_observation'])

    def test_unknown_sheet_and_contradictory_observations_are_not_answers(self):
        asset = self.ingest(simple(42, 'SheetA') + simple(999, 'SheetB'))
        for observations, expected in (([dict(key='sheet:0', status='unknown', value=None)], 'context_unknown'),
            ([dict(key='sheet:0', status='provided', value='SheetA'), dict(key='sheet:0', status='provided', value='SheetB')], 'conditions_conflict')):
            value = self.create([asset])
            result = self.call('POST', '/v1/evidence-tasks/' + value['id'] + '/observations', dict(expected_revision=1, observations=observations))[1]
            self.assertEqual(result['state'], expected)
            self.assertEqual(result['claims'], [])

    def test_creator_tenant_and_revoked_token_gates(self):
        asset = self.ingest(simple()); result = self.create([asset])
        for token in ('other', 'foreign'):
            self.error(lambda: self.call('GET', '/v1/evidence-tasks/' + result['id'], token=token), ['not_found'])
        self.auth.revoke('owner')
        self.error(lambda: self.call('GET', '/v1/evidence-tasks/' + result['id']), ['authentication_required'])

    def test_acl_rechecked_for_saved_task_and_receipt(self):
        asset = self.ingest(simple()); key = self.key(); result = self.create([asset], key=key)
        self.store.resource_access = lambda db, tenant, actor, asset_id: False
        self.error(lambda: self.call('GET', '/v1/evidence-tasks/' + result['id']), ['permission_revoked'])
        self.error(lambda: self.create([asset], key=key), ['permission_revoked'])

    def test_deletion_erases_snapshots_and_blocks_receipts(self):
        asset = self.ingest(simple()); key = self.key(); result = self.create([asset], key=key)
        self.call('DELETE', '/v1/assets/' + asset, {})
        self.error(lambda: self.call('GET', '/v1/evidence-tasks/' + result['id']), ['deleted_dependency'])
        self.error(lambda: self.create([asset], key=key), ['deleted_dependency'])
        with self.store.tx() as db:
            records = [json.loads(row['data']) for row in db.execute("SELECT data FROM records WHERE kind IN ('evidence_task_internals','evidence_task_snapshots')")]
        self.assertTrue(records)
        self.assertTrue(all(value == dict(id=value['id'], state='invalidated') for value in records))

    def test_new_source_version_invalidates_saved_basis(self):
        asset = self.ingest(simple()); result = self.create([asset])
        self.ingest(simple(99), asset=asset)
        self.error(lambda: self.call('GET', '/v1/evidence-tasks/' + result['id']), ['evidence_task_basis_changed'])

    def test_tampered_claim_and_undeclared_support_fail_closed(self):
        asset = self.ingest(simple()); result = self.create([asset])
        with self.store.tx() as db:
            for mutation in ('value', 'evidence'):
                tampered = copy.deepcopy(result)
                if mutation == 'value':
                    tampered['claims'][0]['value'] = '777'
                else:
                    tampered['claims'][0]['operands'][0]['support_evidence']['evidence']['asset_id'] = 'f' * 32
                self.error(lambda: self.api.evidence_assist.verify_response(db, self.auth.authenticate('owner'), tampered, '/v1/evidence-tasks/{id}'), ['evidence_task_response_integrity'])

    def test_same_snapshot_and_receipt_tampering_still_recomputes_decision(self):
        asset = self.ingest(simple()); key = self.key(); result = self.create([asset], key=key)
        changed = copy.deepcopy(result); changed['claims'][0]['value'] = '777'
        with self.store.tx() as db:
            snap_id = digest([result['id'], 1])[:32]
            snap = self.store.record(db, 'review-A', 'evidence_task_snapshots', snap_id)
            snap['response'] = changed
            db.execute("UPDATE records SET data=? WHERE tenant=? AND kind='evidence_task_snapshots' AND id=?", (canonical(snap), 'review-A', snap_id))
            db.execute('UPDATE idempotency SET data=? WHERE tenant=? AND actor=? AND key=?', (canonical(changed), 'review-A', 'owner', key))
        self.error(lambda: self.create([asset], key=key), ['evidence_task_decision_integrity'])

    def test_precise_sum_does_not_round_source_value(self):
        literal = '123456789012345678901234567890'
        asset = self.ingest(simple(literal))
        value = self.create([asset], body=self.body([asset], operation='sum'))
        self.assert_answer(value, literal, asset)

    def test_flat_and_typed_preserve_precise_source_value(self):
        literal = '0.123456789123456789'
        asset = self.ingest(simple(literal))
        for mode in ('flat', 'typed'):
            body = self.body([asset]); body['memory_mode'] = mode
            value = self.create([asset], body=body)
            self.assert_answer(value, literal, asset)

    def test_extreme_exponent_arithmetic_is_bounded_and_fail_closed(self):
        asset = self.ingest(simple('1e1000000'))
        for operation, precision in (('identity', 2), ('sum', 0)):
            with self.subTest(operation=operation):
                body = self.body([asset], operation=operation, precision=precision)
                try:
                    value = self.create([asset], body=body)
                except APIError as error:
                    self.assertIn(error.status, (413, 422))
                else:
                    self.assertNotEqual(value['state'], 'answered_calculation')
                    self.assertEqual(value['claims'], [])

    def test_units_are_explicitly_unverified(self):
        asset = self.ingest(simple())
        value = self.create([asset], body=self.body([asset], unit='totally imaginary unit'))
        self.assert_answer(value, 42, asset)
        self.assertEqual(value['claims'][0]['unit_status'], 'not_semantically_verified')
        self.assertEqual(value['stages']['applicability']['source_validity'], 'unassessed')
        self.assertEqual(value['stages']['decision'], 'arithmetic_result_only_authority_unassessed')

    def test_requested_as_of_cannot_be_silently_ignored(self):
        asset = self.ingest(simple())
        value = self.create([asset], body=self.body([asset], as_of='9999-01-01'))
        self.assertEqual(value['state'], 'context_unknown')
        self.assertEqual(value['claims'], [])

    def test_two_confirmation_budget_holds_third_unresolved_selector(self):
        rows = [(1, [('A', 'text', 'Category'), ('B', 'text', 'Value')])]
        rows += [(i + 2, [('A', 'text', 'Target' + str(i)), ('B', 'number', i + 10)]) for i in range(3)]
        asset = self.ingest([('SheetA', rows), ('SheetB', rows)])
        body = self.body([asset], operation='sum')
        body['query'] = 'SheetA SheetB'
        body['task']['selectors'] = [dict(row='Target' + str(i), column='Value') for i in range(3)]
        value = self.create([asset], body=body)
        for i in range(2):
            self.assertEqual(value['state'], 'needs_confirmation')
            self.assertEqual(value['confirmation']['key'], 'sheet:' + str(i))
            value = self.call('POST', '/v1/evidence-tasks/' + value['id'] + '/observations',
                dict(expected_revision=value['revision'], observations=[dict(key=value['confirmation']['key'], status='provided', value='SheetA')]))[1]
        self.assertEqual(value['state'], 'context_unknown')
        self.assertEqual(value['claims'], [])
        self.assertEqual(value['budgets']['prompts'], 2)
        self.assertEqual(value['budgets']['updates'], 2)

    def test_empty_asset_allowlist_cannot_fall_back_to_all_sources(self):
        self.ingest(simple(999))
        value = self.create([])
        self.assertEqual(value['scope'], [])
        self.assertEqual(value['excerpts'], [])
        self.assertEqual(value['claims'], [])

    def test_unrequested_and_invalid_observation_does_not_advance_revision(self):
        asset = self.ingest(simple(42, 'SheetA') + simple(999, 'SheetB'))
        pending = self.create([asset])
        for observation in (dict(key='sheet:0', status='unknown', value='SheetA'),
                            dict(key='sheet:0', status='provided', value=None),
                            dict(key='sheet:1', status='provided', value='SheetA')):
            self.error(lambda: self.call('POST', '/v1/evidence-tasks/' + pending['id'] + '/observations',
                dict(expected_revision=1, observations=[observation])), ['unrequested_observation'])
            self.assertEqual(self.call('GET', '/v1/evidence-tasks/' + pending['id'])[1], pending)

    def test_formula_caches_cannot_become_trusted_numeric_values(self):
        sheets = simple(); sheets[0][1][1][1][1] = ('B', 'formula', 42)
        asset = self.ingest(sheets)
        value = self.create([asset])
        self.assertNotEqual(value['state'], 'answered_calculation')
        self.assertEqual(value['claims'], [])

    def test_request_fields_and_limits_are_enforced_without_model(self):
        asset = self.ingest(simple())
        for change in ('query', 'selectors', 'oracle'):
            body = self.body([asset])
            if change == 'query': body['query'] = 'x' * 1001
            elif change == 'selectors': body['task']['selectors'] *= 21
            else: body['decision'] = dict(state='answered_calculation')
            self.error(lambda: self.create([asset], body=body))
        body = self.body([asset]); body['ranking_mode'] = 'hybrid'
        self.error(lambda: self.create([asset], body=body), ['text_embedding_model_unavailable'])
        value = self.create([asset])
        self.assertLessEqual(value['budgets']['searches'], 3)
        self.assertLessEqual(value['budgets']['context_characters'], 8000)
        self.assertLessEqual(len(value['excerpts']), 20)
        self.assertLessEqual(len(value['memory']), 256)
        self.assertIsNone(value['model_used'])

    def test_xlsx_untrusted_xml_macro_and_external_link_boundaries(self):
        raw = workbook(simple())
        changes = [({'extra.xml': '<!DOCTYPE x [<!ENTITY injected "boom">]><x>&injected;</x>'}, 'xlsx_invalid'),
            ({'xl/vbaProject.bin': b'inert'}, 'xlsx_feature_unsupported'),
            ({'extra.rels': '<Relationships><Relationship Id="external" TargetMode="External" Target="https://example.invalid/"/></Relationships>'}, 'xlsx_external_link_unsupported'),
            ({'../outside.xml': '<x/>'}, 'xlsx_invalid')]
        for parts, code in changes:
            with self.subTest(code=code):
                self.error(lambda: source_snapshot(replace_package_parts(raw, parts)), [code])

    def test_source_segment_cell_tamper_cannot_be_used_for_arithmetic(self):
        asset = self.ingest(simple()); result = self.create([asset])
        support = result['claims'][0]['operands'][0]['support_evidence']
        with self.store.tx() as db:
            ref = support['evidence']
            row = db.execute('SELECT data FROM segments WHERE tenant=? AND id=?', ('review-A', ref['segment_id'])).fetchone()
            tampered = json.loads(row['data'])
            tampered['fields']['cells'][1]['value'] = 777
            db.execute('UPDATE segments SET data=? WHERE tenant=? AND id=?', (canonical(tampered), 'review-A', ref['segment_id']))
        self.error(lambda: self.call('GET', '/v1/evidence-tasks/' + result['id']), ['evidence_integrity_failed'])

    def test_header_candidate_capacity_fails_instead_of_silent_truncation(self):
        rows = [(i, [('A', 'text', 'Label' + str(i)), ('B', 'text', 'Header' + str(i))]) for i in range(1, 130)]
        raw = workbook([('SheetA', rows)])
        error = self.error(lambda: parse_source(dict(raw=raw, sha=digest(raw), asset='a' * 32, version=1, meta=dict(media_type=MEDIA))), ['xlsx_header_candidate_limit'])
        self.assertEqual(error.status, 413)


if __name__ == '__main__':
    unittest.main()
