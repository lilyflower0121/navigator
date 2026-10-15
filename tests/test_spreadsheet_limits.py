"""Independent synthetic boundary review; no corpus/gold/model weight loads."""
import base64
import copy
from decimal import Decimal
import json
import unittest
from collections import OrderedDict
from contextlib import nullcontext
import threading
from types import SimpleNamespace
from unittest.mock import patch

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest
from navigator.local_decision import IsolatedLocalDecision, LocalQwenDecision, PROFILE, WEIGHT_SHA, parse_candidate
from navigator.store import Store
from navigator.xlsx_ingest import MEDIA
from tests.test_spreadsheet_boundaries import workbook


def table(values, name='Synthetic'):
    return (name, [(1, [('A', 'text', 'Category'), ('B', 'text', 'Amount')])] + [
        (i, [('A', 'text', label), ('B', 'number', value)]) for i, (label, value) in enumerate(values, 2)])


class TablePlanIndependentReview(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:'); self.n = 0
        self.auth = Auth({'owner': dict(tenant='v3-review', actor='owner', revision=1, scopes=['read', 'write', 'delete']),
            'other': dict(tenant='v3-review', actor='other', revision=1, scopes=['read']),
            'foreign': dict(tenant='other-tenant', actor='owner', revision=1, scopes=['read'])})
        self.api = API(self.store, self.auth)

    def tearDown(self): self.store.close()

    def call(self, method, path, body=None, key=None, token='owner'):
        self.n += 1
        return self.api.dispatch(method, path, token, body,
            key or ('independent-v3-' + str(self.n) if method in ('POST', 'DELETE') else None))[1]

    def ingest(self, values=None, *, finish=True, asset=None, sheets=None):
        raw = workbook(sheets or [table(values or [('Target', 4), ('Published', 4)])])
        body = dict(name='Synthetic v3 review', media_type=MEDIA, content_base64=base64.b64encode(raw).decode())
        if asset: body['asset_id'] = asset
        value = self.call('POST', '/v1/assets', body)
        if finish:
            self.assertTrue(self.api.library.run_once())
            self.assertEqual(self.call('GET', '/v1/jobs/' + value['job']['id'])['state'], 'succeeded')
        return value['asset_id']

    def selector(self, row='Target', **fields): return dict(row=row, column='Amount', **fields)

    def body(self, assets, *, rows=('Target',), policy='conditional', **fields):
        return dict(query='Synthetic Amount', asset_ids=assets, retrieval_policy=policy,
            task=dict(kind='table_calculation', operation='sum', selectors=[self.selector(row) for row in rows], **fields))

    def create(self, body, key=None): return self.call('POST', '/v1/evidence-tasks', body, key)

    def assert_hold(self, value):
        self.assertNotEqual(value['state'], 'answered_calculation', dict(state=value['state'], reason=value['reason'], claims=[c.get('value') for c in value['claims']]))
        self.assertEqual(value['claims'], [])
        self.assertFalse(value['stages']['explicit_task_complete'])

    def observe(self, value, given):
        return self.call('POST', '/v1/evidence-tasks/' + value['id'] + '/observations', dict(expected_revision=value['revision'], observations=[
            dict(key=value['confirmation']['key'], status='provided', value=given)]))

    def assert_answer(self, value, expected, asset):
        self.assertEqual(value['state'], 'answered_calculation', value['reason'])
        self.assertEqual([Decimal(c['value']) for c in value['claims']], [Decimal(str(v)) for v in expected])
        self.assertTrue(value['stages']['explicit_task_complete'])
        for claim in value['claims']:
            supports = [o['support_evidence'] for o in claim['operands']]
            if claim.get('comparison'): supports.append(claim['comparison']['target']['support_evidence'])
            for support in supports:
                self.assertEqual(support['evidence']['asset_id'], asset)
                with self.store.tx() as db: resolved = self.api.library.resolve(db, 'v3-review', support['evidence'])
                self.assertEqual(support['quote'], resolved['quote'])
                self.assertEqual(support['quote_context_sha256'], digest(resolved['quote'].encode()))
                self.assertEqual(support['source_sha256'], resolved['source_sha256'])

    def test_alias_on_second_operand_prompts_before_missing_memory_failure(self):
        asset = self.ingest()
        body = self.body([asset], rows=('Target', 'Published'))
        body['task']['selectors'][1]['source_title'] = 'Caller alias'
        pending = self.create(body)
        self.assertEqual(pending['state'], 'needs_confirmation')
        self.assertEqual(pending['confirmation']['key'], 'source_reference')
        self.assert_answer(self.observe(pending, asset), [8], asset)

    def test_alias_on_comparison_prompts_and_selects_pinned_source(self):
        asset = self.ingest()
        body = self.body([asset], compare_to=self.selector('Published', source_title='Caller alias'))
        pending = self.create(body)
        self.assertEqual(pending['state'], 'needs_confirmation')
        self.assertEqual(pending['confirmation']['key'], 'source_reference')
        result = self.observe(pending, asset)
        self.assert_answer(result, [4], asset)
        self.assertEqual(result['claims'][0]['comparison']['result'], 'equal')

    def test_empty_sum_operand_plan_cannot_synthesize_zero_from_comparison_only(self):
        asset = self.ingest()
        for policy in ('static', 'active', 'conditional'):
            with self.subTest(policy=policy):
                self.assert_hold(self.create(self.body([asset], rows=(), policy=policy, compare_to=self.selector('Published'))))

    def test_missing_required_operand_and_comparison_cannot_be_complete(self):
        asset = self.ingest()
        for body in (self.body([asset], rows=('Target', 'Missing')),
                     self.body([asset], compare_to=self.selector('Missing'))):
            value = self.create(body); self.assert_hold(value)
            self.assertIn(value['reason'], ('selector_not_resolved_in_returned_memory', 'comparison_selector_not_resolved'))

    def test_comparison_target_is_pinned_not_a_task_supplied_value(self):
        asset = self.ingest([('Target', 4), ('Published', 99)])
        value = self.create(self.body([asset], compare_to=self.selector('Published')))
        self.assert_answer(value, [4], asset)
        comparison = value['claims'][0]['comparison']
        self.assertEqual(comparison['result'], 'different')
        self.assertEqual(comparison['target']['value'], '99')
        self.assertTrue(value['stages']['explicit_task_complete'])

    def test_scale_and_comparison_rounding_negative_half_up(self):
        asset = self.ingest([('Target', '-1.235'), ('Published', '-2.47')])
        body = self.body([asset], scale='2', precision=1, comparison_precision=2, compare_to=self.selector('Published'))
        value = self.create(body)
        self.assert_answer(value, ['-2.5'], asset)
        claim = value['claims'][0]
        self.assertEqual(claim['comparison']['compared_value'], '-2.47')
        self.assertEqual(claim['comparison']['result'], 'equal')
        self.assertEqual(claim['constants'][0]['provenance'], 'explicit_task_input')
        self.assertIsNone(claim['constants'][0]['source_evidence'])

    def test_percentage_then_explicit_scale_records_both_constants(self):
        asset = self.ingest([('Target', 1), ('Whole', 4)])
        body = self.body([asset], rows=('Target', 'Whole'), scale='.01')
        body['task']['operation'] = 'percentage'
        value = self.create(body); self.assert_answer(value, ['.25'], asset)
        self.assertEqual({c['role'] for c in value['claims'][0]['constants']}, {'scale', 'percentage_multiplier'})
        self.assertTrue(all(c['source_evidence'] is None for c in value['claims'][0]['constants']))

    def test_failed_or_queued_other_scope_asset_has_priority_over_ready_value(self):
        ready = self.ingest()
        pending = self.ingest(finish=False)
        for policy in ('static', 'active', 'conditional'):
            body = self.body([ready, pending], policy=policy, as_of='wrong period')
            value = self.create(body); self.assert_hold(value)
            self.assertEqual(value['state'], 'extraction_incomplete')
            self.assertEqual(value['budgets']['searches'], 0)
            self.assertEqual(value['excerpts'], [])

    def test_conditional_exact_lookup_does_not_call_ranker_or_model(self):
        asset = self.ingest()
        with patch.object(self.api.library, 'search', side_effect=AssertionError('ranked search is forbidden in conditional policy')):
            body = self.body([asset]); body['ranking_mode'] = 'hybrid'; body['memory_mode'] = 'flat'
            value = self.create(body); self.assert_answer(value, [4], asset)
            self.assertIsNone(value['model_used'])
            self.assertEqual(value['stages']['memory_kind'], 'typed')

    def test_comparison_changed_source_revision_and_foreign_tenant_block_replay(self):
        asset = self.ingest()
        body = self.body([asset], compare_to=self.selector('Published'))
        key = 'independent-v3-version-comparison'
        value = self.create(body, key)
        with self.assertRaises(APIError) as caught: self.call('GET', '/v1/evidence-tasks/' + value['id'], token='foreign')
        self.assertEqual(caught.exception.code, 'not_found')
        self.ingest([('Target', 4), ('Published', 999)], asset=asset)
        with self.assertRaises(APIError) as caught: self.create(body, key)
        self.assertEqual(caught.exception.code, 'evidence_task_basis_changed')

    def test_default_api_keeps_model_disabled_and_request_cannot_inject_weight_path(self):
        asset = self.ingest()
        fresh = API(self.store, self.auth)
        body = dict(query='Synthetic Amount', task=dict(kind='reference'), asset_ids=[asset])
        with patch('navigator.local_decision.file_sha', side_effect=AssertionError('default API cannot read weights')):
            result = fresh.dispatch('POST', '/v1/evidence-tasks', 'owner', body, 'independent-v3-default-disabled')[1]
        self.assertIsNone(result['model_used']); self.assertIsNone(result['generated_answer'])
        self.assertEqual(result['claims'], [])
        self.assertNotIn('model_candidate', result['stages'])
        body['decision_weights_path'] = '/untrusted/path'
        with self.assertRaises(APIError) as caught:
            fresh.dispatch('POST', '/v1/evidence-tasks', 'owner', body, 'independent-v3-weight-injection')
        self.assertEqual(caught.exception.code, 'invalid_input_fields')


class LocalCandidateParserIndependentReview(unittest.TestCase):
    def source(self):
        quote = 'Synthetic limit is 42. Exception: category B is excluded.'
        return dict(quote=quote, evidence=dict(asset_id='a'*32, source_version=1, analysis_version='synthetic', segment_id='b'*32),
            source_sha256=digest(quote.encode()), locator=dict(kind='lines', line_start=1, line_end=1))

    def payload(self, **fields):
        body = dict(status='answer', answer='42', citations=[dict(index=1, quote='limit is 42')]); body.update(fields)
        return json.dumps(body)

    def test_even_contradictory_answer_with_valid_quote_is_unverified_candidate_only(self):
        result = parse_candidate(self.payload(answer='Category B is included and its limit is 999.'), [self.source()])
        self.assertEqual(result['status'], 'candidate')
        self.assertFalse(result['semantic_support_verified'])
        self.assertEqual(result['reason'], 'quote_identity_checked_semantic_support_unverified')

    def test_undeclared_boolean_index_nonliteral_quote_and_extra_field_are_rejected(self):
        for raw in (self.payload(citations=[dict(index=2, quote='limit is 42')]),
                    self.payload(citations=[dict(index=True, quote='limit is 42')]),
                    self.payload(citations=[dict(index=1, quote='limit is 999')]),
                    self.payload(source_path='/untrusted/weights')):
            self.assertEqual(parse_candidate(raw, [self.source()])['status'], 'rejected')

    def test_json_duplicate_keys_trailing_text_empty_citation_and_unknown_contract(self):
        for raw in ('{"status":"answer","status":"unknown","answer":null,"citations":[]}',
                    self.payload() + ' trailing', self.payload(citations=[]), self.payload(answer='')):
            self.assertEqual(parse_candidate(raw, [self.source()])['status'], 'rejected')
        declined = parse_candidate(self.payload(status='unknown', answer=None, citations=[]), [self.source()])
        self.assertEqual(declined['status'], 'unknown')
        self.assertNotIn('answer', declined)

    def test_whitespace_padding_does_not_bypass_answer_character_bound(self):
        result = parse_candidate(self.payload(answer=' ' * 1200 + '42'), [self.source()])
        self.assertEqual(result['status'], 'rejected')

    def test_escaped_unpaired_unicode_surrogate_is_rejected_before_utf8_output(self):
        result = parse_candidate(self.payload(answer='\ud800'), [self.source()])
        self.assertEqual(result['status'], 'rejected')


class WeightFreeLocalAdapterReview(unittest.TestCase):
    """Structural receipt/cache tests using explicit doubles, not inference proof."""
    def fixture(self, count=20, terminal=True):
        parsed = LocalCandidateParserIndependentReview()
        raw = parsed.payload()
        class Tokenizer:
            def __call__(self, prompt, **kwargs):
                self.last_prompt = prompt
                return {'input_ids': SimpleNamespace(shape=(1, count))}
            def decode(self, ids, **kwargs): return raw
        class Generated:
            def __getitem__(self, index): return SimpleNamespace(tolist=lambda: [17, 151645] if terminal else [17, 18])
        class Model:
            calls = 0
            def generate(self, **kwargs):
                self.calls += 1; self.arguments = kwargs
                return Generated()
        # Bypass only the constructor in a clearly marked structural test double.
        # Never call file_sha, load_file, torch, transformers, or a real tokenizer.
        adapter = object.__new__(LocalQwenDecision)
        adapter.metadata = dict(profile='explicit-test-double-not-real-model', cooperative_max_seconds=15)
        adapter.model = Model(); adapter.tokenizer = Tokenizer()
        adapter.torch = SimpleNamespace(inference_mode=lambda: nullcontext())
        adapter.lock = threading.Lock(); adapter.cache = OrderedDict()
        return adapter, [parsed.source()]

    def test_input_token_cap_blocks_without_model_generation_or_context_truncation(self):
        adapter, sources = self.fixture(count=257)
        value = adapter.decide('Synthetic query', {}, sources)
        self.assertEqual(value['status'], 'blocked')
        self.assertEqual(value['reason'], 'decision_input_token_limit')
        self.assertFalse(value['context_truncated'])
        self.assertEqual(adapter.model.calls, 0)

    def test_output_budget_exhaustion_rejects_syntactically_valid_json(self):
        adapter, sources = self.fixture(terminal=False)
        value = adapter.decide('Synthetic query', {}, sources)
        self.assertEqual(value['status'], 'rejected')
        self.assertEqual(value['reason'], 'model_generation_budget_exhausted')
        self.assertFalse(value['generation_terminated'])
        self.assertNotIn('max_time', adapter.model.arguments)
        self.assertEqual(adapter.model.arguments['max_new_tokens'], 64)

    def test_candidate_cache_is_copy_isolated_and_clear_forces_fresh_receipt(self):
        adapter, sources = self.fixture()
        first = adapter.decide('Synthetic query', {}, sources)
        self.assertEqual(first['status'], 'candidate')
        first['answer'] = 'tampered outside cache'
        second = adapter.decide('Synthetic query', {}, sources)
        self.assertEqual(second['answer'], '42')
        self.assertEqual(adapter.model.calls, 1)
        adapter.clear(); self.assertEqual(len(adapter.cache), 0)
        adapter.decide('Synthetic query', {}, sources)
        self.assertEqual(adapter.model.calls, 2)

    def test_pinned_hash_mismatch_rejects_before_framework_or_weight_load(self):
        with patch('navigator.local_decision.file_sha', return_value='0'*64):
            with self.assertRaisesRegex(ValueError, 'decision_weight_sha_mismatch'):
                LocalQwenDecision(weights='/synthetic/nonexistent', tokenizer='/synthetic/nonexistent')
        with patch('navigator.local_decision.file_sha', side_effect=[WEIGHT_SHA, '0'*64]):
            with self.assertRaisesRegex(ValueError, 'decision_tokenizer_sha_mismatch'):
                LocalQwenDecision(weights='/synthetic/nonexistent', tokenizer='/synthetic/nonexistent')


class IsolatedWorkerBoundaryReview(unittest.TestCase):
    def source(self): return LocalCandidateParserIndependentReview().source()

    def test_timeout_kills_and_reaps_only_owned_worker(self):
        import subprocess
        from unittest.mock import Mock
        process = Mock(); process.communicate.side_effect = [subprocess.TimeoutExpired('owned-worker', 30), ('', '')]
        with patch('subprocess.Popen', return_value=process) as launch:
            result = IsolatedLocalDecision().decide('Synthetic query', {}, [self.source()])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'decision_wall_time_limit')
        self.assertEqual(process.kill.call_count, 1)
        self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual(process.communicate.call_args_list[0].kwargs['timeout'], 30)
        self.assertIn('navigator.local_decision', launch.call_args.args[0])
        self.assertEqual(launch.call_args.kwargs['env']['HF_HUB_OFFLINE'], '1')

    def test_large_byte_context_is_blocked_without_subprocess(self):
        source = self.source(); source['quote'] = 'x' * 12001
        with patch('subprocess.Popen', side_effect=AssertionError('oversized request cannot launch weights')):
            result = IsolatedLocalDecision().decide('Synthetic query', {}, [source])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'decision_input_byte_limit')

    def test_nonobject_worker_json_is_rejected_without_internal_exception(self):
        from unittest.mock import Mock
        process = Mock(); process.returncode = 0; process.communicate.return_value = ('[]', '')
        with patch('subprocess.Popen', return_value=process):
            result = IsolatedLocalDecision().decide('Synthetic query', {}, [self.source()])
        self.assertEqual(result['status'], 'failed')


