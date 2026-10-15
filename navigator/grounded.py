"""Three explicit source-content extraction tasks; no natural-language model.

The reviewed grammar recognizes a pressure specification, a power-off waiting
instruction, and a midrail height inspection example. Values come from source
matches, never from the question, gold data, or a fixed answer table. A matching
quote establishes only what the saved source says, not real-site safety.
"""
import re
import unicodedata
from decimal import Decimal

from .contracts import (APIError, QUERY, SCHEMAS, array, canonical, digest,
                        integer, obj, string, validate)
from .store import uid

CONTRACT_VERSION = 'bounded-facts-v1'
INTENT_POLICY_VERSION = 'bounded-intent-en-ja-v1'
FIELDS = ['maximum_allowable_pressure', 'power_off_wait', 'midrail_height']
SCORE_MEANINGS = {
    'lexical': 'lexical_overlap; not factual confidence',
    'bm25': 'BM25 relevance; not factual confidence',
    'hybrid': 'RRF lexical+dense ranks; not factual confidence',
}
NOTICE = ('明示指定または限定した英日質問意図の3種類の原文事実のみを字句規則で抽出。LLM回答ではありません。'
          '原文の仕様・指示・点検内容例を、現場実測・合格・現在の安全性とみなしません。'
          '検索順位とOCR信頼度は主張の確信度ではありません。'
          'excerptsは検索候補であり、support_evidenceに採用されない引用は主張の根拠ではありません。')
LEGACY_NOTICE = ('明示指定された3種類の原文事実のみを字句規則で抽出。LLM回答ではありません。'
                 '原文の仕様・指示・点検内容例を、現場実測・合格・現在の安全性とみなしません。'
                 '検索順位とOCR信頼度は主張の確信度ではありません。')

# Exported for the API owner's contract registration. These schemas are also
# checked here, so callers bypassing HTTP cannot submit their own evidence.
GROUNDED_INPUT = obj(dict(
    **QUERY['properties'],
    requested_field=dict(type='string', enum=FIELDS),
    source_page=integer(1, 32)), ['query'])
GROUNDED_INPUT['properties'].setdefault('ranking_mode',
    dict(type='string', enum=['lexical', 'bm25', 'hybrid']))
RETRIEVAL_MODEL_VERSION = obj(dict(
    model_id=string(200), revision=string(160), dimension=integer(1, 65536),
    precision=dict(type='string', const='float32'),
    query_prefix=string(1000, 0), document_prefix=string(1000, 0),
    chunk_policy=dict(type='string', const='unicode-400-overlap80-v1'),
    metric=dict(type='string', const='cosine'),
    fusion=dict(type='string', const='rrf-k60-bm25-dense-top20-v1')))
FACT_VALUE = obj(dict(value=string(32), unit=string(40),
    operator=dict(type='string', enum=['equals', 'at_least', 'gte', 'lte'])))
SUPPORT = obj(dict(evidence={'$ref': '#/components/schemas/Evidence'},
    source_sha256=string(64, 64), locator={'$ref': '#/components/schemas/Locator'},
    quote=string(8192), start_char=integer(0, 8192), end_char=integer(1, 8192),
    offset_unit=dict(type='string', const='unicode_codepoints'),
    quote_context_sha256=string(64, 64)))
CLAIM = obj(dict(field=dict(type='string', enum=FIELDS),
    value_role=dict(type='string', enum=['source_specification',
        'source_instruction', 'inspection_example_criterion']),
    relation=dict(type='string', enum=['maximum_allowable_pressure',
        'after_stopping_operation_before_power_off', 'midrail_height_inclusive_bounds']),
    values=array(FACT_VALUE, 2, 1), support_evidence=SUPPORT))
GROUNDED_ANSWER = obj(dict(id=SCHEMAS['Evidence']['properties']['asset_id'],
    mode=dict(type='string', const='bounded_fact_extraction'),
    contract_version=dict(type='string', const=CONTRACT_VERSION),
    status=dict(type='string', enum=['supported', 'hold']), reason=string(100),
    query=QUERY['properties']['query'],
    requested_field=dict(type=['string', 'null'], enum=FIELDS + [None]),
    resolved_field=dict(type=['string', 'null'], enum=FIELDS + [None]),
    intent_policy_version=dict(type='string', const=INTENT_POLICY_VERSION),
    scaffold_type=dict(type=['string', 'null'], enum=['suspended', 'frame', 'tube', 'wedge', None]),
    filters=QUERY['properties']['filters'],
    source_page=dict(type=['integer', 'null'], minimum=1, maximum=32),
    retrieval_mode=dict(type='string', enum=list(SCORE_MEANINGS)), score_meaning=string(200),
    assessment_scope=dict(type='string', const='returned_excerpts'),
    claims=array(CLAIM, 1), excerpts=array({'$ref': '#/components/schemas/Match'}, 20),
    model_used=dict(type='null'), generated_answer=dict(type='null'), notice=string(500)))
