"""Human-defined reference conditions; bounded confirmation, no generation.

Ranked similarity is a discovery aid. Claims are independently recomputed
from the immutable authorised source scope, with explicit applicability.
"""
import json
import re
import unicodedata
from .contracts import APIError,ID,EVIDENCE,HASH,SCHEMAS,canonical,digest,array,integer,obj,string,validate
from .store import uid
from .grounded import CLAIM,_question_intent,_claims,_heading_scaffold_type,EXAMPLE_HEADING,QUESTION_SCOPE_BLOCK,_is_ocr,WAIT_TIME_INTENT,WAIT_POWER_INTENT,_wait_question_relation

POLICY='human-reference-conditions-v7'
KEYS=['source_reference','operation_relation','scaffold_type']
STATES=['needs_confirmation','answered_reference','unknown_context','conditions_conflict','evidence_conflict','knowledge_insufficient']
ANSWER=obj(dict(key=dict(type='string',enum=KEYS),status=dict(type='string',enum=['provided','unknown']),value=dict(type=['string','null'],maxLength=200)))
OPTION=obj(dict(id=ID,asset_id=ID,source_version=integer(1,100),source_sha256=HASH,
    source_kind=dict(type='string',enum=['air_conditioner','manual','scaffold','unclassified']),title=string(160),rule_scope_verified=dict(type='boolean'),
    identity_evidence={'anyOf':[array(EVIDENCE,20),dict(type='null')]},models=array(string(80),20)))
REQUIREMENT=obj(dict(key=dict(type='string',enum=KEYS),status=dict(type='string',enum=['satisfied','missing','unknown','conflicting','not_applicable']),
    impact=string(300),basis=array(EVIDENCE,10)))
CONFIRM=obj(dict(key=dict(type='string',enum=KEYS),question=string(500),options=array(obj(dict(value=string(200),label=string(200))),16)))
FLOW=obj(dict(id=ID,revision=integer(1,9),context_generation=integer(1,9),creator=string(80),query=string(1000),
    state=dict(type='string',enum=STATES),reason=string(120),policy_version=dict(type='string',const=POLICY),
    condition_definition_origin=dict(type='string',const='human_authored_rules_with_native_quote_bindings'),
    scope=obj(dict(sources=array(obj(dict(asset_id=ID,source_version=integer(1,100),source_sha256=HASH)),8),
        verified_segments=integer(0,256),retrieval_kind=dict(type='string',const='bm25_discovery_plus_verified_rule_lookup'),
        retrieved_refs=array(EVIDENCE,20),discovery_query=string(1000),knowledge_claim=dict(type='string',const='this_saved_scope_only'))),
    source_options=array(OPTION,8),requirements=array(REQUIREMENT,3),confirmation={'anyOf':[CONFIRM,dict(type='null')]},
    claims=array(CLAIM,1),conflicting_claims=array(CLAIM,10),observations=array(ANSWER,24),
    metrics=obj(dict(prompt_count=integer(0,3),update_count=integer(0,8),repeated_questions=integer(0,0))),
    model_used=dict(type='null'),generated_answer=dict(type='null'),limitations=array(string(500),10)))
CLARIFICATION_SCHEMAS={
    'ConditionalFlowInput':obj(dict(query=string(1000),asset_ids=array(ID,8)),['query']),
    'ConditionalObservation':ANSWER,
    'ConditionalUpdateInput':obj(dict(expected_revision=integer(1,9),answers=array(ANSWER,3),replacement_query=string(1000)),['expected_revision','answers']),
    'ConditionalFlow':FLOW,'ConditionalSourceOption':OPTION,'ConditionalRequirement':REQUIREMENT,'ConditionalPrompt':CONFIRM,
}
CLARIFICATION_OPERATIONS=(('post','/v1/answer-flows','ConditionalFlowInput',201,'read'),
    ('get','/v1/answer-flows/{id}',None,200,'read'),
    ('post','/v1/answer-flows/{id}/observations','ConditionalUpdateInput',201,'read'))