class ModelDiagnosticIntegrationReview(TablePlanIndependentReview):
    """API wiring with explicit doubles; never evidence of actual inference."""
    def setUp(self):
        super().setUp()
        class ReceiptDouble:
            metadata = dict(profile=PROFILE, weights_sha256=WEIGHT_SHA, semantic_quality_validated=False,
                explicit_test_double=True)
            calls = 0; clears = 0
            def decide(self, query, task, excerpts):
                self.calls += 1
                raw = json.dumps(dict(status='answer', answer='Deliberately unsupported 999',
                    citations=[dict(index=1, quote=excerpts[0]['quote'][:100])]))
                return dict(model=self.metadata, raw_output=raw, **parse_candidate(raw, excerpts))
            def clear(self): self.clears += 1
        self.double = ReceiptDouble()
        self.api = API(self.store, self.auth, decision_model=self.double)

    def reference(self, assets): return dict(query='Synthetic Amount', task=dict(kind='reference'), asset_ids=assets)

    def test_even_parseable_candidate_is_diagnostic_only_not_answer_or_claim(self):
        asset = self.ingest()
        result = self.create(self.reference([asset]))
        self.assertEqual(result['state'], 'semantic_unavailable')
        self.assertEqual(result['reason'], 'local_decision_quality_gate_not_passed')
        self.assertEqual(result['claims'], [])
        self.assertIsNone(result['generated_answer'])
        self.assertFalse(result['stages']['model_candidate']['semantic_support_verified'])
        self.assertEqual(self.double.calls, 1)
        caps = self.call('GET', '/v1/capabilities')
        self.assertFalse(caps['assist']['generative'])
        self.assertFalse(caps['assist']['general_language_decision'])
        self.assertTrue(caps['assist']['local_generation_diagnostic'])

    def test_blocked_without_raw_output_does_not_claim_model_execution(self):
        asset = self.ingest()
        receipt = dict(status='blocked', reason='decision_input_byte_limit', raw_output=None, output_tokens=0)
        with patch.object(self.double, 'decide', return_value=receipt):
            result = self.create(self.reference([asset]))
        self.assertIsNone(result['model_used'])
        self.assertFalse(result['stages']['actual_generation_returned'])
        self.assertEqual(result['stages']['configured_model_profile'], PROFILE)
        self.assertIsNone(result['generated_answer']); self.assertEqual(result['claims'], [])
        caps = self.call('GET', '/v1/capabilities')
        self.assertIsNone(caps['assist']['model_used'])
        self.assertEqual(caps['assist']['configured_model_profile'], PROFILE)

    def test_saved_replay_does_not_regenerate_after_cache_clear_and_disable(self):
        asset = self.ingest(); body = self.reference([asset]); key = 'independent-v3-diagnostic-replay'
        first = self.create(body, key)
        self.double.clear(); self.api.evidence_assist.decision_model = None
        with patch.object(self.double, 'decide', side_effect=AssertionError('saved response cannot rerun weights')):
            self.assertEqual(self.call('GET', '/v1/evidence-tasks/' + first['id']), first)
            self.assertEqual(self.create(body, key), first)
        self.assertEqual(self.double.calls, 1)

    def test_diagnostic_tenant_creator_acl_and_delete_clear_gates(self):
        asset = self.ingest(); body = self.reference([asset]); key = 'independent-v3-diagnostic-delete'
        first = self.create(body, key); path = '/v1/evidence-tasks/' + first['id']
        for token in ('foreign', 'other'):
            with self.assertRaises(APIError): self.call('GET', path, token=token)
        self.store.resource_access = lambda *args: False
        with self.assertRaises(APIError): self.create(body, key)
        self.store.resource_access = self.api.governance.asset_allowed
        self.call('DELETE', '/v1/assets/' + asset, {})
        self.assertEqual(self.double.clears, 1)
        with self.assertRaises(APIError): self.create(body, key)
        with self.store.tx() as db:
            records = [json.loads(row['data']) for row in db.execute("SELECT data FROM records WHERE kind IN ('evidence_task_internals','evidence_task_snapshots')")]
        self.assertTrue(all(value.get('state') == 'invalidated' for value in records))

    def test_saved_receipt_raw_tampering_is_rejected_without_regeneration(self):
        asset = self.ingest(); first = self.create(self.reference([asset]))
        with self.store.tx() as db:
            internal = self.store.record(db, 'v3-review', 'evidence_task_internals', first['id'])
            internal['generation_receipt']['receipt']['raw_output'] = '{"status":"unknown","answer":null,"citations":[]}'
            db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?',
                (canonical(internal), 'v3-review', 'evidence_task_internals', first['id']))
        with self.assertRaises(APIError) as caught: self.call('GET', '/v1/evidence-tasks/' + first['id'])
        self.assertIn(caught.exception.code, ('evidence_task_context_integrity', 'decision_receipt_integrity'))
        self.assertEqual(self.double.calls, 1)

    def test_receipt_sha_detects_raw_tamper_even_when_snapshot_input_is_also_changed(self):
        asset = self.ingest(); first = self.create(self.reference([asset]))
        with self.store.tx() as db:
            internal = self.store.record(db, 'v3-review', 'evidence_task_internals', first['id'])
            internal['generation_receipt']['receipt']['raw_output'] = '{"status":"unknown","answer":null,"citations":[]}'
            snap_id = digest([first['id'], 1])[:32]
            snapshot = self.store.record(db, 'v3-review', 'evidence_task_snapshots', snap_id)
            snapshot['state_input'] = internal
            for kind, id, record in (('evidence_task_internals', first['id'], internal), ('evidence_task_snapshots', snap_id, snapshot)):
                db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?', (canonical(record), 'v3-review', kind, id))
        with self.assertRaises(APIError) as caught: self.call('GET', '/v1/evidence-tasks/' + first['id'])
        self.assertEqual(caught.exception.code, 'decision_receipt_integrity')
        self.assertEqual(self.double.calls, 1)

    def test_authorization_revoked_during_candidate_generation_rolls_back_snapshot(self):
        asset = self.ingest(); generate = self.double.decide
        def revoke_after_generation(*args):
            result = generate(*args); self.auth.revoke('owner'); return result
        with patch.object(self.double, 'decide', side_effect=revoke_after_generation):
            with self.assertRaises(APIError): self.create(self.reference([asset]))
        with self.store.tx() as db:
            count = db.execute("SELECT count(*) FROM records WHERE kind IN ('evidence_task_internals','evidence_task_snapshots')").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == '__main__': unittest.main()