GROUNDED_ANSWER['properties']['retrieval_model_version'] = RETRIEVAL_MODEL_VERSION
INTENT_FIELDS = {'resolved_field', 'intent_policy_version', 'scaffold_type'}
LEGACY_GROUNDED_ANSWER = obj({key: value for key, value in GROUNDED_ANSWER['properties'].items()
    if key not in INTENT_FIELDS | {'retrieval_model_version'}})
LEGACY_GROUNDED_ANSWER['properties']['notice'] = dict(type='string', const=LEGACY_NOTICE)
LEGACY_GROUNDED_ANSWER['properties']['retrieval_mode'] = dict(type='string', const='lexical')
GROUNDED_SCHEMAS = dict(GroundedInput=GROUNDED_INPUT,
    GroundedAnswer={'anyOf': [GROUNDED_ANSWER, LEGACY_GROUNDED_ANSWER]},
    GroundedSupport=SUPPORT, GroundedClaim=CLAIM)

NUMBER = r'[0-9]{1,9}(?:\.[0-9]{1,9})?'
SPACE = r'[ \t\r\n]{1,64}'
OPTIONAL_SPACE = r'[ \t\r\n]{0,64}'
PRESSURE = re.compile(
    rf'\bmaximum{SPACE}allowable{SPACE}pressure{SPACE}is{SPACE}'
    rf'(?P<psi>{NUMBER}){OPTIONAL_SPACE}(?P<psi_unit>psi){OPTIONAL_SPACE}'
    rf'\({OPTIONAL_SPACE}(?P<mpa>{NUMBER}){OPTIONAL_SPACE}(?P<mpa_unit>MPa)'
    rf'{OPTIONAL_SPACE}\)', re.IGNORECASE)
WAIT = re.compile(
    rf'\bDo{SPACE}not{SPACE}turn{SPACE}off{SPACE}the{SPACE}power{SPACE}'
    rf'immediately{SPACE}after{SPACE}stopping{SPACE}operation\.'
    rf'[\s\S]{{0,240}}?\bAlways{SPACE}wait{SPACE}for{SPACE}at{SPACE}least{SPACE}'
    rf'(?P<wait>{NUMBER}){SPACE}(?P<unit>minutes?){SPACE}before{SPACE}'
    rf'turning{SPACE}off{SPACE}the{SPACE}power\.', re.IGNORECASE)


def _japanese(literal):
    # A native PDF line break may split a Japanese word. Preserve that original
    # spelling in support.quote; normalization is limited to declared unit text.
    return OPTIONAL_SPACE.join(map(re.escape, literal))


MIDRAIL = re.compile(
    _japanese('中さんの高さは') + OPTIONAL_SPACE + rf'(?P<lower>{NUMBER})' +
    OPTIONAL_SPACE + _japanese('センチメートル以上') + OPTIONAL_SPACE +
    rf'(?P<upper>{NUMBER})' + OPTIONAL_SPACE + _japanese('センチメートル以下か'))
EXAMPLE_HEADING = re.compile(_japanese('点検の内容例'))


def _normalized_query(query):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', query).casefold()).strip()


# A small reviewed intent grammar, independent of ranking and evaluation cases.
# It selects existing parsers only; it never extracts values from the question.
QUESTION_SCOPE_BLOCK = re.compile(
    r'\b(?:safe|safety|unsafe|pass|passed|passing|approved|approval|compliant|compliance|'
    r'legal|legally|certified|verified|acceptable|measured|measurement|measurements|'
    r'actual|observed|reading|readings|gauge|sensor|currently|current|now|today|live)\b'
    r'|\b(?:inspection results?|operating pressure|at the site|in the field|okay|ok|can i|may i)\b'
    r'|実測|計測|測定|ゲージ|センサ|現場|現在|今の|今は|実際|安全|合格|適合|適法|確認済|承認|正常|危険|違反|良否|診断|大丈夫|問題ない|してもよい|していい')