CLARIFICATION_RESPONSE_SCHEMAS={(method,path):'ConditionalFlow' for method,path,*_ in CLARIFICATION_OPERATIONS}
MODEL=re.compile(r'(?<![A-Za-z0-9_-])[A-Z][A-Z0-9_-]{2,}[0-9][A-Z0-9_-]*(?![A-Za-z0-9_-])')


def question_plan(query):
    """Small human-authored reference grammar; values never come from query."""
    q=re.sub(r'\s+',' ',unicodedata.normalize('NFKC',query)).strip()
    field,kind,blocked=_question_intent(query)
    models=list(dict.fromkeys(MODEL.findall(q)))
    base=dict(field=field,kind=kind,reason=None,subject=None,relation=None,models=models,reference=False,identity_terms=[])
    if QUESTION_SCOPE_BLOCK.search(q.casefold()):
        return dict(base,reason='operational_or_measurement_knowledge_unavailable')
    if len(models)>1:return dict(base,reason='multiple_requested_targets_not_supported')
    # A native MODEL is a document subject, never proof of physical unit identity.
    # English source-prefix syntax and Japanese document phrases are bounded.
    prefix=re.fullmatch(r'([A-Za-z][A-Za-z0-9 _-]{0,100} manual):\s*(.+)',q,re.I)
    tail=prefix[2] if prefix else q
    en_pressure=r'(?:What is the )?maximum allowable pressure(?: stated| specified)?(?: in (?:the |this )?(?:(?:(?:system )?air[ -]?conditioner|aircon)(?: installation)? manual|installation manual for model [A-Z][A-Z0-9_-]{2,}[0-9][A-Z0-9_-]*))?(?: in (?:MPa|psi))?\?'
    en_wait=r'(?:How long (?:must I|to)|how long is the) (?:wait after stopping operation before turning off the power|wait before turning off the power|power off wait)\?'
    en_rail=r'In (?:the )?(?:(suspended|hanging|frame|tube|wedge)[ -])?scaffold inspection example, what height range is specified for (?:the )?intermediate rail\?'
    model=r'[A-Z][A-Z0-9_-]{2,}[0-9][A-Z0-9_-]*'
    jp_head=rf'(?:(?:{model})の(?:資料(?:に記載された|では|で)?|)|資料(?:に記載された|では|の)?)'
    jp_pressure=rf'{jp_head}(?:最大許容圧力|許容圧力上限|圧力上限|圧力の上限)は[?？]'
    jp_wait=rf'{jp_head}(?:運転停止後から電源を切る前まで(?:の待ち時間|[、,]?何分待つ)|待ち時間)は?[?？]'
    jp_rail=r'資料の(?:(?:わく組|枠組|つり|吊り|移動式)足場の)?中さんの高さの範囲は[?？]'
    legacy_pressure=r'(?:空調機|エアコン)の(?:据付資料|据付説明書)で[、,]?(?:配管が想定する)?圧力の上限を(?:二つの表示単位で)?教えてください[。.]?'
    legacy_wait=r'運転を止めた直後に主電源を切ってよいですか[。.]切るまでの最短待ち時間を教えてください[。.]?'
    legacy_rail=r'(?:つり(?:\(棚\))?|吊り|棚)足場用の点検内容例で[、,]中さんの高さは何センチメートル以上[、,]何センチメートル以下ですか[。.]?'
    patterns={'maximum_allowable_pressure':[en_pressure,jp_pressure,legacy_pressure],
              'power_off_wait':[en_wait,jp_wait,legacy_wait],
              'midrail_height':[en_rail,jp_rail,legacy_rail,r'足場の点検内容例で[、,]中さんの高さ(?:範囲)?を教えてください[。.]?']}
    matched=next((name for name,forms in patterns.items() if any(re.fullmatch(f,tail,re.I) for f in forms)),None)
    if not matched:return dict(base,reason=blocked or 'question_scope_not_understood')
    if matched=='midrail_height' and blocked:return dict(base,reason=blocked)
    if prefix and not models:return dict(base,reason='named_manual_target_not_bound')
    relation='after_stop_before_power_off' if matched=='power_off_wait' and (_wait_question_relation(q.casefold()) or '止めた直後' in q) else None
    reference=bool(prefix or re.search(r'資料|manual|点検内容例|点検の内容例|inspection example',q,re.I))
    subject='scaffold' if matched=='midrail_height' else 'air_conditioner' if re.search(r'air[ -]?conditioner|aircon|空調機|エアコン',q,re.I) else None
    return dict(base,field=matched,kind=kind,reason=None,subject=subject,relation=relation,reference=reference,identity_terms=[t.casefold() for t in re.findall(r'[A-Za-z][A-Za-z_-]*',MODEL.sub('',prefix[1])) if t.casefold() not in ('manual','service','installation','system','for','model')] if prefix else [])


