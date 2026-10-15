"""Bounded source-reading and table-calculation component, not a general LLM.

The same kernel runs flat/typed and static/active retrieval. Explicit task inputs
are user instructions, not oracle values. No evaluation labels are read here.
"""
import json,re,unicodedata
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP,localcontext,DecimalException,Inexact
from .contracts import APIError,ID,EVIDENCE,HASH,SCHEMAS,obj,string,array,integer,canonical,digest,validate
from .store import uid

VERSION='typed-evidence-assist-v4'
SELECTOR=obj(dict(row=string(300),column=string(200),sheet=string(31),source_title=string(160),cell=string(20,pattern=r'^[A-Z]{1,3}[1-9][0-9]{0,4}$')),[])
SELECTOR['oneOf']=[dict(required=['row','column'],**{'not':dict(required=['cell'])}),dict(required=['cell','sheet'],**{'not':dict(anyOf=[dict(required=['row']),dict(required=['column'])])})]
TASK=obj(dict(kind=dict(type='string',enum=['reference','table_lookup','table_calculation']),operation=dict(type='string',enum=['identity','sum','difference','ratio','percentage','percentage_point']),selectors=array(SELECTOR,20),precision=integer(0,8),as_of=string(40),unit=string(80,0),scale=string(128,pattern=r'^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$'),compare_to=SELECTOR,comparison_precision=integer(0,8)),['kind'])
INPUT=obj(dict(query=string(1000),task=TASK,asset_ids=array(ID,8),memory_mode=dict(type='string',enum=['flat','typed']),retrieval_policy=dict(type='string',enum=['static','active','conditional']),ranking_mode=dict(type='string',enum=['bm25','hybrid']),answer_mode=dict(type='string',const='local_short_answer')),['query','task'])
OBS=obj(dict(key=string(100),status=dict(type='string',enum=['provided','unknown']),value=dict(type=['string','null'],maxLength=200)))
UPDATE=obj(dict(expected_revision=integer(1,9),observations=array(OBS,2)),['expected_revision','observations'])
# Detailed traces are source-derived and bounded inside the component.
RESPONSE=obj(dict(id=ID,creator=string(80),revision=integer(1,9),query=string(1000),task=TASK,policy_version=dict(type='string',const=VERSION),state=dict(type='string',enum=['needs_confirmation','reference_candidates','answered_calculation','context_unknown','conditions_conflict','retrieval_incomplete','extraction_incomplete','semantic_unavailable','answer_candidate','model_hold']),reason=string(200),scope=array(obj(dict(asset_id=ID,source_version=integer(1,100),source_sha256=HASH)),8),excerpts=array({'$ref':'#/components/schemas/Match'},20),memory=array(dict(type='object'),256),claims=array(dict(type='object'),20),confirmation=dict(type=['object','null']),observations=array(OBS,16),stages=dict(type='object'),budgets=obj(dict(searches=integer(0,3),context_characters=integer(0,8000),prompts=integer(0,2),updates=integer(0,8))),model_used=dict(type=['string','null']),generated_answer={'anyOf':[dict(type='null'),obj(dict(answer=string(600),supports=array(dict(type='object'),20,1),semantic_support_verified=dict(type='boolean',const=False),review_required=dict(type='boolean',const=True),answer_is_source_substring=dict(type='boolean')))]},limitations=array(string(500),10)))
ASSIST_SCHEMAS={'EvidenceTask':TASK,'EvidenceTaskInput':INPUT,'EvidenceTaskUpdateInput':UPDATE,'EvidenceTaskResult':RESPONSE}
ASSIST_OPERATIONS=(('post','/v1/evidence-tasks','EvidenceTaskInput',201,'read'),('get','/v1/evidence-tasks/{id}',None,200,'read'),('post','/v1/evidence-tasks/{id}/observations','EvidenceTaskUpdateInput',201,'read'))
ASSIST_RESPONSES={(v,p):'EvidenceTaskResult' for v,p,*_ in ASSIST_OPERATIONS}


def norm(v):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(v))).casefold()

def support(item):
    return dict(evidence=item['evidence'],source_sha256=item['source_sha256'],locator=item['locator'],quote=item['quote'],start_char=0,end_char=len(item['quote']),offset_unit='unicode_codepoints',quote_context_sha256=digest(item['quote'].encode()))


def atomize(items,mode):
    atoms=[]
    for item in items:
        fields=item['fields']
        if mode=='typed' and item['locator']['kind']=='xlsx_row':
            for cell in fields['cells']:
                atoms.append(dict(kind='cell',evidence=item['evidence'],support_evidence=support(item),address=cell['address'],value=cell['value'],cache_state=cell['cache_state'],formula=cell['formula'],row_labels=fields['labels'],header_candidates=fields['headers'],sheet=fields['sheet'],row=fields['row'],column=cell['column'],row_hidden=fields['row_hidden'],column_hidden=cell['column_hidden'],cell_kind=cell['kind']))
        else:
            atoms.append(dict(kind='quote',support_evidence=support(item),text=item['quote'],date_mentions=re.findall(r'(?:\d{4}年\s*\d{1,2}月(?:\s*\d{1,2}日)?|令和\s*\d+年|平成\s*\d+年)',item['quote']) if mode=='typed' else [],scope_markers=[line.strip()[:300] for line in item['quote'].splitlines() if re.search(r'場合|ただし|限り|施行|適用|経過措置|except|only|effective',line,re.I)][:10] if mode=='typed' else []))
    if len(atoms)>256:atoms=atoms[:256]
    return atoms