PRESSURE_INTENT = re.compile(r'\bpressure\b|圧力')
PRESSURE_LIMIT_INTENT = re.compile(
    r'\b(?:maximum|max|highest|allowable|permissible|permitted|limit|upper bound|rated)\b'
    r'|最大|許容|上限')
WAIT_STOP_INTENT = re.compile(r'\b(?:stop|stops|stopped|stopping|halt|halting|shutdown)\b|停止|止め|止ま|運転終了')
WAIT_POWER_INTENT = re.compile(
    r'\b(?:turn(?:ing)?|switch(?:ing)?|shut(?:ting)?)\s+(?:the\s+)?(?:power\s+)?off\b'
    r'|\boff\s+(?:the\s+)?power\b|\b(?:cut|cutting|disconnect|disconnecting)\s+(?:the\s+)?power\b'
    r'|\bpower(?:ing)?\s+(?:down|off)\b'
    r'|電源.{0,12}(?:切|落|オフ|off)|電源オフ')
WAIT_TIME_INTENT = re.compile(r'\b(?:wait|waiting|long|time|minutes?|delay|duration)\b|待|時間|何分')
MIDRAIL_INTENT = re.compile(r'\b(?:mid[ -]?rails?|middle rails?|intermediate rails?)\b|中さん|中桟')
HEIGHT_INTENT = re.compile(r'\b(?:height|high|range|lower|upper|tall|bounds?)\b|高さ|下限|上限|範囲|寸法')
SCAFFOLD_TYPES = {
    'suspended': re.compile(r'\b(?:suspended|hanging)(?:[ -]+platform)?[ -]+scaffold(?:s|ing)?\b'
                            r'|(?:つり|吊り|吊)(?:\(棚\))?足場|棚足場'),
    'frame': re.compile(r'\b(?:frame|framed)[ -]+scaffold(?:s|ing)?\b|(?:わく組|枠組)(?:み)?足場'),
    'tube': re.compile(r'\b(?:single[ -]+(?:tube|pole)|tube(?:[ -]+and[ -]+coupler)?)[ -]+scaffold(?:s|ing)?\b|単管足場'),
    'wedge': re.compile(r'\bwedge(?:[ -]+lock)?[ -]+scaffold(?:s|ing)?\b|くさび(?:緊結式)?足場'),
}
UNKNOWN_SCAFFOLD_TYPE = re.compile(
    r'\b(?:mobile|rolling|cantilever|trestle|ladder|wooden|bamboo|modular)[ -]+scaffold(?:s|ing)?\b'
    r'|移動式足場|移動足場|張り出し足場|丸太足場|脚立足場')


def _wait_question_relation(text):
    stop, power = WAIT_STOP_INTENT.search(text), WAIT_POWER_INTENT.search(text)
    if not stop or not power:
        return False
    stop_prefix = text[max(0, stop.start() - 50):stop.start()]
    power_prefix = text[max(0, power.start() - 50):power.start()]
    stop_markers = re.findall(r'\b(after|once|following|before|until)\b', stop_prefix)
    power_markers = re.findall(r'\b(after|once|following|before|until|prior to)\b', power_prefix)
    after_stop = bool(stop_markers and stop_markers[-1] in ('after', 'once', 'following'))
    before_power = bool(power_markers and power_markers[-1] in ('before', 'until', 'prior to'))
    jp_after_stop = bool(re.search(r'(?:停止|止め|止ま|運転終了).{0,12}(?:後|から)',
                                  text[stop.start():stop.end() + 16]))
    jp_before_power = bool(re.search(r'電源.{0,16}(?:切|落|オフ|off).{0,8}(?:前|まで)',
                                    text[power.start():power.end() + 20]))
    between_ordered = (stop.start() < power.start() and
                       bool(re.search(r'\bbetween\b', stop_prefix)))
    return (after_stop and before_power) or (jp_after_stop and jp_before_power) or between_ordered


