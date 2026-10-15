"""Disabled-by-default, exact-request authorized Gemini reader experiment.

No HTTP-server flag enables this provider. A grant is an administrator's recorded
execution decision, not a claim that API-key presence proves billing or consent.
Only numbered quotes are sent; evidence identities stay in the local parser.
"""
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request

from .contracts import canonical, digest
from .short_answer import prompt_for

PRICES = {'gemini-3.8-flash': (Decimal('.75'), Decimal('3.75')),
          'gemini-4-argon': (Decimal('2'), Decimal('10'))}
PRICE_DEADLINES = {'gemini-3.8-flash':'2027-01-01T00:00:00+00:00',
                   'gemini-4-argon':'2026-12-01T00:00:00+00:00'}
INPUT_LIMIT = OUTPUT_LIMIT = 4000
RESPONSE_LIMIT = 65536
SOURCE_SHA = '119b6a2760323352be8e2929f687f550a371985d0abecbbd2d8458839eac260c'
ERROR_CODES = frozenset('''reader_query_limit reader_span_limit reader_context_limit reader_byte_limit
reader_execution_not_authorized reader_budget_not_authorized reader_request_already_attempted
reader_request_budget_exhausted reader_destination_rejected reader_existing_key_unavailable
reader_response_byte_limit reader_response_shape reader_transport_failed_no_retry reader_response_json
reader_usage_shape reader_response_steps reader_response_parts reader_nontext_response
reader_unexpected_step reader_output_byte_limit reader_usage_unverified reader_usage_exceeds_authorized_budget
reader_reference_only reader_token_count_unavailable reader_input_token_limit reader_provider_halted reader_pricing_expired'''.split())


def request_body(query, spans):
    if not isinstance(query, str) or not 1 <= len(query) <= 1000:
        raise ValueError('reader_query_limit')
    if not isinstance(spans, list) or not 1 <= len(spans) <= 4:
        raise ValueError('reader_span_limit')
    if sum(len(s['quote']) for s in spans) > 2000:
        raise ValueError('reader_context_limit')
    prompt = prompt_for(query, spans)
    if len(prompt.encode()) > 16000:
        raise ValueError('reader_byte_limit')
    return dict(input=prompt, store=False, background=False, stream=False, service_tier='standard',
                generation_config=dict(max_output_tokens=OUTPUT_LIMIT, thinking_level='low',
                                       thinking_summaries='none', tool_choice='none'))


def request_hash(model, body):
    return digest(dict(model=model, **body))