def flat_cells(memory):
    """Decode only returned row quotes. No attached global header metadata.

    This lets the flat control do the same arithmetic when its returned context
    contains the headers, instead of disabling the control's table capability.
    """
    rows=[]
    for atom in memory:
        loc=atom['support_evidence']['locator']
        if loc['kind']!='xlsx_row':continue
        text=atom['text'].split(': ',1)[-1];cells=[]
        for field in text.split('\t'):
            match=re.fullmatch(r'([A-Z]+)([0-9]+)=(.*)',field,re.S)
            if not match:continue
            col,row,raw=match.groups()
            try:value=json.loads(raw,parse_float=Decimal,parse_int=Decimal)
            except (ValueError,TypeError):continue
            if value is None or isinstance(value,(list,dict,bool)):continue
            cells.append((col+row,col,value))
        rows.append((atom,loc,cells))
    result=[]
    for atom,loc,cells in rows:
        headers=[dict(address=addr,column=col,text=v,role='candidate')
                 for a,l,cs in rows if l['sheet']==loc['sheet'] and l['row']<loc['row']
                 and a['support_evidence']['evidence']['asset_id']==atom['support_evidence']['evidence']['asset_id']
                 for addr,col,v in cs if isinstance(v,str)]
        for addr,col,v in cells:
            if not isinstance(v,(float,int,Decimal)):continue
            result.append(dict(kind='cell',evidence=atom['support_evidence']['evidence'],support_evidence=atom['support_evidence'],address=addr,value=str(v),cache_state='not_formula',formula=None,row_labels=[v for _,_,v in cells if isinstance(v,str)],header_candidates=headers,sheet=loc['sheet'],row=loc['row'],column=col,row_hidden=False,column_hidden=False,cell_kind='number'))
    return result


