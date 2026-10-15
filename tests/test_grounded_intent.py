"""Bounded intent policy tests authored without Phase3 evaluation case access."""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest

from navigator.contracts import canonical, digest
from navigator.grounded import (_assessment, _question_intent, SCORE_MEANINGS,
                                  INTENT_FIELDS, LEGACY_NOTICE)
from tests.test_grounded import GroundedFixture

TEST_RETRIEVER_METADATA = dict(model_id='test-only-local-retriever', revision='test-revision',
    dimension=3, precision='float32', query_prefix='query: ', document_prefix='',
    chunk_policy='unicode-400-overlap80-v1', metric='cosine', fusion='rrf-k60-bm25-dense-top20-v1')


class IntentPolicyTests(unittest.TestCase):
    def test_ordinary_english_and_japanese_select_only_existing_tasks(self):
        examples = {
            'What pressure limit does the manual specify?': 'maximum_allowable_pressure',
            '許容圧力の最大値はいくらですか': 'maximum_allowable_pressure',
            'How long should I wait after stopping the unit before switching the power off?': 'power_off_wait',
            '運転を停止した後、電源を切るまで何分待つのですか': 'power_off_wait',
            'What is the height range of the intermediate rail?': 'midrail_height',
            '中桟の高さの下限と上限を知りたい': 'midrail_height',
        }
        for query, expected in examples.items():
            with self.subTest(query=query):
                self.assertEqual(_question_intent(query), (expected, None, None))

    def test_measured_safety_current_and_multiple_questions_never_select_normative_fact(self):
        for query in (
                'What is the current pressure limit at the site?',
                'Is the maximum allowable pressure safe?',
                'Tell me the measured midrail height.',
                '中さんの高さを実測して合否を教えて',
                '電源を切るのは大丈夫ですか',
                'What are the maximum pressure and the midrail height?'):
            with self.subTest(query=query):
                field, scope, reason = _question_intent(query)
                self.assertIsNone(field)
                self.assertIsNone(scope)
                self.assertIsNotNone(reason)
        self.assertEqual(_question_intent('Is the maximum allowable pressure safe?', 'maximum_allowable_pressure'),
                         (None, None, 'unsupported_question_scope'))

    def test_reversed_wait_direction_is_unsupported(self):
        for query in ('How long must I wait after turning off power before stopping operation?',
                      '停止する前に、電源を切った後何分待つのか'):
            self.assertEqual(_question_intent(query)[0], None)
            self.assertEqual(_question_intent(query, 'power_off_wait')[2], 'unsupported_question_relation')
        self.assertEqual(_question_intent('How long is the interval between stopping operation and power off?')[0],
                         'power_off_wait')

    def test_scaffold_type_comes_from_query_words_not_page_numbers(self):
        for query in ('つり（棚）足場の中さんの高さは', '吊り足場の中桟の高さを教えて',
                      'What height must the mid-rail have on hanging scaffolds?',
                      'Give the midrail height range for suspended scaffolding'):
            self.assertEqual(_question_intent(query), ('midrail_height', 'suspended', None))
        self.assertEqual(_question_intent('つり足場と単管足場の中さんの高さの範囲')[2], 'ambiguous_source_context')
        self.assertEqual(_question_intent('What is the midrail height for mobile scaffolds?')[2], 'unsupported_source_context')
        self.assertEqual(_question_intent('中さん', 'midrail_height'), ('midrail_height', None, None))

    def test_ocr_confidence_never_becomes_claim_support(self):
        for confidence in (0.05, 0.99):
            item = dict(evidence=dict(analysis_version='native-image-ocr-v1:x'),
                        locator=dict(kind='image_bbox'), ocr_confidence=confidence,
                        quote='unreviewed unrelated OCR text')
            self.assertEqual(_assessment([item], 'maximum_allowable_pressure'),
                             ('hold', 'ocr_claim_support_unreviewed', []))

    def test_unrelated_ocr_does_not_hide_verified_fact_but_related_ocr_holds(self):
        sources = {
            'maximum_allowable_pressure': 'maximum allowable pressure is 725 psi (5.00 MPa)',
            'power_off_wait': 'Do not turn off the power immediately after stopping operation. '
                'Always wait for at least 9 minutes before turning off the power.',
            'midrail_height': '点検の内容例 －つり（棚）足場用－ 中さんの高さは30センチメートル以上45センチメートル以下か',
        }
        for field, quote in sources.items():
            native = dict(quote=quote, source_sha256='a' * 64, locator=dict(kind='lines', line_start=1, line_end=1),
                          evidence=dict(asset_id='a' * 32, source_version=1,
                                        analysis_version='structured-text-v1:x', segment_id='b' * 32))
            ocr = dict(native, quote='STANLEY 3m', locator=dict(kind='image_bbox'),
                       evidence={**native['evidence'], 'analysis_version': 'native-image-ocr-v1:x'})
            scope = 'suspended' if field == 'midrail_height' else None
            with self.subTest(field=field):
                status, _, claims = _assessment([native, ocr], field, scope)
                self.assertEqual(status, 'supported')
                self.assertEqual(claims[0]['support_evidence']['evidence'], native['evidence'])
                related = dict(ocr, quote=quote)
                self.assertEqual(_assessment([native, related], field, scope),
                                 ('hold', 'ocr_claim_support_unreviewed', []))
                self.assertEqual(_assessment([ocr], field, scope),
                                 ('hold', 'ocr_claim_support_unreviewed', []))