def _question_intent(query, requested_field=None):
    """Return field, explicit source subtype, and an optional hold reason."""
    text = _normalized_query(query)
    if QUESTION_SCOPE_BLOCK.search(text):
        return None, None, 'unsupported_question_scope'
    matches = []
    if PRESSURE_INTENT.search(text) and PRESSURE_LIMIT_INTENT.search(text):
        matches.append('maximum_allowable_pressure')
    if WAIT_TIME_INTENT.search(text) and _wait_question_relation(text):
        matches.append('power_off_wait')
    if MIDRAIL_INTENT.search(text) and HEIGHT_INTENT.search(text):
        matches.append('midrail_height')
    if requested_field is not None:
        field = requested_field
        if (field == 'power_off_wait' and WAIT_STOP_INTENT.search(text) and
                WAIT_POWER_INTENT.search(text) and WAIT_TIME_INTENT.search(text) and
                not _wait_question_relation(text)):
            return field, None, 'unsupported_question_relation'
    elif len(matches) > 1:
        return None, None, 'ambiguous_question_intent'
    elif not matches:
        return None, None, 'unsupported_requested_field'
    else:
        field = matches[0]
    scope = None
    if field == 'midrail_height':
        compact = text.replace(' ', '')
        kinds = [name for name, pattern in SCAFFOLD_TYPES.items()
                 if pattern.search(text) or pattern.search(compact)]
        if len(kinds) > 1:
            return field, None, 'ambiguous_source_context'
        if UNKNOWN_SCAFFOLD_TYPE.search(text) or UNKNOWN_SCAFFOLD_TYPE.search(compact):
            return field, None, 'unsupported_source_context'
        scope = kinds[0] if kinds else None
    return field, scope, None


def _heading_scaffold_type(quote, heading):
    end = quote.find('\n', heading.end())
    if end < 0:
        end = len(quote)
    text = _normalized_query(quote[heading.end():min(end, heading.end() + 160)])
    text = re.sub(r'^[\s\-‐–—ー:]+', '', text)
    compact = text.replace(' ', '')
    kinds = []
    for name, pattern in SCAFFOLD_TYPES.items():
        match = pattern.match(text) or pattern.match(compact)
        if match is None:
            continue
        matched_text = text if pattern.match(text) else compact
        if re.match(r'(?:用|\s*[-‐–—ー]|\s*$)', matched_text[match.end():]):
            kinds.append(name)
    return kinds[0] if len(kinds) == 1 else None


def _negated_prefix(quote, start):
    """Reject simple negated/hypothetical assertion prefixes in this grammar.

    This is intentionally conservative, not arbitrary discourse understanding.
    Native PDF lines contain both columns; only the same physical line before
    the contracted assertion is checked, bounded to 100 characters.
    """
    prefix = quote[max(quote.rfind('\n', 0, start) + 1, start - 100):start]
    return bool(re.search(r'\b(?:not|incorrect|wrong|invalid|false|untrue|hypothetical|if|unless)\b'
                          r'|誤り|誤記|不正確|仮定|もし', prefix, re.IGNORECASE))


def _support(item, start, end):
    return dict(evidence=item['evidence'], source_sha256=item['source_sha256'],
        locator=item['locator'], quote=item['quote'][start:end],
        start_char=start, end_char=end, offset_unit='unicode_codepoints',
        quote_context_sha256=digest(item['quote'].encode('utf-8')))


def _claims(item, field, scaffold_type=None):
    quote = item['quote']
    pattern = dict(maximum_allowable_pressure=PRESSURE,
                   power_off_wait=WAIT, midrail_height=MIDRAIL)[field]
    result = []
    for match in pattern.finditer(quote):
        if _negated_prefix(quote, match.start()):
            continue
        start, end = match.span()
        if field == 'maximum_allowable_pressure':
            values = [dict(value=match['psi'], unit=match['psi_unit'], operator='equals'),
                      dict(value=match['mpa'], unit=match['mpa_unit'], operator='equals')]
            role, relation = 'source_specification', field
        elif field == 'power_off_wait':
            values = [dict(value=match['wait'], unit=match['unit'], operator='at_least')]
            role, relation = 'source_instruction', 'after_stopping_operation_before_power_off'
        else:
            # Without the explicit example heading this could be a reported
            # question or a real observation. Do not invent its source role.
            headings = list(EXAMPLE_HEADING.finditer(quote, 0, start))
            if not headings or Decimal(match['lower']) > Decimal(match['upper']):
                continue
            if scaffold_type is not None and _heading_scaffold_type(quote, headings[-1]) != scaffold_type:
                continue
            start = headings[-1].start()
            values = [dict(value=match['lower'], unit='センチメートル', operator='gte'),
                      dict(value=match['upper'], unit='センチメートル', operator='lte')]
            role, relation = 'inspection_example_criterion', 'midrail_height_inclusive_bounds'
        result.append(dict(field=field, value_role=role, relation=relation,
                           values=values, support_evidence=_support(item, start, end)))
    return result