def profile(segments,pin):
    covers=[s for s in segments if s['locator'].get('kind')=='pdf_page' and s['locator'].get('page')==1 and not _is_ocr(s)]
    if not covers:covers=[s for s in segments if not _is_ocr(s)][:20]
    text='\n'.join(s['quote'] for s in covers)
    title=next((line.strip()[:160] for line in text.splitlines() if line.strip()),'資料識別未確認')
    models=[];kind='unclassified'
    # Explicit MODEL lines in the native document, not filenames or query values.
    for match in re.finditer(r'\bMODEL\b([^\n]*)(?:\n|$)',text):
        models.extend(MODEL.findall(match[1]))
        following=text[match.end():].splitlines()
        for line in following:
            if MODEL.fullmatch(line.strip()):models.append(line.strip())
            else:break
    models=list(dict.fromkeys(models))[:20]
    if re.search(r'INSTALLATION\s+MANUAL',text,re.I) and re.search(r'System\s+Air\s+Conditioner',text,re.I):kind='air_conditioner'
    elif re.search(r'\bmanual\b',title,re.I) and models:kind='manual'
    elif '足場等点検チェックリスト' in text or any(_claims(s,'midrail_height') for s in covers):kind='scaffold'
    scope_text='\n'.join(s['quote'] for s in segments if not _is_ocr(s))
    declarations=0;outside=[];in_declaration=False
    for line in scope_text.splitlines():
        if re.search(r'\bMODEL\b',line) or (re.search(r'\bmodel\b',line,re.I) and MODEL.search(line)):
            declarations+=1;in_declaration=True;continue
        if in_declaration and MODEL.fullmatch(line.strip()):continue
        if not line.strip():continue
        in_declaration=False;outside.append(line)
    # A shared cover MODEL list is supported. Explicit further model sections or
    # model mentions outside that list need a condition parser we do not have.
    model_heading=re.compile(r'^\s*([A-Z][A-Z0-9_-]{2,}[0-9][A-Z0-9_-]*)\s+.*(?:pressure|wait|power|operation|instruction|specification|圧力|待ち|電源)',re.I)
    coded_condition=re.compile(r'(?:\b(?:for|except|applies\s+to|applicable\s+to)\s+(?:model\s+)?[A-Z][A-Z0-9_-]{2,}[0-9][A-Z0-9_-]*\b|\b[A-Z][A-Z0-9_-]{2,}[0-9][A-Z0-9_-]*(?:\s+only\b|\s*(?:専用|のみ|限定)))',re.I)
    verified=declarations<=1 and not any(m in MODEL.findall('\n'.join(outside)) for m in models) and not any(model_heading.search(line) or coded_condition.search(line) for line in outside)
    return dict(id=digest(pin)[:32],**pin,source_kind=kind,title=title,rule_scope_verified=verified,identity_evidence=[s['evidence'] for s in covers] if kind!='unclassified' else None,models=models)


