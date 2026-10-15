"""Persist verified source excerpts without generating a factual answer."""
from .contracts import APIError, SCHEMAS, canonical, validate
from .store import uid

NOTICE = ('関連する原文引用。引用の存在は質問の結論を支持する判定ではありません。'
          '質問の結論・安全性・業務判断は生成していません。')


class Assist:
    def __init__(self, store, library):
        self.store, self.library = store, library

    def _verify_excerpt(self, db, tenant, item):
        source = self.library.resolve(db, tenant, item['evidence'])
        for key, value in source.items():
            if key not in item or canonical(item[key]) != canonical(value):
                raise APIError(409, 'evidence_integrity_failed')
        return canonical(source['evidence'])

    def verify_saved(self, db, tenant, value):
        """Revalidate a stored or replayed answer against its exact originals."""
        try:
            validate(value, SCHEMAS['Answer'])
        except APIError as error:
            raise APIError(409, 'answer_integrity_failed') from error
        references = set()
        for item in value['excerpts']:
            reference = self._verify_excerpt(db, tenant, item)
            if reference in references:
                raise APIError(409, 'answer_integrity_failed')
            references.add(reference)
        expected_status = ('insufficient_evidence' if not references else
                           'review_required' if len(references) > 1 or any(i.get('review_required') for i in value['excerpts']) else
                           'evidence_found')
        expected_reason = ('no_matching_evidence' if not references else
                           'ocr_observations_require_review' if any(i.get('review_required') for i in value['excerpts']) else
                           'conflicting_or_distinct_records' if len(references) > 1 else
                           'source_quotes_only')
        if (value['status'] != expected_status or value['reason'] != expected_reason or
                value['notice'] != NOTICE):
            raise APIError(409, 'answer_integrity_failed')
        from .grounded import Grounded
        Grounded(self.store,self.library)._verify_retrieval_metadata(value)
        return value

    def answer(self, db, ctx, body):
        if body.get('mode', 'extractive') == 'generative':
            raise APIError(503, 'generation_model_unavailable')

        search = self.library.search(db, ctx['tenant'], body)
        excerpts, references = [], set()
        for item in search['results']:
            # Search relevance is not evidence integrity or conclusion support.
            # Resolve again before persisting any source content, and reject an
            # excerpt whose content disagrees with its pinned source reference.
            reference = self._verify_excerpt(db, ctx['tenant'], item)
            if reference in references:
                continue
            references.add(reference)
            excerpts.append(item)

        # Different records need a human to establish their relationship, even
        # when their values happen to agree. We do not infer that two readings
        # describe the same event, or that a quotation answers the question.
        ocr_review=any(i.get('review_required') for i in excerpts)
        review = len(references) > 1 or ocr_review
        value = dict(
            id=uid(), mode='extractive',
            status=('insufficient_evidence' if not excerpts else
                    'review_required' if review else 'evidence_found'),
            reason=('no_matching_evidence' if not excerpts else
                    'ocr_observations_require_review' if ocr_review else
                    'conflicting_or_distinct_records' if review else
                    'source_quotes_only'),
            query=body['query'], filters=body.get('filters', {}),
            retrieval_mode=search['mode'], score_meaning=search['score_meaning'],
            assessment_scope='returned_excerpts', conclusion_support='unassessed',
            excerpts=excerpts, model_used=None, generated_answer=None,
            notice=NOTICE,
        )
        if 'retrieval_model_version' in search:
            value['retrieval_model_version']=search['retrieval_model_version']
        assets = sorted({item['evidence']['asset_id'] for item in excerpts})
        self.store.save_record(db, ctx['tenant'], 'answers', value, assets)
        return 201, value, assets