def _is_ocr(item):
    return ('ocr' in item['evidence']['analysis_version'].lower() or
            item['locator']['kind'] not in ('lines', 'pdf_page', 'json_pointer') or
            item.get('extraction_method') == 'ocr' or
            item.get('ocr_confidence') is not None)


def _ocr_related(item, field, scaffold_type):
    """OCR related to this fact stays unreviewed, including incomplete values."""
    quote = item['quote']
    if field == 'maximum_allowable_pressure':
        return bool(re.search(rf'\bmaximum{SPACE}allowable{SPACE}pressure\b', quote, re.I))
    if field == 'power_off_wait':
        return bool(re.search(r'\bwait\b', quote, re.I) and
                    re.search(r'\bpower\b', quote, re.I) and
                    re.search(r'\bstopping\b', quote, re.I))
    if not re.search(_japanese('中さんの高さ') + '|' + _japanese('中桟の高さ'), quote):
        return False
    if scaffold_type is None:
        return True
    headings = list(EXAMPLE_HEADING.finditer(quote))
    # Unknown OCR context cannot rule out this field; a clearly different
    # inspected subtype can be excluded without trusting OCR as claim support.
    return not headings or any(_heading_scaffold_type(quote, heading) in (None, scaffold_type)
                               for heading in headings)


def _legacy_assessment(excerpts, field):
    if field is None:
        return 'hold', 'unsupported_requested_field', []
    if not excerpts:
        return 'hold', 'no_matching_evidence_in_scope', []
    if any(_is_ocr(item) for item in excerpts):
        return 'hold', 'ocr_claim_support_unreviewed', []
    candidates = [claim for item in excerpts for claim in _claims(item, field)]
    if not candidates:
        return 'hold', 'requested_fact_not_supported', []
    if len(candidates) != 1:
        return 'hold', 'ambiguous_evidence_scope', []
    return 'supported', 'bounded_literal_support_verified', candidates


def _assessment(excerpts, field, scaffold_type=None, intent_hold_reason=None):
    if intent_hold_reason is not None:
        return 'hold', intent_hold_reason, []
    if field is None:
        return 'hold', 'unsupported_requested_field', []
    if not excerpts:
        return 'hold', 'no_matching_evidence_in_scope', []
    ocr = [item for item in excerpts if _is_ocr(item)]
    if any(_ocr_related(item, field, scaffold_type) for item in ocr):
        return 'hold', 'ocr_claim_support_unreviewed', []
    verified = [item for item in excerpts if not _is_ocr(item)]
    candidates = [claim for item in verified for claim in _claims(item, field, scaffold_type)]
    if not candidates:
        if ocr:
            return 'hold', 'ocr_claim_support_unreviewed', []
        if scaffold_type is not None and any(_claims(item, field) for item in verified):
            return 'hold', 'requested_source_context_not_supported', []
        return 'hold', 'requested_fact_not_supported', []
    if len(candidates) != 1:
        return 'hold', 'ambiguous_evidence_scope', []
    return 'supported', 'bounded_literal_support_verified', candidates