class Clarifications:
    def __init__(self,store,library):self.store,self.library=store,library

    def _basis(self,db,ctx,body):
        tenant=ctx['tenant'];assets=[]
        for row in db.execute('SELECT id FROM assets WHERE tenant=? AND deleted=0 ORDER BY id',(tenant,)):
            if ('asset_ids' not in body or row['id'] in body['asset_ids']) and self.store.asset_readable(db,tenant,row['id']):assets.append(row['id'])
        if len(assets)>8:raise APIError(413,'clarification_scope_limit')
        basis=[];total=0
        for asset in assets:
            version=db.execute('SELECT max(version) FROM sources WHERE tenant=? AND asset=?',(tenant,asset)).fetchone()[0]
            _,sha=self.library.original(db,tenant,asset,version)
            meta=json.loads(db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',(tenant,asset,version)).fetchone()[0])
            refs=[json.loads(r['data'])['evidence'] for r in db.execute('SELECT data FROM segments WHERE tenant=? AND asset=? AND version=? ORDER BY id',(tenant,asset,version))] if meta['media_type'] in ('application/pdf','text/plain','text/markdown') else []
            total+=len(refs)
            if total>256:raise APIError(413,'clarification_segment_limit')
            basis.append(dict(asset_id=asset,source_version=version,source_sha256=sha,refs=refs))
        search=self.library.search(db,tenant,dict(query=body['query'],filters=dict(asset_ids=assets,versions='latest',media_types=['application/pdf','text/plain','text/markdown']),limit=20,ranking_mode='bm25'))
        return basis,[r['evidence'] for r in search['results']]

    def _material(self,db,ctx,internal):
        options=[];segments={}
        for source in internal['basis']:
            _,sha=self.library.original(db,ctx['tenant'],source['asset_id'],source['source_version'])
            latest=db.execute('SELECT max(version) FROM sources WHERE tenant=? AND asset=?',(ctx['tenant'],source['asset_id'])).fetchone()[0]
            if sha!=source['source_sha256'] or latest!=source['source_version']:raise APIError(409,'clarification_basis_changed')
            items=[self.library.resolve(db,ctx['tenant'],ref) for ref in source['refs']]
            items.sort(key=lambda s:(s['locator'].get('page',1),s['locator'].get('line_start',0),s['id']))
            pin={k:source[k] for k in ('asset_id','source_version','source_sha256')}
            option=profile(items,pin);options.append(option);segments[option['id']]=items
        return options,segments

    def _decision(self,db,ctx,internal,*,issue_prompt=False):
        options,segments=self._material(db,ctx,internal);plan=question_plan(internal['query'])
        value=dict(id=internal['id'],revision=internal['revision'],context_generation=internal['generation'],creator=internal['creator'],query=internal['query'],
            state='knowledge_insufficient',reason=plan['reason'] or 'no_supported_rule_in_saved_scope',policy_version=POLICY,
            condition_definition_origin='human_authored_rules_with_native_quote_bindings',
            scope=dict(sources=[{k:s[k] for k in ('asset_id','source_version','source_sha256')} for s in internal['basis']],verified_segments=sum(len(v) for v in segments.values()),
                retrieval_kind='bm25_discovery_plus_verified_rule_lookup',retrieved_refs=internal['retrieved_refs'],discovery_query=internal['query'],knowledge_claim='this_saved_scope_only'),
            source_options=options,requirements=[],confirmation=None,claims=[],conflicting_claims=[],observations=internal['observations'],
            metrics=dict(prompt_count=len(internal['asked']),update_count=internal['revision']-1,repeated_questions=0),model_used=None,generated_answer=None,
            limitations=['保存した認可済み原本版と3種の人手定義parserの範囲だけを確認。世界全体に答えがないとは認定しない。',
                '資料に記された条件付き規則の参照。実機の安全、実測、型番別例外、発行元の現行authorityは未確認。',
                '一般表、図の関係、画像意味、映像・音声認識はこのflowへ未接続。',
                'rule_scope_verifiedは対応済み記法の検査。任意自然文の例外・型番別全分岐・発行元権威は未認定。'])
        if plan['reason']:return value
        field=plan['field'];relevant=(['operation_relation','source_reference'] if field=='power_off_wait' and plan['relation'] is None else ['source_reference','operation_relation'] if field=='power_off_wait' else ['source_reference','scaffold_type'] if field=='midrail_height' else ['source_reference'])
        observations={key:[o for o in internal['observations'] if o['key']==key] for key in relevant}
        for key,items in observations.items():
            if len({o['value'] for o in items if o['status']=='provided'})>1:
                value.update(state='conditions_conflict',reason='contradictory_user_assertions');return value
        def state(key,derived=None):
            items=observations.get(key,[])
            if items:
                if derived is not None and any(o['status']=='provided' and o['value']!=derived for o in items):return ('conflicting',None)
                return ('unknown',None) if items[-1]['status']=='unknown' else ('satisfied',items[-1]['value'])
            return ('satisfied',derived) if derived is not None else ('missing',None)
        source_status,selection=state('source_reference')
        eligible=[o for o in options if o['source_kind']=='scaffold'] if field=='midrail_height' else [o for o in options if o['source_kind'] in ('air_conditioner','manual')]
        if plan['subject']=='air_conditioner':eligible=[o for o in eligible if o['source_kind']=='air_conditioner']
        if plan['models']:
            eligible=[o for o in eligible if all(m in o['models'] for m in plan['models'])]
            if not eligible:value.update(state='conditions_conflict',reason='requested_model_not_in_native_scope');return value
        if plan['identity_terms']:
            named=[o for o in eligible if all(re.search(r'(?<![A-Za-z0-9_-])'+re.escape(t)+r'(?![A-Za-z0-9_-])',o['title'].casefold()) for t in plan['identity_terms'])]
            if not named:value.update(state='conditions_conflict',reason='named_source_not_in_native_identity');return value
            eligible=named
        if not eligible:return value
        if selection is None and source_status=='missing' and (plan['subject'] or plan['models'] or plan['reference']) and len(eligible)==1:
            source_status,selection='satisfied',eligible[0]['id']
        chosen=next((o for o in options if o['id']==selection),None)
        if chosen and chosen not in eligible:
            value.update(state='conditions_conflict' if plan['models'] else 'knowledge_insufficient',reason='selected_source_has_no_applicable_rule');return value
        if source_status=='satisfied' and chosen is None:value.update(state='conditions_conflict',reason='source_reference_not_in_pinned_scope');return value
        def target(option):
            return ('models',tuple(sorted(option['models']))) if option['models'] else (option['source_kind'],option['title'].casefold())
        # Comparison is inside a native target group, never across unrelated models.
        applicable=[o for o in eligible if (bool(set(o['models'])&set(chosen['models'])) if chosen['models'] else target(o)==target(chosen))] if chosen else eligible
        if field!='midrail_height' and any(not o['rule_scope_verified'] for o in applicable):value.update(reason='source_rule_model_scope_unverified');return value
        all_candidates=[]
        for option in applicable:
            for item in segments[option['id']]:
                for claim in _claims(item,field):
                    quote=claim['support_evidence']['quote'];headings=list(EXAMPLE_HEADING.finditer(quote))
                    kind=_heading_scaffold_type(quote,headings[-1]) if headings else None
                    all_candidates.append((option,kind,claim))
        if not all_candidates:return value
        scope_status,scope_value=('not_applicable',None)
        if field=='power_off_wait':scope_status,scope_value=state('operation_relation',plan['relation'])
        if field=='midrail_height':
            kinds={kind for _,kind,_ in all_candidates if kind}
            scope_status,scope_value=state('scaffold_type',plan['kind'] or (next(iter(kinds)) if len(kinds)==1 else None))
        if scope_status=='conflicting':value.update(state='conditions_conflict',reason='provided_condition_contradicts_question');return value
        if field=='power_off_wait' and scope_status=='satisfied' and scope_value!='after_stop_before_power_off':value.update(state='conditions_conflict',reason='operation_relation_not_supported_by_rule');return value
        if field=='midrail_height' and scope_status=='satisfied':all_candidates=[c for c in all_candidates if c[1]==scope_value]
        if not all_candidates:value.update(reason='requested_type_rule_not_found_in_scope');return value
        groups={}
        for option,kind,claim in all_candidates:
            key=(('requested_model',tuple(plan['models'])) if plan['models'] else ('selected_overlapping_targets',chosen['id']) if chosen else target(option),kind if field=='midrail_height' else None)
            groups.setdefault(key,[]).append(claim)
        conflicts=[]
        for candidates in groups.values():
            signatures={canonical(dict(field=c['field'],value_role=c['value_role'],relation=c['relation'],values=c['values'])) for c in candidates}
            if len(signatures)>1:conflicts.extend(candidates)
        if conflicts:
            value.update(state='evidence_conflict',reason='different_rule_values_for_same_native_target',conflicting_claims=conflicts[:10]);return value
        basis_refs=list({canonical(c['support_evidence']['evidence']):c['support_evidence']['evidence'] for _,_,c in all_candidates}.values())[:10]
        statuses={'source_reference':source_status}
        if field=='power_off_wait':statuses['operation_relation']=scope_status
        if field=='midrail_height':statuses['scaffold_type']=scope_status
        impacts={'source_reference':'対象資料を選ぶと、その原本版に限定した参照回答へ進む。個体適用を認定しない。',
                 'operation_relation':'この待ち時間規則は運転停止後かつ電源断前にだけ記されている。',
                 'scaffold_type':'資料の型別例が変わると採用する基準と根拠が変わる。'}
        value['requirements']=[dict(key=key,status=statuses[key],impact=impacts[key],basis=basis_refs) for key in relevant]
        unknown=next((key for key in relevant if statuses[key]=='unknown'),None)
        if unknown:value.update(state='unknown_context',reason='required_condition_unknown');return value
        missing=next((key for key in relevant if statuses[key]=='missing'),None)
        if missing:
            token=str(internal['generation'])+':'+missing
            already_issued=internal.get('prompt_revisions',{}).get(token)==internal['revision']
            if token in internal['asked'] and not already_issued or len(internal['asked'])>=3 and not already_issued:
                value.update(state='unknown_context',reason='repeated_or_excess_confirmation_prevented');return value
            choices=[dict(value=o['id'],label=(o['title']+' / '+','.join(o['models'])+' / 受領版 '+str(o['source_version']))[:200]) for o in eligible] if missing=='source_reference' else [dict(value='after_stop_before_power_off',label='運転停止後・主電源を切る前')] if missing=='operation_relation' else [dict(value=k,label=k) for k in sorted({kind for _,kind,_ in all_candidates if kind})]
            questions={'source_reference':'どの資料の規則を参照する質問ですか。資料の選択は実機の安全認定を意味しません。',
                       'operation_relation':'確認したい操作は「運転停止後、主電源を切る前」ですか。不明なら不明と答えてください。',
                       'scaffold_type':'資料のどの足場型の点検内容例を参照しますか。'}
            value.update(state='needs_confirmation',reason='applicability_condition_missing',confirmation=dict(key=missing,question=questions[missing],options=choices))
            if issue_prompt:
                internal['asked'].append(token);internal['prompt_revisions'][token]=internal['revision'];value['metrics']['prompt_count']=len(internal['asked'])
            return value
        selected=[c for option,kind,c in all_candidates if option['id']==chosen['id']]
        if not selected:value.update(reason='selected_source_has_no_matching_rule');return value
        value.update(state='answered_reference',reason='required_reference_conditions_and_quote_verified',claims=[selected[0]])
        return value

    def _internal(self,db,ctx,id):
        value=self.store.record(db,ctx['tenant'],'clarification_internals',id)
        if value['creator']!=ctx['actor']:raise APIError(404,'not_found')
        self._material(db,ctx,value)
        return value

    def _save(self,db,ctx,internal,value,*,new):
        assets=[s['asset_id'] for s in internal['basis']]
        if new:self.store.save_record(db,ctx['tenant'],'clarification_internals',internal,assets)
        else:db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?',(canonical(internal),ctx['tenant'],'clarification_internals',internal['id']))
        snapshot=dict(id=digest([internal['id'],internal['revision']])[:32],response=value,state_input=internal)
        self.store.save_record(db,ctx['tenant'],'clarification_snapshots',snapshot,assets)
        return 201,value,assets,{}

    def handle(self,db,ctx,route,params,body,if_match=None):
        if route=='/v1/answer-flows':
            basis,refs=self._basis(db,ctx,body)
            internal=dict(id=uid(),revision=1,generation=1,creator=ctx['actor'],query=body['query'],basis=basis,retrieved_refs=refs,observations=[],asked=[],prompt_revisions={},history=[])
            value=self._decision(db,ctx,internal,issue_prompt=True)
            return self._save(db,ctx,internal,value,new=True)
        internal=self._internal(db,ctx,params['id'])
        if route.endswith('/observations'):
            if body['expected_revision']!=internal['revision']:raise APIError(409,'revision_conflict')
            if internal['revision']>=9:raise APIError(409,'clarification_update_limit')
            if 'replacement_query' in body:
                if body['replacement_query']==internal['query']:raise APIError(422,'replacement_query_must_change')
                internal['history'].append(dict(revision=internal['revision'],query=internal['query'],observations=internal['observations']))
                internal.update(query=body['replacement_query'],generation=internal['generation']+1,observations=[])
                search=self.library.search(db,ctx['tenant'],dict(query=internal['query'],filters=dict(asset_ids=[s['asset_id'] for s in internal['basis']],versions='latest',media_types=['application/pdf','text/plain','text/markdown']),limit=20,ranking_mode='bm25'))
                internal['retrieved_refs']=[r['evidence'] for r in search['results']]
            plan=question_plan(internal['query']);allowed={'source_reference'}|({'operation_relation'} if plan['field']=='power_off_wait' else {'scaffold_type'} if plan['field']=='midrail_height' else set())
            if not body['answers'] and 'replacement_query' not in body:raise APIError(422,'condition_observation_required')
            if len({o['key'] for o in body['answers']})!=len(body['answers']):raise APIError(422,'duplicate_condition_key')
            for answer in body['answers']:
                if answer['key'] not in allowed or plan['reason']:raise APIError(422,'condition_not_applicable')
                if (answer['status']=='unknown')!=(answer['value'] is None):raise APIError(422,'condition_observation_status_mismatch')
                internal['observations'].append(answer)
            internal['revision']+=1
            value=self._decision(db,ctx,internal,issue_prompt=True)
            return self._save(db,ctx,internal,value,new=False)
        snapshot=self.store.record(db,ctx['tenant'],'clarification_snapshots',digest([internal['id'],internal['revision']])[:32])
        return 200,snapshot['response'],[],{}

    def verify_response(self,db,ctx,value,route,receipt_replay=False):
        internal=self._internal(db,ctx,value['id'])
        snapshot=self.store.record(db,ctx['tenant'],'clarification_snapshots',digest([value['id'],value['revision']])[:32])
        if canonical(value)!=canonical(snapshot['response']):raise APIError(409,'clarification_response_integrity_failed')
        if value['revision']==internal['revision'] and canonical(snapshot['state_input'])!=canonical(internal):raise APIError(409,'clarification_context_integrity_failed')
        expected=self._decision(db,ctx,snapshot['state_input'])
        if canonical(value)!=canonical(expected):raise APIError(409,'clarification_decision_integrity_failed')
        for claim in value['claims']+value['conflicting_claims']:
            support=claim['support_evidence'];parent=self.library.resolve(db,ctx['tenant'],support['evidence'])
            if support['source_sha256']!=parent['source_sha256'] or support['locator']!=parent['locator'] or support['quote_context_sha256']!=digest(parent['quote'].encode()) or support['quote']!=parent['quote'][support['start_char']:support['end_char']] or not any(canonical(c)==canonical(claim) for c in _claims(parent,claim['field'])):
                raise APIError(409,'clarification_claim_integrity_failed')
        validate(value,FLOW)
