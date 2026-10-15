"""Independent deterministic calculation→JSON report bridge boundary review.

All sources/templates are synthetic; no gold/original/model files are imported.
"""
import base64
import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest
from navigator.store import Store
from navigator.xlsx_ingest import MEDIA
from tests.test_spreadsheet_boundaries import workbook


class ReportBridgeIndependentReview(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:'); self.n = 0
        scopes = ['read', 'write', 'delete', 'export']
        self.auth = Auth({'owner': dict(tenant='bridge-review', actor='owner', revision=1, scopes=scopes),
            'other': dict(tenant='bridge-review', actor='other', revision=1, scopes=scopes),
            'foreign': dict(tenant='bridge-other', actor='owner', revision=1, scopes=scopes),
            'reader': dict(tenant='bridge-review', actor='owner', revision=1, scopes=['read'])})
        self.api = API(self.store, self.auth)

    def tearDown(self): self.store.close()

    def call(self, method, path, body=None, token='owner', key=None):
        self.n += 1
        return self.api.dispatch(method, path, token, body,
            key or ('bridge-independent-' + str(self.n) if method in ('POST', 'DELETE') else None))[1]

    def ingest(self, values=(4, 6), *, asset=None, sheet='Synthetic', finish=True):
        rows = [(i + 1, [('B', 'number', value)]) for i, value in enumerate(values)]
        raw = workbook([(sheet, rows)])
        body = dict(name='Synthetic bridge source', media_type=MEDIA, content_base64=base64.b64encode(raw).decode())
        if asset: body['asset_id'] = asset
        value = self.call('POST', '/v1/assets', body)
        if finish:
            self.assertTrue(self.api.library.run_once())
            self.assertEqual(self.call('GET', '/v1/jobs/' + value['job']['id'])['state'], 'succeeded')
        return value['asset_id']

    def calculation(self, assets, *, selectors=None, **task):
        plan = dict(kind='table_calculation', operation='sum', selectors=selectors or [dict(sheet='Synthetic', cell='B1'), dict(sheet='Synthetic', cell='B2')])
        plan.update(task)
        return self.call('POST', '/v1/evidence-tasks', dict(query='Synthetic source', task=plan, asset_ids=assets, retrieval_policy='conditional'))

    def template(self, count=2, token='owner'):
        fields = [dict(field='field' + str(i), cell='A' + str(i + 1), required=True) for i in range(count)]
        raw = json.dumps(dict(format='navigator-cell-layout-v1', fields=fields)).encode()
        return self.call('POST', '/v1/templates', dict(name='Synthetic bridge template', content_base64=base64.b64encode(raw).decode()), token)

    def reference(self, task, index=0): return dict(task_id=task['id'], revision=task['revision'], claim_index=index)

    def body(self, template, task, index=0):
        return dict(template_id=template['id'], assignments=[dict(field='field0', calculation=self.reference(task, index))])

    def report(self, template, task, **kwargs): return self.call('POST', '/v1/reports', self.body(template, task), **kwargs)

    def error(self, fn, codes=None):
        with self.assertRaises(APIError) as caught: fn()
        if codes: self.assertIn(caught.exception.code, codes)
        return caught.exception

    def stored(self, db, kind, id): return self.store.record(db, 'bridge-review', kind, id)

    def update(self, db, kind, id, value):
        db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?', (canonical(value), 'bridge-review', kind, id))

    def test_computed_sum_is_placed_with_full_provenance_and_unknown_field_retained(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template()
        report = self.report(template, task)
        cell = report['cells'][0]
        self.assertEqual(cell['value'], '10')
        self.assertEqual(cell['value_type'], 'calculated_decimal')
        self.assertEqual(cell['status'], 'copied')
        self.assertFalse(cell['formula_injection_risk'])
        self.assertIsNone(cell['evidence']); self.assertIsNone(cell['source_quote'])
        calc = cell['calculation']
        self.assertEqual(calc['reference'], self.reference(task))
        self.assertEqual(calc['response_sha256'], digest(task))
        self.assertEqual(calc['claim'], task['claims'][0])
        self.assertEqual(calc['scope'], task['scope'])
        self.assertEqual(calc['task'], task['task'])
        self.assertEqual(len(calc['claim']['operands']), 2)
        self.assertEqual(report['required_unresolved_fields'], ['field1'])
        self.assertTrue(report['review_required'])
        self.assertFalse(report['external_submission'])
        self.assertFalse(report['rendered_office_file'])
        self.assertEqual(self.call('GET', '/v1/reports/' + report['id']), report)

    def test_negative_rounding_and_multi_lookup_claim_index_match_exact_values(self):
        asset = self.ingest(('1.235', '-2.345'))
        task = self.calculation([asset], operation='identity', precision=2)
        template = self.template()
        body = dict(template_id=template['id'], assignments=[dict(field='field' + str(i), calculation=self.reference(task, i)) for i in range(2)])
        report = self.call('POST', '/v1/reports', body)
        self.assertEqual([Decimal(c['value']) for c in report['cells']], [Decimal('1.24'), Decimal('-2.35')])
        self.assertEqual([c['calculation']['claim']['operands'][0]['value'] for c in report['cells']], ['1.235', '-2.345'])
        self.assertTrue(all(c['status'] == 'copied' and not c['formula_injection_risk'] for c in report['cells']))
        self.assertEqual(report['required_unresolved_fields'], [])

    def test_report_value_operand_task_scope_and_response_hash_tamper_rejected(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template(); original = self.report(template, task)
        for change in ('value', 'operand', 'task', 'scope', 'hash', 'reference', 'type', 'status', 'mapping'):
            with self.subTest(change=change):
                altered = copy.deepcopy(original); cell = altered['cells'][0]
                if change == 'value': cell['value'] = '999'
                elif change == 'operand': cell['calculation']['claim']['operands'][0]['value'] = '999'
                elif change == 'task': cell['calculation']['task']['operation'] = 'ratio'
                elif change == 'scope': cell['calculation']['scope'][0]['source_sha256'] = '0' * 64
                elif change == 'hash': cell['calculation']['response_sha256'] = '0' * 64
                elif change == 'reference': cell['calculation']['reference']['claim_index'] = 19
                elif change == 'type': cell['value_type'] = 'literal'
                elif change == 'status': cell['status'] = 'unknown'
                else: cell['cell'] = 'Z99'
                with self.store.tx() as db: self.update(db, 'reports', original['id'], altered)
                self.error(lambda: self.call('GET', '/v1/reports/' + original['id']))
                with self.store.tx() as db: self.update(db, 'reports', original['id'], original)

    def test_calculation_snapshot_and_source_operand_tamper_block_saved_report(self):
        asset = self.ingest(); task = self.calculation([asset]); report = self.report(self.template(), task)
        snap_id = digest([task['id'], task['revision']])[:32]
        with self.store.tx() as db:
            snap = self.stored(db, 'evidence_task_snapshots', snap_id); saved = copy.deepcopy(snap)
            snap['response']['claims'][0]['value'] = '999'
            self.update(db, 'evidence_task_snapshots', snap_id, snap)
        self.error(lambda: self.call('GET', '/v1/reports/' + report['id']), ['evidence_task_decision_integrity'])
        with self.store.tx() as db:
            self.update(db, 'evidence_task_snapshots', snap_id, saved)
            ref = task['claims'][0]['operands'][0]['support_evidence']['evidence']
            row = db.execute('SELECT data FROM segments WHERE tenant=? AND id=?', ('bridge-review', ref['segment_id'])).fetchone()
            changed = json.loads(row['data']); changed['fields']['cells'][0]['value'] = 999
            db.execute('UPDATE segments SET data=? WHERE tenant=? AND id=?', (canonical(changed), 'bridge-review', ref['segment_id']))
        self.error(lambda: self.call('GET', '/v1/reports/' + report['id']), ['evidence_integrity_failed'])

    def test_idempotent_replay_never_duplicates_report_or_dependency(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template(); body = self.body(template, task)
        first = self.call('POST', '/v1/reports', body, key='bridge-review-stable-replay')
        second = self.call('POST', '/v1/reports', body, key='bridge-review-stable-replay')
        self.assertEqual(second, first)
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='reports'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM dependencies WHERE kind='reports' AND id=?", (first['id'],)).fetchone()[0], 1)
        changed = copy.deepcopy(body); changed['assignments'][0]['calculation']['claim_index'] = 1
        self.error(lambda: self.call('POST', '/v1/reports', changed, key='bridge-review-stable-replay'), ['idempotency_conflict'])

    def test_creator_tenant_export_scope_and_current_acl_apply_on_render_get_replay(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template(); body = self.body(template, task)
        first = self.call('POST', '/v1/reports', body, key='bridge-review-scope-replay'); path = '/v1/reports/' + first['id']
        self.error(lambda: self.call('POST', '/v1/reports', body, token='other'), ['not_found'])
        self.error(lambda: self.call('GET', path, token='other'), ['not_found'])
        self.error(lambda: self.call('GET', path, token='foreign'), ['not_found'])
        self.error(lambda: self.call('POST', '/v1/reports', body, token='reader'), ['scope_denied'])
        self.store.resource_access = lambda *args: False
        self.error(lambda: self.call('GET', path), ['permission_revoked'])
        self.error(lambda: self.call('POST', '/v1/reports', body, key='bridge-review-scope-replay'), ['permission_revoked'])
        self.store.resource_access = self.api.governance.asset_allowed
        self.auth.revoke('owner')
        self.error(lambda: self.call('GET', path), ['authentication_required'])

    def test_stale_source_version_invalidates_render_get_and_receipt(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template(); body = self.body(template, task)
        first = self.call('POST', '/v1/reports', body, key='bridge-review-stale-source')
        self.ingest((8, 9), asset=asset)
        self.error(lambda: self.call('POST', '/v1/reports', body), ['evidence_task_basis_changed'])
        self.error(lambda: self.call('GET', '/v1/reports/' + first['id']), ['evidence_task_basis_changed'])
        self.error(lambda: self.call('POST', '/v1/reports', body, key='bridge-review-stale-source'), ['evidence_task_basis_changed'])

    def test_multiple_source_and_comparison_dependencies_deleted_on_any_source(self):
        a = self.ingest((4,), sheet='SourceA'); b = self.ingest((6,), sheet='SourceB')
        target = self.ingest((10,), sheet='Published')
        selectors = [dict(sheet='SourceA', cell='B1'), dict(sheet='SourceB', cell='B1')]
        task = self.calculation([a, b, target], selectors=selectors, compare_to=dict(sheet='Published', cell='B1'))
        report = self.report(self.template(), task, key='bridge-review-multisource')
        self.assertEqual(report['cells'][0]['value'], '10')
        self.assertEqual(report['cells'][0]['calculation']['claim']['comparison']['result'], 'equal')
        with self.store.tx() as db:
            deps = {row['asset'] for row in db.execute("SELECT asset FROM dependencies WHERE kind='reports' AND id=?", (report['id'],))}
        self.assertEqual(deps, {a, b, target})
        self.call('DELETE', '/v1/assets/' + target, {})
        self.error(lambda: self.call('GET', '/v1/reports/' + report['id']), ['deleted_dependency'])
        with self.store.tx() as db:
            tombstone = json.loads(db.execute("SELECT data FROM records WHERE kind='reports' AND id=?", (report['id'],)).fetchone()['data'])
            self.assertEqual(tombstone, dict(id=report['id'], state='invalidated'))

    def test_incomplete_unknown_reference_and_nonconditional_task_are_not_report_values(self):
        template = self.template(); pending_asset = self.ingest(finish=False)
        pending = self.calculation([pending_asset])
        self.error(lambda: self.report(template, pending), ['report_calculation_not_complete'])
        self.assertTrue(self.api.library.run_once())
        asset = self.ingest()
        unknown = self.calculation([asset], selectors=[dict(sheet='Synthetic', cell='C99')])
        self.error(lambda: self.report(template, unknown), ['report_calculation_not_complete'])
        reference = self.call('POST', '/v1/evidence-tasks', dict(query='Synthetic', asset_ids=[asset], task=dict(kind='reference')))
        self.error(lambda: self.report(template, reference), ['report_calculation_not_complete'])
        raw = workbook([('Synthetic', [(1, [('A', 'text', 'Category'), ('B', 'text', 'Amount')]), (2, [('A', 'text', 'Target'), ('B', 'number', 4)])])])
        ingested = self.call('POST', '/v1/assets', dict(name='Synthetic labels', media_type=MEDIA, content_base64=base64.b64encode(raw).decode()))
        self.assertTrue(self.api.library.run_once())
        task = self.call('POST', '/v1/evidence-tasks', dict(query='Target Amount', asset_ids=[ingested['asset_id']], retrieval_policy='active',
            task=dict(kind='table_lookup', selectors=[dict(row='Target', column='Amount')])) )
        self.assertEqual(task['state'], 'answered_calculation')
        self.error(lambda: self.report(template, task), ['report_calculation_not_complete'])

    def test_unknown_task_revision_claim_and_mixed_assignment_union_are_rejected(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template()
        for key, value, codes in (('task_id', 'f'*32, ['not_found']), ('revision', 2, ['calculation_revision_changed']),
                                 ('claim_index', 19, ['report_calculation_claim_not_found'])):
            body = self.body(template, task); body['assignments'][0]['calculation'][key] = value
            self.error(lambda: self.call('POST', '/v1/reports', body), codes)
        body = self.body(template, task); body['assignments'][0]['value'] = '999'
        self.error(lambda: self.call('POST', '/v1/reports', body))
        body = self.body(template, task); body['assignments'][0].update(source_field='/cells/0/value', evidence=task['claims'][0]['operands'][0]['support_evidence']['evidence'])
        self.error(lambda: self.call('POST', '/v1/reports', body))

    def test_duplicate_mapping_and_eleven_calculations_reject_atomically(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template(11)
        body = self.body(template, task); body['assignments'] *= 2
        self.error(lambda: self.call('POST', '/v1/reports', body), ['invalid_report_mapping'])
        body['assignments'] = [dict(field='field' + str(i), calculation=self.reference(task)) for i in range(11)]
        self.error(lambda: self.call('POST', '/v1/reports', body), ['report_calculation_limit'])
        with self.store.tx() as db: self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='reports'").fetchone()[0], 0)
        body['assignments'] = body['assignments'][:10]
        report = self.call('POST', '/v1/reports', body)
        self.assertEqual(sum(c['value_type'] == 'calculated_decimal' for c in report['cells']), 10)
        self.assertEqual(report['required_unresolved_fields'], ['field10'])

    def test_mid_render_auth_revocation_rolls_back_report_and_receipt(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template()
        original = self.api.evidence_assist.report_claim
        def revoke_after_claim(*args):
            value = original(*args); self.auth.revoke('owner'); return value
        with patch.object(self.api.evidence_assist, 'report_claim', side_effect=revoke_after_claim):
            self.error(lambda: self.report(template, task, key='bridge-review-revoked-mid-render'))
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='reports'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM idempotency WHERE key=?', ('bridge-review-revoked-mid-render',)).fetchone()[0], 0)

    def test_mixed_literal_calculation_and_unknown_fields_keep_separate_provenance(self):
        asset = self.ingest(); task = self.calculation([asset]); template = self.template(3)
        operand = task['claims'][0]['operands'][0]
        body = self.body(template, task)
        body['assignments'].append(dict(field='field1', evidence=operand['support_evidence']['evidence'], source_field='/cells/0/value'))
        report = self.call('POST', '/v1/reports', body)
        self.assertEqual([c['value_type'] for c in report['cells']], ['calculated_decimal', 'literal', 'literal'])
        self.assertEqual([c['value'] for c in report['cells']], ['10', 4, None])
        self.assertIsNotNone(report['cells'][1]['evidence']); self.assertNotIn('calculation', report['cells'][1])
        self.assertEqual(report['required_unresolved_fields'], ['field2'])

    def test_original_sha_corruption_blocks_saved_calculated_value(self):
        asset = self.ingest(); task = self.calculation([asset]); report = self.report(self.template(), task)
        with self.store.tx() as db:
            db.execute('UPDATE sources SET sha=? WHERE tenant=? AND asset=? AND version=1', ('0'*64, 'bridge-review', asset))
        self.error(lambda: self.call('GET', '/v1/reports/' + report['id']), ['source_integrity_failed'])

    def test_confirmation_required_and_old_task_revision_cannot_supply_a_report(self):
        asset = self.ingest(); template = self.template()
        selectors = [dict(sheet='Synthetic', cell='B1', source_title='Caller alias'), dict(sheet='Synthetic', cell='B2')]
        pending = self.calculation([asset], selectors=selectors)
        self.assertEqual(pending['state'], 'needs_confirmation')
        self.error(lambda: self.report(template, pending), ['report_calculation_not_complete'])
        completed = self.call('POST', '/v1/evidence-tasks/' + pending['id'] + '/observations',
            dict(expected_revision=1, observations=[dict(key='source_reference', status='provided', value=asset)]))
        self.assertEqual(completed['state'], 'answered_calculation')
        self.assertEqual(completed['revision'], 2)
        self.error(lambda: self.report(template, pending), ['calculation_revision_changed'])
        report = self.report(template, completed)
        self.assertEqual(report['cells'][0]['value'], '10')
        self.assertEqual(report['cells'][0]['calculation']['reference']['revision'], 2)


if __name__ == '__main__': unittest.main()