class EvidenceAssist:
    def __init__(self,store,library,decision_model=None,short_answer_model=None):
        self.store,self.library,self.decision_model=store,library,decision_model
        self.short_answer_model=short_answer_model

    def _basis(self,db,ctx,body):
        ids=[r['id'] for r in db.execute('SELECT id FROM assets WHERE tenant=? AND deleted=0 ORDER BY id',(ctx['tenant'],)) if self.store.asset_readable(db,ctx['tenant'],r['id']) and ('asset_ids' not in body or r['id'] in body['asset_ids'])]
        if len(ids)>8:raise APIError(413,'evidence_task_source_limit')
        pins=[]
        for asset in ids:
            row=db.execute('SELECT version,sha FROM sources WHERE tenant=? AND asset=? ORDER BY version DESC LIMIT 1',(ctx['tenant'],asset)).fetchone()
            self.library.original(db,ctx['tenant'],asset,row['version'])
            pins.append(dict(asset_id=asset,source_version=row['version'],source_sha256=row['sha']))
        return pins

    def _material(self,db,ctx,internal):
        items=[];extraction=[]
        for pin in internal['basis']:
            _,sha=self.library.original(db,ctx['tenant'],pin['asset_id'],pin['source_version'])
            latest=db.execute('SELECT max(version) FROM sources WHERE tenant=? AND asset=?',(ctx['tenant'],pin['asset_id'])).fetchone()[0]
            if sha!=pin['source_sha256'] or latest!=pin['source_version']:raise APIError(409,'evidence_task_basis_changed')
            meta=json.loads(db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',(ctx['tenant'],pin['asset_id'],pin['source_version'])).fetchone()[0])
            job=self.store.job(db,ctx['tenant'],meta['job_id'])
            refs=[json.loads(r['data'])['evidence'] for r in db.execute('SELECT data FROM segments WHERE tenant=? AND asset=? AND version=? ORDER BY id',(ctx['tenant'],pin['asset_id'],pin['source_version']))]
            entry=dict(**pin,job_state=job['state'],error=job['error'],segment_count=len(refs))
            if 'xlsx_selection' in meta:entry['xlsx_selection']=meta['xlsx_selection']
            extraction.append(entry)
            items.extend(self.library.resolve(db,ctx['tenant'],ref) for ref in refs)
        return items,extraction

    def _retrieve(self,db,ctx,internal,items_override=None):
        request=internal['input'];queries=[request['query']]
        if request.get('retrieval_policy')=='conditional':
            return self._conditional(db,ctx,internal,items_override)
        if request.get('retrieval_policy','active')=='active':
            for s in request['task'].get('selectors',[]):
                q=' '.join(str(s.get(k,'')) for k in ('row','column','sheet')).strip()
                if q and q not in queries:queries.append(q)
                if len(queries)==3:break
        seen={};runs=[]
        for q in queries:
            if items_override is None:
                r=self.library.search(db,ctx['tenant'],dict(query=q,ranking_mode=request.get('ranking_mode','bm25'),filters=dict(asset_ids=[p['asset_id'] for p in internal['basis']],versions='latest'),limit=20))
            else:
                # Extraction intervention retains the exact ranking/chunk/fusion
                # component and the same query/context budgets as the live arm.
                candidates=[]
                for item in items_override:
                    ref=item['evidence'];meta=json.loads(db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',(ctx['tenant'],ref['asset_id'],ref['source_version'])).fetchone()[0])
                    parsed=self.store.parse_cache[(ctx['tenant'],ref['asset_id'],ref['source_version'])]
                    first=next(iter(parsed.values()))['quote'] if parsed else ''
                    title=next((line.strip()[:160] for line in first.splitlines() if line.strip()),'')
                    candidates.append(dict(ref=ref,meta=meta,segment=item,title=title))
                ranking=self.library.ranking.rank(ctx['tenant'],q,candidates,request.get('ranking_mode','bm25')) if candidates else []
                ranking=sorted(ranking,key=lambda x:(-x[1],candidates[x[0]]['ref']['asset_id'],candidates[x[0]]['ref']['segment_id']))[:20]
                r=dict(results=[dict(**candidates[i]['segment'],retrieval_score=score,media_type=candidates[i]['meta']['media_type'],labels=candidates[i]['meta']['labels']) for i,score in ranking],model_used=self.library.encoder.metadata['model_id'] if request.get('ranking_mode','bm25')=='hybrid' and candidates else None)
            # Equal final budget; active uses round-robin candidate selection.
            runs.append(r)
        for rank in range(20):
            for r in runs:
                if rank<len(r['results']):
                    item=r['results'][rank];seen.setdefault(canonical(item['evidence']),item)
        selected=[];chars=0
        for item in seen.values():
            if len(selected)==20:break
            if chars+len(item['quote'])>8000:continue
            selected.append(item);chars+=len(item['quote'])
        return selected,queries,chars,runs

    def _conditional(self,db,ctx,internal,items):
        """A bounded exact row service over the already verified source scope."""
        if items is None:items,_=self._material(db,ctx,internal)
        task=internal['input']['task'];selectors=list(task.get('selectors',[]))
        if task.get('compare_to'):selectors.append(task['compare_to'])
        observations={o['key']:o for o in internal['observations']}
        rows=[s for s in items if s['locator']['kind']=='xlsx_row'];chosen={};missing=[]
        # Source aliases cannot select a workbook. Ask before enumerating a
        # potentially large set of identically labelled candidate rows.
        source_observation=observations.get('source_reference')
        if any(s.get('source_title') for s in selectors) and (source_observation is None or source_observation['status']=='unknown'):
            stats=dict(policy='conditional',ranking='not_used_exact_row_sheet_conditions',verified_rows_scanned=len(rows),
                requested_selectors=len(selectors),matching_rows=0,candidate_quote_characters=0,missing_selector_rows=[],
                selection_complete=False,cap_reason=None,selected_rows=0,awaiting_source_reference=True)
            return [],[internal['input']['query']],0,[dict(results=[],model_used=None,stats=stats)]
        for index,selector in enumerate(selectors):
            sheet=selector.get('sheet');key='sheet:compare_to' if index==len(task.get('selectors',[])) else 'sheet:'+str(index)
            observed=observations.get(key)
            if observed and observed['status']=='provided':sheet=observed['value']
            source=observations.get('source_reference') if any(s.get('source_title') for s in selectors) else None
            matches=[s for s in rows if (s['locator']['row']==int(re.search(r'[0-9]+',selector['cell'])[0]) if 'cell' in selector else norm(selector['row']) in [norm(x) for x in s['fields']['labels']])
                and (not sheet or norm(s['locator']['sheet'])==norm(sheet))
                and (not source or source['status']!='provided' or s['evidence']['asset_id']==source['value'])]
            if not matches:missing.append(index)
            for item in matches:chosen[canonical(item['evidence'])]=item
        ordered=sorted(chosen.values(),key=lambda s:(s['evidence']['asset_id'],s['locator']['sheet'],s['locator']['row'],s['id']))
        chars=sum(len(s['quote']) for s in ordered)
        reason=('conditional_table_selectors_required' if task['kind']=='reference' or not task.get('selectors') else
                'conditional_row_cap_exceeded' if len(ordered)>20 else
                'conditional_context_cap_exceeded' if chars>8000 else None)
        stats=dict(policy='conditional',ranking='not_used_exact_row_sheet_conditions',verified_rows_scanned=len(rows),
            requested_selectors=len(selectors),matching_rows=len(ordered),candidate_quote_characters=chars,
            missing_selector_rows=missing,selection_complete=reason is None,cap_reason=reason,
            selected_rows=0 if reason else len(ordered))
        selected=[]
        if reason is None:
            for item in ordered:
                ref=item['evidence'];meta=json.loads(db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',
                    (ctx['tenant'],ref['asset_id'],ref['source_version'])).fetchone()[0])
                selected.append(dict(**item,retrieval_score=1.0,media_type=meta['media_type'],labels=meta['labels']))
        run=dict(results=selected,model_used=None,stats=stats)
        return selected,[internal['input']['query']],0 if reason else chars,[run]

    def compute(self,db,ctx,internal,overrides=None,issue_prompt=False):
        # Diagnostic overrides are an in-process evaluator interface, never HTTP.
        overrides=overrides or {};items,extraction=self._material(db,ctx,internal)
        if 'extraction' in overrides:items=overrides['extraction']
        request=internal['input'];task=request['task'];conditional=request.get('retrieval_policy')=='conditional'
        extraction_failed=any(x['job_state']!='succeeded' for x in extraction)
        if extraction_failed:selected,queries,chars,runs=[],[],0,[]
        else:selected,queries,chars,runs=self._retrieve(db,ctx,internal,items if 'extraction' in overrides or conditional else None)
        if 'retrieved_refs' in overrides:
            keys={canonical(ref) for ref in overrides['retrieved_refs']};selected=[s for s in items if canonical(s['evidence']) in keys];chars=sum(len(s['quote']) for s in selected)
        memory_mode='typed' if conditional else request.get('memory_mode','typed')
        memory=atomize(selected,memory_mode)
        if 'typed_memory' in overrides:memory=overrides['typed_memory']
        value=dict(id=internal['id'],creator=internal['creator'],revision=internal['revision'],query=request['query'],task=task,policy_version=VERSION,state='retrieval_incomplete',reason='required_evidence_coverage_unverified',scope=internal['basis'],excerpts=selected,memory=memory,claims=[],confirmation=None,observations=internal['observations'],stages=dict(extraction=extraction,retrieval_queries=queries,retrieved_refs=[s['evidence'] for s in selected],memory_kind=request.get('memory_mode','typed'),applicability=dict(as_of=task.get('as_of'),source_validity='unassessed'),decision='not_established'),budgets=dict(searches=len(queries),context_characters=min(chars,8000),prompts=len(internal['asked']),updates=internal['revision']-1),model_used=next((r['model_used'] for r in runs if r['model_used']),None),generated_answer=None,limitations=['Task operations/selectors are explicit user inputs; general autonomous natural-language interpretation is not implemented.','Typed date mentions are observations, not verified legal valid-time or authority.','No LLM/VLM/ASR decision model; quotes are candidates until their final claim is independently checked.'])
        # A dataset's explicitly stated base-period label is narrower than
        # general valid-time reasoning; arbitrary dates still remain unverified.
        declared=[x for x in items if x['locator']['kind']=='xlsx_row' and x['locator']['row']==1]
        period=task.get('as_of')
        all_sheets={(x['evidence']['asset_id'],x['locator']['sheet']) for x in items if x['locator']['kind']=='xlsx_row'}
        declared_sheets={(x['evidence']['asset_id'],x['locator']['sheet']) for x in declared}
        if (period and re.fullmatch(r'[0-9]{4}年基準',period) and declared and declared_sheets==all_sheets
            and all(any(re.search(r'(?<![0-9])'+re.escape(period),label) for label in x['fields'].get('labels',[])) for x in declared)):
            value['stages']['applicability'].update(valid_time_verified=True,verification_kind='literal_dataset_base_period_only',supports=[support(x) for x in declared])
        value['stages']['source_options']=[dict(asset_id=x['evidence']['asset_id'],heading=x['quote'][:500],support_evidence=support(x)) for x in declared]
        if 'applicability' in overrides:value['stages']['applicability']=overrides['applicability']
        value['stages']['oracle_interventions']=sorted(overrides)
        value['stages']['oracle_context_budget_exceeded']=chars>8000 or len(selected)>20
        value['stages']['memory_truncated']=sum(len(x['fields'].get('cells',[])) if memory_mode=='typed' and x['locator']['kind']=='xlsx_row' else 1 for x in selected)>256
        value['stages']['request_interpretation']='explicit task only; additional natural-language requirements are not automatically validated'
        value['stages'].update(memory_kind=memory_mode,requested_memory_mode=request.get('memory_mode','typed'),
            retrieval_policy=request.get('retrieval_policy','active'),retrieval_stats=[r['stats'] for r in runs if 'stats' in r],
            completion_scope='explicit_task_only',explicit_task_complete=False)
        value['stages']['context_budget_definition']='returned quote characters; structured metadata counted separately'
        value['stages']['serialized_memory_bytes']=len(canonical(memory).encode())
        if any('xlsx_selection' in x for x in extraction):
            value['stages']['source_coverage']='explicit_selected_ranges_only'
            value['limitations'].append('Only explicitly selected XLSX ranges are available; whole-workbook completeness and unselected content are not established.')
        cap=next((r['stats']['cap_reason'] for r in runs if r.get('stats',{}).get('cap_reason')),None)
        if conditional and not cap and sum(len(x['fields'].get('cells',[])) for x in selected)>256:cap='conditional_memory_cap_exceeded'
        if extraction_failed:
            value.update(state='extraction_incomplete',reason='source_processing_not_complete_or_supported')
        elif cap:
            value.update(state='retrieval_incomplete',reason=cap)
        elif task['kind']=='reference':
            value.update(state='reference_candidates' if selected else value['state'],reason='semantic_decision_model_unavailable' if selected else value['reason']);value['stages']['decision']='reference_only_semantic_support_unassessed'
            if request.get('answer_mode')=='local_short_answer':
                self._short_answer(request,selected,items,internal,value,issue_prompt)
            elif selected and (self.decision_model is not None or 'generation_receipt' in internal):
                self._model_diagnostic(request,selected,internal,value,issue_prompt)
        else:
            self._table(task,memory,selected,internal,value,issue_prompt)
        if 'decision' in overrides:
            value.update(overrides['decision']);value['stages']['decision']='oracle_diagnostic_only'
        return value

    def _short_answer(self,request,selected,material,internal,value,execute):
        from .short_answer import PROFILE,WEIGHT_SHA,LIMITS,windows,prompt_for,parse
        from .table_context import POLICY,build
        # Old persisted receipts use their exact legacy selection/binding.
        if 'short_answer_receipt' not in internal:internal.setdefault('short_answer_context_policy',POLICY)
        context=build(request['query'],selected,material) if internal.get('short_answer_context_policy')==POLICY else None
        spans=context['spans'] if context is not None else windows(request['query'],selected)
        bound=dict(profile=PROFILE,weights=WEIGHT_SHA,limits=LIMITS,prompt=prompt_for(request['query'],spans),spans=spans)
        if internal.get('short_answer_context_policy'):bound['context_policy']=internal['short_answer_context_policy']
        # Optional admin-injected provider identity is pinned with the receipt.
        # Legacy local bindings remain byte-identical; GET never reads live config.
        if 'short_answer_receipt' not in internal and self.short_answer_model is not None:
            runtime=getattr(self.short_answer_model,'request_binding',None)
            if runtime is not None:internal['short_answer_runtime_binding']=json.loads(canonical(runtime))
        runtime=internal.get('short_answer_runtime_binding')
        if runtime is not None:
            bound=dict(profile=runtime['profile'],runtime_binding=runtime,prompt=prompt_for(request['query'],spans),spans=spans)
            if internal.get('short_answer_context_policy'):bound['context_policy']=internal['short_answer_context_policy']
        binding=digest(bound)
        if 'short_answer_receipt' not in internal:
            if not execute:raise APIError(409,'short_answer_receipt_missing')
            if not spans:receipt=dict(status='blocked',reason=context['blocked_reason'] if context and context['blocked_reason'] else 'no_retrieved_context',raw_output=None,complete=False)
            elif self.short_answer_model is None:receipt=dict(status='blocked',reason='short_answer_model_unavailable',raw_output=None,complete=False)
            else:receipt=self.short_answer_model.decide(request['query'],request['task'],spans)
            internal['short_answer_receipt']=dict(request_sha256=binding,receipt=receipt,receipt_sha256=digest(receipt))
        saved=internal['short_answer_receipt']
        if saved.get('request_sha256')!=binding or saved.get('receipt_sha256')!=digest(saved.get('receipt')):raise APIError(409,'short_answer_receipt_integrity')
        receipt=saved['receipt'];raw=receipt.get('raw_output')
        parsed=parse(raw,spans,receipt.get('complete') is True)
        value.update(state='model_hold',reason=parsed['reason'] if raw is not None else receipt.get('reason','short_answer_failed'),model_used=PROFILE if raw else None)
        if runtime is not None:
            value['model_used']=runtime['model_id'] if raw else None
            if receipt.get('status') in ('budget_incomplete','incomplete'):value['reason']=receipt['reason']
        if parsed['status']=='candidate':
            value.update(state='answer_candidate',generated_answer={k:parsed[k] for k in ('answer','supports','semantic_support_verified','review_required','answer_is_source_substring')})
        value['stages'].update(decision='experimental_short_answer',model_receipt=receipt,model_candidate=parsed,model_request_sha256=binding,
            answer_context=spans,answer_context_characters=sum(len(x['quote']) for x in spans),answer_context_scope='original_table_units_query_coverage_unverified' if context else 'query_ranked_original_windows_incomplete_coverage',
            actual_generation_returned=bool(raw),completion_scope='candidate_only_semantics_unverified',request_interpretation='natural_language_query_no_explicit_answer_selectors')
        if internal.get('short_answer_context_policy'):value['stages']['context_selection']=context or dict(policy='legacy-short-windows-v1')
        value['limitations']=['Experimental local model answer; original citation identity is checked but relevance and entailment require independent review.',
            ('Context selection and structural header/footnote candidates do not establish query completeness or semantic applicability; UNKNOWN is not proof of source absence.' if internal.get('short_answer_context_policy') else 'Query-selected short windows can omit exceptions, headers and other required facts; UNKNOWN is not proof of source absence.'),
            'No automatic approval, operational action, calculation certification or report export from these candidates.']
        if runtime is not None:value['limitations'][0]='Experimental configured model answer; original citation identity is checked but relevance and entailment require independent review.'

    def _model_diagnostic(self,request,selected,internal,value,execute):
        from .local_decision import PROFILE,WEIGHT_SHA,parse_candidate
        binding=digest(dict(profile=PROFILE,weights_sha256=WEIGHT_SHA,query=request['query'],task=request['task'],excerpts=selected))
        if 'generation_receipt' not in internal:
            if not execute or self.decision_model is None:raise APIError(409,'decision_receipt_missing')
            receipt=self.decision_model.decide(request['query'],request['task'],selected)
            internal['generation_receipt']=dict(request_sha256=binding,receipt=receipt,receipt_sha256=digest(receipt))
        saved=internal['generation_receipt']
        if saved.get('request_sha256')!=binding or saved.get('receipt_sha256')!=digest(saved.get('receipt')):
            raise APIError(409,'decision_receipt_integrity')
        receipt=json.loads(canonical(saved['receipt']))
        raw=receipt.get('raw_output')
        # Re-parse saved raw text against current verified excerpts, without
        # regenerating on GET/replay or treating the model as an authority.
        parsed=parse_candidate(raw,selected) if isinstance(raw,str) else None
        value.update(state='semantic_unavailable',reason='local_decision_quality_gate_not_passed',model_used=PROFILE if isinstance(raw,str) else None)
        value['stages'].update(decision='actual_local_generation_diagnostic_only',model_receipt=receipt,model_candidate=parsed,model_request_sha256=binding,configured_model_profile=PROFILE,actual_generation_returned=isinstance(raw,str))
        value['limitations'][-1]='An actual local 0.6B generator is connected for diagnostics only; initial QA failed. No model answer is accepted. VLM/ASR remain unconnected.'

    def _table(self,task,memory,selected,internal,value,issue_prompt):
        applicability=value['stages']['applicability']
        if applicability.get('status') in ('unknown','conflict'):
            value.update(state='context_unknown' if applicability['status']=='unknown' else 'conditions_conflict',reason='applicability_not_established');return
        if task.get('as_of') and not applicability.get('valid_time_verified',False):
            value.update(state='context_unknown',reason='requested_valid_time_unverified');return
        selectors=list(applicability.get('selectors',task.get('selectors',[])))
        operand_count=len(selectors)
        if not operand_count:value.update(state='semantic_unavailable',reason='explicit_table_selectors_required');return
        if task.get('compare_to'):selectors.append(task['compare_to'])
        if value['stages']['memory_kind']=='flat' and not any(a.get('kind')=='cell' for a in memory):memory=flat_cells(memory)
        if not selectors:value.update(state='semantic_unavailable',reason='explicit_table_selectors_required');return
        observations={}
        for o in internal['observations']:
            if o['key'] in observations and observations[o['key']]['status']=='provided' and observations[o['key']]!=o:value.update(state='conditions_conflict',reason='contradictory_context_observations');return
            observations[o['key']]=o
        confirmed_asset=None
        if any(selector.get('source_title') for selector in selectors):
            source_key='source_reference';o=observations.get(source_key)
            options=sorted({x['asset_id'] for x in value['stages'].get('source_options',[])})
            if o is None:
                if source_key in internal['asked'] or len(internal['asked'])>=2:
                    value.update(state='context_unknown',reason='confirmation_bound');return
                value.update(state='needs_confirmation',reason='source_title_alias_needs_original_selection',confirmation=dict(key=source_key,question='原本見出しを確認し、この問いに使用する資料を選んでください。',options=options))
                if issue_prompt:internal['asked'].append(source_key);value['budgets']['prompts']=len(internal['asked'])
                return
            if o['status']=='unknown':value.update(state='context_unknown',reason='source_reference_unknown');return
            if o['value'] not in options:value.update(state='conditions_conflict',reason='source_reference_outside_scope');return
            confirmed_asset=o['value']
        candidates=[]
        for index,selector in enumerate(selectors):
            sheet=selector.get('sheet');key='sheet:compare_to' if index==operand_count else 'sheet:'+str(index)
            if key in observations:
                o=observations[key]
                if o['status']=='unknown':value.update(state='context_unknown',reason='required_sheet_unknown');return
                if sheet and sheet!=o['value']:value.update(state='conditions_conflict',reason='provided_sheet_contradicts_task');return
                sheet=o['value']
            matches=[]
            for atom in memory:
                if confirmed_asset and atom.get('evidence',{}).get('asset_id')!=confirmed_asset:continue
                if atom.get('kind')!='cell' or atom['cell_kind'] not in ('number','formula') or atom['value'] is None:continue
                if sheet and norm(atom['sheet'])!=norm(sheet):continue
                if 'cell' in selector:
                    if atom['address']==selector['cell']:matches.append(atom)
                    continue
                if norm(selector['row']) not in [norm(x) for x in atom['row_labels']]:continue
                headers=[h for h in atom['header_candidates'] if h['column']==atom['column']]
                source_row=next((r for r in selected if r['evidence']==atom['evidence']),None)
                source_headers=[h for h in source_row['fields'].get('headers',[]) if h['column']==atom['column']] if source_row else headers
                wanted=[h for h in headers if norm(selector['column'])==norm(h['text'])]
                if len(wanted)!=1:continue
                leaf=wanted[0]
                valid=True
                for parent in source_headers:
                    if parent['address']==leaf['address']:continue
                    bounds=re.fullmatch(r'([A-Z]+)([0-9]+):([A-Z]+)([0-9]+)',parent.get('merged_range') or '')
                    if not bounds:
                        # Two adjacent header rows may use centred text rather
                        # than an OOXML merge. Bind only the unique latest leaf,
                        # with another peer column on that same header row.
                        parent_row=int(re.search(r'[0-9]+',parent['address']).group())
                        leaf_row=int(re.search(r'[0-9]+',leaf['address']).group())
                        peers=source_row['fields'].get('headers',[]) if source_row else []
                        if (parent_row+1==leaf_row and norm(parent['text'])!=norm(leaf['text'])
                            and any(h.get('row')==leaf_row and h['column']!=atom['column'] for h in peers)):
                            continue
                        valid=False;break
                    def column_number(col):
                        n=0
                        for c in col:n=n*26+ord(c)-64
                        return n
                    left,top,right,bottom=bounds.groups()
                    if not (column_number(left)<column_number(right) and column_number(left)<=column_number(atom['column'])<=column_number(right) and int(bottom)<int(re.search(r'[0-9]+',leaf['address']).group())):
                        valid=False;break
                if not valid:continue
                if atom['formula'] and atom['cache_state']!='present':continue
                matches.append(atom)
            if not matches:value.update(reason='comparison_selector_not_resolved' if index==operand_count else 'selector_not_resolved_in_returned_memory');return
            sheets=sorted({m['sheet'] for m in matches})
            if not sheet and len(sheets)>1:
                if key in internal['asked'] or len(internal['asked'])>=2:value.update(state='context_unknown',reason='confirmation_bound');return
                value.update(state='needs_confirmation',reason='multiple_source_sheets',confirmation=dict(key=key,question='どの原シートの値を参照しますか。',options=sheets))
                if issue_prompt:internal['asked'].append(key);value['budgets']['prompts']=len(internal['asked'])
                return
            unique={canonical(dict(value=m['value'],address=m['address'],ref=m['evidence'])):m for m in matches}
            if len(unique)!=1:value.update(state='conditions_conflict',reason='ambiguous_row_or_header_binding');return
            candidates.append(next(iter(unique.values())))
        comparison=candidates.pop() if task.get('compare_to') else None
        selectors=selectors[:operand_count]
        try:values=[Decimal(str(c['value'])) for c in candidates];scale=Decimal(task.get('scale','1'))
        except (InvalidOperation,ValueError):value.update(state='semantic_unavailable',reason='non_numeric_operands');return
        numbers=values+[scale]+([Decimal(str(comparison['value']))] if comparison else [])
        if any(not v.is_finite() or abs(v.adjusted())>300 or len(v.as_tuple().digits)>128 for v in numbers):
            value.update(state='semantic_unavailable',reason='arithmetic_numeric_bound');return
        op=task.get('operation','identity');result=None
        if task.get('unit') and op in ('ratio','percentage_point') and 'scale' not in task:
            value.update(state='semantic_unavailable',reason='unit_conversion_plan_not_implemented');return
        if comparison and op=='identity' and len(values)>1:
            value.update(state='semantic_unavailable',reason='comparison_requires_single_result');return
        try:
            with localcontext() as ctx:
                ctx.prec=1024;ctx.Emax=1000;ctx.Emin=-1000
                if op=='identity' and values:result=values
                elif op=='sum':result=sum(values)
                elif op in ('difference','percentage_point') and len(values)==2:result=values[0]-values[1]
                elif op in ('ratio','percentage') and len(values)==2 and values[1]!=0:result=values[0]/values[1]*(100 if op=='percentage' else 1)
                if result is None:value.update(state='semantic_unavailable',reason='unsupported_operand_count_or_zero_denominator');return
                arithmetic_inexact=ctx.flags[Inexact]
                results=result if isinstance(result,list) else [result]
                claims=[]
                constants=[dict(kind='requested_constant',role='scale',value=task['scale'],provenance='explicit_task_input',source_evidence=None)] if 'scale' in task else []
                if op=='percentage':constants=constants+[dict(kind='operation_constant',role='percentage_multiplier',value='100',provenance='explicit_percentage_operation',source_evidence=None)]
                for index,raw in enumerate(results):
                    ctx.clear_flags();scaled=raw*scale
                    displayed=scaled.quantize(Decimal(1).scaleb(-task['precision']),rounding=ROUND_HALF_UP) if 'precision' in task else scaled
                    pairs=[(values[index],candidates[index])] if op=='identity' else list(zip(values,candidates))
                    claim=dict(kind='source_table_calculation',rounded=arithmetic_inexact or ctx.flags[Inexact],calculation_precision_digits=1024,value=str(displayed),requested_unit=task.get('unit',''),unit_status='not_semantically_verified',operation='lookup' if op=='identity' else op,operands=[dict(value=str(v),cell_address=c['address'],sheet=c['sheet'],support_evidence=c['support_evidence']) for v,c in pairs],constants=constants)
                    if op=='identity':claim['selector']=selectors[index]
                    if comparison:
                        precision=task.get('comparison_precision',task.get('precision'))
                        compared=scaled.quantize(Decimal(1).scaleb(-precision),rounding=ROUND_HALF_UP) if precision is not None else scaled
                        target=Decimal(str(comparison['value']))
                        claim['comparison']=dict(result='equal' if compared==target else 'different',compared_value=str(compared),precision=precision,
                            rounding='half_up_from_unrounded_scaled_result',target=dict(value=str(target),cell_address=comparison['address'],sheet=comparison['sheet'],support_evidence=comparison['support_evidence']))
                    claims.append(claim)
        except DecimalException:
            value.update(state='semantic_unavailable',reason='arithmetic_numeric_bound');return
        value.update(state='answered_calculation',reason='source_cells_and_requested_arithmetic_resolved',claims=claims)
        value['stages'].update(decision='arithmetic_result_only_authority_unassessed',explicit_task_complete=True,
            comparison_requested=comparison is not None,scale_origin='explicit_task_input' if 'scale' in task else 'identity_multiplier_no_unit_inference')

    def _internal(self,db,ctx,id):
        internal=self.store.record(db,ctx['tenant'],'evidence_task_internals',id)
        if internal['creator']!=ctx['actor']:raise APIError(404,'not_found')
        self._material(db,ctx,internal);return internal

    def report_claim(self,db,ctx,reference):
        """Pin a completed deterministic calculation, rechecking its live basis."""
        from .contracts import CALCULATION_REF
        validate(reference,CALCULATION_REF)
        internal=self._internal(db,ctx,reference['task_id'])
        if reference['revision']!=internal['revision']:
            raise APIError(409,'calculation_revision_changed')
        snap=self.store.record(db,ctx['tenant'],'evidence_task_snapshots',digest([internal['id'],reference['revision']])[:32])
        value=snap['response']
        self.verify_response(db,ctx,value,'/v1/evidence-tasks/{id}')
        if (value['state']!='answered_calculation' or not value['stages']['explicit_task_complete']
                or internal['input'].get('retrieval_policy')!='conditional'
                or value['task']['kind'] not in ('table_lookup','table_calculation')):
            raise APIError(422,'report_calculation_not_complete')
        index=reference['claim_index']
        if index>=len(value['claims']):raise APIError(422,'report_calculation_claim_not_found')
        claim=value['claims'][index]
        if claim['kind']!='source_table_calculation':raise APIError(422,'report_calculation_not_supported')
        return dict(reference=dict(reference),response_sha256=digest(value),policy_version=value['policy_version'],
                    task=value['task'],scope=value['scope'],claim=claim)

    def handle(self,db,ctx,route,params,body,if_match=None):
        if route=='/v1/evidence-tasks':
            if body.get('answer_mode') and (body['task']!={'kind':'reference'} or body.get('retrieval_policy','static')=='conditional'):
                raise APIError(422,'short_answer_reference_query_only')
            selectors=list(body['task'].get('selectors',[]))+([body['task']['compare_to']] if body['task'].get('compare_to') else [])
            for selector in selectors:
                if 'cell' in selector:
                    if 'sheet' not in selector or 'row' in selector or 'column' in selector:raise APIError(422,'invalid_cell_selector')
                    from .xlsx_adapter import _coordinate
                    _coordinate(selector['cell'])
                    if body.get('retrieval_policy')!='conditional':raise APIError(422,'cell_selector_requires_conditional')
                elif not {'row','column'}<=selector.keys():raise APIError(422,'invalid_table_selector')
            internal=dict(id=uid(),creator=ctx['actor'],revision=1,input=body,basis=self._basis(db,ctx,body),observations=[],asked=[],prompt_revisions={})
            value=self.compute(db,ctx,internal,issue_prompt=True)
            assets=[p['asset_id'] for p in internal['basis']];self.store.save_record(db,ctx['tenant'],'evidence_task_internals',internal,assets)
        else:
            internal=self._internal(db,ctx,params['id']);assets=[p['asset_id'] for p in internal['basis']]
            if route.endswith('/observations'):
                if body['expected_revision']!=internal['revision']:raise APIError(409,'revision_conflict')
                if internal['revision']>=9:raise APIError(409,'evidence_task_update_limit')
                previous=self.store.record(db,ctx['tenant'],'evidence_task_snapshots',digest([internal['id'],internal['revision']])[:32])['response']
                allowed=previous['confirmation']['key'] if previous['confirmation'] else (internal['asked'][-1] if previous['state']=='context_unknown' and internal['asked'] else None)
                if not body['observations']:raise APIError(422,'empty_observations')
                for o in body['observations']:
                    if o['key']!=allowed or (o['status']=='unknown')!=(o['value'] is None):raise APIError(422,'unrequested_observation')
                    internal['observations'].append(o)
                internal['revision']+=1;value=self.compute(db,ctx,internal,issue_prompt=True)
                db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?',(canonical(internal),ctx['tenant'],'evidence_task_internals',internal['id']))
            else:
                snap=self.store.record(db,ctx['tenant'],'evidence_task_snapshots',digest([internal['id'],internal['revision']])[:32]);return 200,snap['response'],[],{}
        snapshot=dict(id=digest([internal['id'],internal['revision']])[:32],state_input=internal,response=value)
        self.store.save_record(db,ctx['tenant'],'evidence_task_snapshots',snapshot,assets)
        return 201,value,assets,{}

    def verify_response(self,db,ctx,value,route,receipt_replay=False):
        internal=self._internal(db,ctx,value['id']);snap=self.store.record(db,ctx['tenant'],'evidence_task_snapshots',digest([value['id'],value['revision']])[:32])
        if canonical(value)!=canonical(snap['response']):raise APIError(409,'evidence_task_response_integrity')
        if value['revision']==internal['revision'] and canonical(internal)!=canonical(snap['state_input']):raise APIError(409,'evidence_task_context_integrity')
        expected=self.compute(db,ctx,snap['state_input'])
        # Previously issued pending prompt stays visible on replay without issuing again.
        if value['state']=='needs_confirmation' and expected['state']=='context_unknown':
            pending=json.loads(canonical(snap['state_input']));pending['asked']=[k for k in pending['asked'] if k!=value['confirmation']['key']]
            expected=self.compute(db,ctx,pending);expected['budgets']['prompts']=len(snap['state_input']['asked'])
        if canonical(value)!=canonical(expected):raise APIError(409,'evidence_task_decision_integrity')
        validate(value,RESPONSE)