class Grounded:
    def __init__(self, store, library):
        self.store, self.library = store, library

    def _verify_excerpt(self, db, tenant, item):
        source = self.library.resolve(db, tenant, item['evidence'])
        for key, value in source.items():
            if key not in item or canonical(item[key]) != canonical(value):
                raise APIError(409, 'evidence_integrity_failed')
        return canonical(source['evidence'])

    def _verify_retrieval_metadata(self, value):
        mode = value.get('retrieval_mode', value.get('mode'))
        version = value.get('retrieval_model_version')
        if mode not in SCORE_MEANINGS or value.get('score_meaning') != SCORE_MEANINGS[mode]:
            raise APIError(409, 'grounded_answer_integrity_failed')
        if mode != 'hybrid':
            if version is not None:
                raise APIError(409, 'grounded_answer_integrity_failed')
            return
        try:
            validate(version, RETRIEVAL_MODEL_VERSION)
        except APIError as error:
            raise APIError(409, 'grounded_answer_integrity_failed') from error
        # Reading a saved answer never loads a retrieval model. If the host has
        # already attached its encoder, compare the descriptor exactly. Without
        # one, this is descriptor validation; it does not certify model execution.
        encoder = getattr(self.library, 'encoder', None)
        metadata = getattr(encoder, 'metadata', None)
        if isinstance(metadata, dict) and canonical(metadata) != canonical(version):
            raise APIError(409, 'grounded_answer_integrity_failed')

    def answer(self, db, ctx, body):
        validate(body, GROUNDED_INPUT)
        search = self.library.search(db, ctx['tenant'], body)
        return self.answer_from_search(db,ctx,body,search)

    def answer_from_search(self,db,ctx,body,search,*,persist=True):
        """Assess already retrieved exact evidence, including a pinned release.

        The caller supplies verified retrieval results, never answer text. Each
        original is resolved again and all support is recomputed below.
        """
        validate(body,GROUNDED_INPUT)
        validate(search,SCHEMAS['Search'])
        self._verify_retrieval_metadata(search)
        excerpts, seen = [], set()
        for item in search['results']:
            reference = self._verify_excerpt(db, ctx['tenant'], item)
            if reference in seen:
                continue
            seen.add(reference)
            if body.get('source_page') is not None and (
                    item['locator']['kind'] != 'pdf_page' or
                    item['locator'].get('page') != body['source_page']):
                continue
            excerpts.append(item)
        field, scaffold_type, intent_hold = _question_intent(body['query'], body.get('requested_field'))
        status, reason, claims = _assessment(excerpts, field, scaffold_type, intent_hold)
        value = dict(id=uid(), mode='bounded_fact_extraction',
            contract_version=CONTRACT_VERSION, status=status, reason=reason,
            query=body['query'], requested_field=body.get('requested_field'),
            resolved_field=field, scaffold_type=scaffold_type,
            intent_policy_version=INTENT_POLICY_VERSION, filters=body.get('filters', {}),
            source_page=body.get('source_page'), retrieval_mode=search['mode'],
            score_meaning=search['score_meaning'], assessment_scope='returned_excerpts',
            claims=claims, excerpts=excerpts, model_used=None, generated_answer=None,
            notice=NOTICE)
        if 'retrieval_model_version' in search:
            value['retrieval_model_version'] = search['retrieval_model_version']
        validate(value, GROUNDED_ANSWER)
        assets = sorted({item['evidence']['asset_id'] for item in excerpts})
        if persist:
            self.store.save_record(db, ctx['tenant'], 'grounded_answers', value, assets)
        return 201, value, assets

    def verify_saved(self, db, tenant, value):
        """Recompute support and every typed value from pinned originals."""
        legacy = (isinstance(value, dict) and not (INTENT_FIELDS & value.keys()) and
                  value.get('notice') == LEGACY_NOTICE)
        try:
            validate(value, LEGACY_GROUNDED_ANSWER if legacy else GROUNDED_ANSWER)
        except APIError as error:
            raise APIError(409, 'grounded_answer_integrity_failed') from error
        self._verify_retrieval_metadata(value)
        seen = set()
        for item in value['excerpts']:
            reference = self._verify_excerpt(db, tenant, item)
            if reference in seen or value['source_page'] is not None and (
                    item['locator']['kind'] != 'pdf_page' or
                    item['locator'].get('page') != value['source_page']):
                raise APIError(409, 'grounded_answer_integrity_failed')
            seen.add(reference)
        if legacy:
            field, scaffold_type = value['requested_field'], None
            status, reason, claims = _legacy_assessment(value['excerpts'], field)
        else:
            field, scaffold_type, intent_hold = _question_intent(value['query'], value['requested_field'])
            status, reason, claims = _assessment(value['excerpts'], field, scaffold_type, intent_hold)
        if (value['status'] != status or value['reason'] != reason or
                not legacy and (value['resolved_field'] != field or value['scaffold_type'] != scaffold_type) or
                canonical(value['claims']) != canonical(claims) or
                value['notice'] != (LEGACY_NOTICE if legacy else NOTICE) or
                value['score_meaning'] != SCORE_MEANINGS[value['retrieval_mode']]):
            raise APIError(409, 'grounded_answer_integrity_failed')
        return value