@dataclass(frozen=True)
class ExecutionGrant:
    approval_reference: str
    paid_tier_confirmed: bool
    billing_project: str
    expires_utc: str
    model: str
    source_sha256: str
    request_hashes: tuple
    max_requests: int
    max_usd: str
    ledger_path: str

    def check(self, model, sha):
        expiry = datetime.fromisoformat(self.expires_utc.replace('Z', '+00:00'))
        price_deadline = datetime.fromisoformat(PRICE_DEADLINES[model])
        if expiry.tzinfo is not None and (datetime.now(timezone.utc)>=price_deadline or expiry>price_deadline):
            raise ValueError('reader_pricing_expired')
        if (not self.approval_reference or self.paid_tier_confirmed is not True or
                not self.billing_project or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc) or
                self.model != model or self.source_sha256 != SOURCE_SHA or
                sha not in self.request_hashes or type(self.max_requests) is not int or
                not 1 <= self.max_requests <= 8 or not 1 <= len(self.request_hashes) <= 8):
            raise ValueError('reader_execution_not_authorized')
        prices = PRICES[model]
        reserve = (prices[0] * INPUT_LIMIT + prices[1] * OUTPUT_LIMIT) / Decimal(1000000)
        budget = Decimal(self.max_usd)
        if not budget.is_finite() or budget < reserve or budget > Decimal('.384'):
            raise ValueError('reader_budget_not_authorized')
        return reserve, budget

    def reserve(self, model, sha):
        reserve, budget = self.check(model, sha)
        path = Path(self.ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Separate model grants may share one ledger; request digest includes model.
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            os.chmod(path, 0o600)
            db.execute('CREATE TABLE IF NOT EXISTS attempts (sha TEXT PRIMARY KEY, model TEXT, reserved_usd TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS halts (model TEXT PRIMARY KEY, reason TEXT)')
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM halts WHERE model=?',(model,)).fetchone():raise ValueError('reader_provider_halted')
            rows = db.execute('SELECT sha,reserved_usd FROM attempts WHERE model=?', (model,)).fetchall()
            if any(r[0] == sha for r in rows):
                raise ValueError('reader_request_already_attempted')
            if len(rows) >= self.max_requests or sum((Decimal(r[1]) for r in rows), Decimal(0)) + reserve > budget:
                raise ValueError('reader_request_budget_exhausted')
            db.execute('INSERT INTO attempts VALUES (?,?,?)', (sha, model, str(reserve)))
        # Reservation remains consumed after count/HTTP/timeout failure: no automatic retry.

    def halt(self, model):
        with closing(sqlite3.connect(self.ledger_path, timeout=5)) as db, db:
            db.execute('INSERT OR REPLACE INTO halts VALUES (?,?)',(model,'usage_unverified_or_out_of_budget'))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def rest(model, method, body):
    """Fixed destination. Never stringify exception bodies/headers or API key."""
    if model not in PRICES or method not in ('countTokens', 'interactions'):
        raise ValueError('reader_destination_rejected')
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise ValueError('reader_existing_key_unavailable')
    base = 'https://generativelanguage.googleapis.com/v1beta/'
    url = base + ('interactions' if method=='interactions' else 'models/'+model+':countTokens')
    payload = dict(model=model, **body) if method=='interactions' else body
    req = urllib.request.Request(url, data=canonical(payload).encode(), method='POST',
                                 headers={'x-goog-api-key': key, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=45) as response:
            raw = response.read(RESPONSE_LIMIT + 1)
        if len(raw) > RESPONSE_LIMIT:
            raise ValueError('reader_response_byte_limit')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('reader_response_shape')
        return value
    except urllib.error.HTTPError as error:
        raise ValueError('reader_http_' + str(error.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('reader_transport_failed_no_retry') from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError('reader_response_json') from None


def verified_usage(response):
    if not isinstance(response,dict):raise ValueError('reader_response_shape')
    usage = response.get('usage', {})
    if not isinstance(usage, dict):raise ValueError('reader_usage_shape')
    counts = {k: usage[k] for k in ('total_input_tokens', 'total_output_tokens', 'total_thought_tokens',
                                   'total_tokens', 'total_cached_tokens', 'total_tool_use_tokens')
              if type(usage.get(k)) is int and usage[k] >= 0}
    if not {'total_input_tokens','total_output_tokens','total_thought_tokens'}<=counts.keys():
        raise ValueError('reader_usage_unverified')
    if (counts['total_input_tokens']>INPUT_LIMIT or counts['total_output_tokens']+counts['total_thought_tokens']>OUTPUT_LIMIT or
            counts.get('total_tool_use_tokens',0)):
        raise ValueError('reader_usage_exceeds_authorized_budget')
    return counts


def decode_response(response):
    counts=verified_usage(response)
    steps = response.get('steps', [])
    if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
        raise ValueError('reader_response_steps')
    texts=[]
    for step in steps:
        if step.get('type')=='model_output':
            parts=step.get('content', [])
            if not isinstance(parts, list) or any(not isinstance(p, dict) for p in parts):
                raise ValueError('reader_response_parts')
            for part in parts:
                if part.get('type')=='text' and isinstance(part.get('text'), str):texts.append(part['text'])
                elif part.get('type') not in ('thought', 'thought_summary'):
                    raise ValueError('reader_nontext_response')
        elif step.get('type') not in ('thought', 'thought_summary'):
            raise ValueError('reader_unexpected_step')
    # Thought text/signatures and service IDs never enter the local receipt.
    raw='\n'.join(texts)
    if len(raw.encode()) > 8192:raise ValueError('reader_output_byte_limit')
    finish=response.get('status')
    complete=finish=='completed' and bool(raw)
    if complete:status,reason='generated','generation_completed'
    elif finish=='incomplete':
        status,reason=(('budget_incomplete','reader_output_budget_incomplete')
                       if counts['total_output_tokens']+counts['total_thought_tokens']>=OUTPUT_LIMIT
                       else ('incomplete','reader_incomplete_cause_unverified'))
    else:status,reason='blocked','reader_finish_not_completed'
    return dict(status=status, reason=reason, raw_output=raw or None, complete=complete,
                stop_reason=finish if isinstance(finish,str) and len(finish)<80 else 'unrecognized', usage=counts,
                completion_class='complete' if complete else 'incomplete_or_service_hold_not_capability_failure')


class GeminiReader:
    def __init__(self, model='gemini-3.8-flash', *, grant=None, transport=None):
        if model not in PRICES:
            raise ValueError('reader_model_rejected')
        self.model, self.grant, self.transport = model, grant, transport or rest
        self.request_binding = dict(profile='gemini-api-reader-v1', model_id=model, backend='gemini-developer-api',
                                    max_input_tokens=INPUT_LIMIT, max_output_tokens_including_thinking=OUTPUT_LIMIT,
                                    thinking_level='low', response_count=1, automatic_retries=0,
                                    api='interactions', store=False, background=False, service_tier='standard',
                                    sampling='provider_default_not_deterministic')

    def clear(self):
        pass

    def decide(self, query, task, spans):
        receipt = dict(status='blocked', reason='reader_execution_not_authorized', raw_output=None, complete=False,
                       model=dict(self.request_binding))
        if self.grant is None:
            return receipt
        began = time.monotonic()
        generation_attempted=usage_verified=False
        try:
            if task != {'kind': 'reference'}:
                raise ValueError('reader_reference_only')
            body = request_body(query, spans)
            sha = request_hash(self.model, body)
            receipt.update(request_sha256=sha, prompt_sha256=digest(prompt_for(query, spans).encode()))
            self.grant.reserve(self.model, sha)
            count = self.transport(self.model, 'countTokens', dict(contents=[dict(role='user',parts=[dict(text=body['input'])])])).get('totalTokens')
            if type(count) is not int or count < 1:
                raise ValueError('reader_token_count_unavailable')
            receipt['counted_input_tokens'] = count
            if count > INPUT_LIMIT:
                raise ValueError('reader_input_token_limit')
            generation_attempted=True
            response=self.transport(self.model, 'interactions', body)
            verified_usage(response)
            usage_verified=True
            receipt.update(decode_response(response))
        except (ValueError, TypeError, KeyError, AttributeError, DecimalException, sqlite3.Error) as error:
            # Only our fixed error codes are exported; arbitrary service/mock text is discarded.
            code = str(error)
            receipt.update(status='blocked', reason=code if code in ERROR_CODES or re.fullmatch(r'reader_http_[1-5][0-9]{2}',code)
                           else 'reader_response_or_policy_failed', raw_output=None, complete=False)
            if generation_attempted and not usage_verified:
                self.grant.halt(self.model)
        receipt['wall_seconds'] = time.monotonic() - began
        return receipt