class IntentIntegrationTests(GroundedFixture, unittest.TestCase):
    def test_original_saved_payload_replays_without_rewriting_and_partial_new_keys_reject(self):
        asset = self.ingest(b'maximum allowable pressure is 725 psi (5.00 MPa)')
        key = self.key()
        _, answer = self.answer('maximum_allowable_pressure', asset=asset, key=key)
        legacy = {key: value for key, value in answer.items() if key not in INTENT_FIELDS}
        legacy['notice'] = LEGACY_NOTICE
        original_bytes = canonical(legacy)
        with self.store.tx() as db:
            db.execute("UPDATE records SET data=? WHERE kind='grounded_answers' AND id=?",
                       (original_bytes, answer['id']))
            db.execute("UPDATE idempotency SET data=? WHERE tenant='A' AND key=?", (original_bytes, key))
        status, restored, _ = self.api.dispatch('GET', '/v1/grounded-answers/' + answer['id'], 'a')
        self.assertEqual(status, 200)
        self.assertEqual(canonical(restored), original_bytes)
        status, replayed, _ = self.api.dispatch('POST', '/v1/grounded-answers', 'a',
            dict(query='maximum allowable pressure', requested_field='maximum_allowable_pressure',
                 limit=20, filters=dict(asset_ids=[asset])), key)
        self.assertEqual(status, 201)
        self.assertEqual(canonical(replayed), original_bytes)
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT data FROM records WHERE kind='grounded_answers' AND id=?",
                                       (answer['id'],)).fetchone()['data'], original_bytes)
            for missing in (['resolved_field'], list(INTENT_FIELDS)):
                corrupted = {key: value for key, value in answer.items() if key not in missing}
                self.assert_error(409, 'grounded_answer_integrity_failed',
                    lambda: self.grounded.verify_saved(db, 'A', corrupted))
            partial_legacy = dict(legacy, resolved_field='maximum_allowable_pressure')
            self.assert_error(409, 'grounded_answer_integrity_failed',
                lambda: self.grounded.verify_saved(db, 'A', partial_legacy))

    def test_source_heading_selects_subtype_and_cannot_be_replaced_by_query_text(self):
        for source, reason in (
                ('点検の内容例 －単管足場用－ 中さんの高さは30センチメートル以上45センチメートル以下か',
                 'requested_source_context_not_supported'),
                ('点検の内容例 中さんの高さは30センチメートル以上45センチメートル以下か つり足場',
                 'requested_source_context_not_supported')):
            asset = self.ingest(source.encode())
            _, held = self.answer(query='つり足場の中さんの高さの範囲', asset=asset)
            self.assertEqual(held['reason'], reason)
        asset = self.ingest('点検の内容例 －つり（棚）足場用－ 中さんの高さは30センチメートル以上45センチメートル以下か'.encode())
        _, answer = self.answer(query='つり足場の中さんの高さの範囲', asset=asset)
        self.assert_support(answer)
        self.assertIsNone(answer['requested_field'])
        self.assertEqual(answer['resolved_field'], 'midrail_height')
        self.assertEqual(answer['scaffold_type'], 'suspended')
        self.assertIn('つり（棚）足場用', answer['claims'][0]['support_evidence']['quote'])
        self.assertEqual(answer['claims'][0]['values'][0]['value'], '30')

    def test_saved_inferred_field_and_query_scope_are_recomputed(self):
        asset = self.ingest(b'maximum allowable pressure is 725 psi (5.00 MPa)')
        _, answer = self.answer(query='What is the maximum allowable pressure?', asset=asset)
        self.assert_support(answer)
        for field, replacement in (('resolved_field', 'midrail_height'),
                                   ('scaffold_type', 'suspended'),
                                   ('query', 'What is the actual measured maximum pressure?')):
            forged = json.loads(canonical(answer)); forged[field] = replacement
            with self.subTest(field=field), self.store.tx() as db:
                self.assert_error(409, 'grounded_answer_integrity_failed',
                    lambda: self.grounded.verify_saved(db, 'A', forged))

    def test_same_literal_support_policy_for_known_retrieval_modes(self):
        self.ingest(b'maximum allowable pressure is 725 psi (5.00 MPa)')
        _, found, _ = self.api.dispatch('POST', '/v1/search', 'a', dict(query='maximum allowable pressure'))
        original = self.api.library.search
        try:
            for mode, meaning in SCORE_MEANINGS.items():
                metadata = dict(retrieval_model_version=TEST_RETRIEVER_METADATA) if mode == 'hybrid' else {}
                self.api.library.search = lambda *args, mode=mode, meaning=meaning, metadata=metadata: dict(found, mode=mode, score_meaning=meaning, **metadata)
                _, answer = self.answer(query='What is the maximum allowable pressure?')
                self.assert_support(answer)
                self.assertEqual(answer['retrieval_mode'], mode)
        finally:
            self.api.library.search = original

    def test_hybrid_descriptor_schema_and_attached_encoder_metadata_are_checked(self):
        self.ingest(b'maximum allowable pressure is 725 psi (5.00 MPa)')
        _, found, _ = self.api.dispatch('POST', '/v1/search', 'a', dict(query='maximum allowable pressure'))
        original = self.api.library.search
        prior_encoder = getattr(self.api.library, 'encoder', None)
        try:
            self.api.library.encoder = SimpleNamespace(metadata=TEST_RETRIEVER_METADATA)
            self.api.library.search = lambda *args: dict(found, mode='hybrid',
                score_meaning=SCORE_MEANINGS['hybrid'], retrieval_model_version=TEST_RETRIEVER_METADATA)
            _, answer = self.answer(query='What is the maximum allowable pressure?')
            self.assert_support(answer)
            self.assertEqual(answer['retrieval_model_version'], TEST_RETRIEVER_METADATA)
            for patch in (dict(precision='int8'), dict(dimension=4), dict(extra='untrusted')):
                forged = json.loads(canonical(answer))
                forged['retrieval_model_version'].update(patch)
                with self.store.tx() as db:
                    self.assert_error(409, 'grounded_answer_integrity_failed',
                        lambda: self.grounded.verify_saved(db, 'A', forged))
        finally:
            self.api.library.search = original
            self.api.library.encoder = prior_encoder




if __name__ == '__main__':
    unittest.main()
