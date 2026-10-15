"""Composable API dispatch; no process globals or external adapters."""
import re
import threading
import time
import shutil
from .contracts import APIError,OPERATIONS,SCHEMAS,canonical,digest,openapi,validate,response_schema,required_scopes,register_module
from .library import CAPABILITIES,Library
from .diagnosis import Diagnosis
from .reports import Reports
from .assist import Assist
from .grounded import Grounded,GROUNDED_SCHEMAS,RETRIEVAL_MODEL_VERSION
from .image_search import ImageSearch
from .table_adapter import TableExtraction,TABLE_SCHEMAS
from .xlsx_reports import XLSXReports
from .governance import Governance,GOVERNANCE_SCHEMAS,GOVERNANCE_OPERATIONS,GOVERNANCE_RESPONSE_SCHEMAS
from .maintenance import Maintenance,MAINTENANCE_SCHEMAS,MAINTENANCE_OPERATIONS,MAINTENANCE_RESPONSE_SCHEMAS
from .release_assist import ReleaseAssist,RELEASE_ASSIST_SCHEMAS,RELEASE_ASSIST_OPERATIONS,RELEASE_ASSIST_RESPONSE_SCHEMAS
from .clarifications import Clarifications,CLARIFICATION_SCHEMAS,CLARIFICATION_OPERATIONS,CLARIFICATION_RESPONSE_SCHEMAS
from .evidence_assist import EvidenceAssist,ASSIST_SCHEMAS,ASSIST_OPERATIONS,ASSIST_RESPONSES
register_module(GOVERNANCE_SCHEMAS,GOVERNANCE_OPERATIONS,GOVERNANCE_RESPONSE_SCHEMAS)
register_module(MAINTENANCE_SCHEMAS,MAINTENANCE_OPERATIONS,MAINTENANCE_RESPONSE_SCHEMAS)
register_module(RELEASE_ASSIST_SCHEMAS,RELEASE_ASSIST_OPERATIONS,RELEASE_ASSIST_RESPONSE_SCHEMAS)
register_module(CLARIFICATION_SCHEMAS,CLARIFICATION_OPERATIONS,CLARIFICATION_RESPONSE_SCHEMAS)
register_module(ASSIST_SCHEMAS,ASSIST_OPERATIONS,ASSIST_RESPONSES)
SCHEMAS.update(TABLE_SCHEMAS)
SCHEMAS.update(GROUNDED_SCHEMAS)
SCHEMAS['RetrievalModelVersion']=RETRIEVAL_MODEL_VERSION

class Auth:
    """Injected local principals. Revocation is checked on every use and commit.

    Production SSO/JWT introspection belongs in the host's authentication layer.
    No browser-provided tenant or authorization revision is trusted here.
    """
    def __init__(self,principals):
        self.principals=principals
        self.lock=threading.RLock()

    def authenticate(self,token):
        with self.lock:
            context=self.principals.get(token)
            if not context or not context.get('active',True) or context.get('expires_at',float('inf'))<=time.time():
                raise APIError(401,'authentication_required')
            return {**context,'scopes':list(context['scopes'])}

    def authorized_worker(self,tenant,actor,revision):
        with self.lock:
            return any(p['tenant']==tenant and p['actor']==actor and p['revision']==revision and 'write' in p['scopes']
                       and p.get('active',True) and p.get('expires_at',float('inf'))>time.time() for p in self.principals.values())

    def revoke(self,token):
        with self.lock:
            self.principals[token]['active']=False

class API:
    def __init__(self,store,auth,encoder=None,decision_model=None,short_answer_model=None):
        self.store,self.auth=store,auth
        if encoder is not None:
            validate(encoder.metadata,RETRIEVAL_MODEL_VERSION)
        self.library=Library(store,auth.authorized_worker,encoder)
        self.diagnosis=Diagnosis(store,self.library)
        self.reports=Reports(store,self.library)
        self.assist=Assist(store,self.library)
        self.grounded=Grounded(store,self.library)
        self.image_search=ImageSearch(store,self.library)
        self.tables=TableExtraction(store,self.library)
        self.xlsx=XLSXReports(store,self.library,self.grounded,self.reports)
        self.governance=Governance(store,self.library,auth)
        self.store.resource_access=self.governance.asset_allowed
        self.store.resource_edit=self.governance.asset_writable
        self.store.worker_access=self.governance.worker_allowed
        self.library.on_source_changed=self.governance.source_changed
        self.maintenance=Maintenance(store,self.library,self.governance,auth)
        self.release_assist=ReleaseAssist(store,self.grounded,self.maintenance)
        self.clarifications=Clarifications(store,self.library)
        self.evidence_assist=EvidenceAssist(store,self.library,decision_model,short_answer_model)
        self.reports.evidence_assist=self.evidence_assist
        self.library.on_delete=self.maintenance.on_delete
        if decision_model is not None:
            def remove_with_model_cache(db,tenant,asset):
                self.maintenance.on_delete(db,tenant,asset)
                decision_model.clear()
            self.library.on_delete=remove_with_model_cache
        self.module_routes={path:self.governance for _,path,*_ in GOVERNANCE_OPERATIONS}
        self.module_routes.update({path:self.maintenance for _,path,*_ in MAINTENANCE_OPERATIONS})
        self.module_routes.update({path:self.release_assist for _,path,*_ in RELEASE_ASSIST_OPERATIONS})
        self.module_routes.update({path:self.clarifications for _,path,*_ in CLARIFICATION_OPERATIONS})
        self.module_routes.update({path:self.evidence_assist for _,path,*_ in ASSIST_OPERATIONS})

    def match(self,method,path):
        for verb,template,schema,status,scope in OPERATIONS:
            pattern=re.escape(template)
            for name in re.findall(r'\{(\w+)\}',template):
                pattern=pattern.replace(re.escape('{'+name+'}'),r'(?P<'+name+'>'+('[1-9][0-9]{0,2}' if name=='version' else '[a-f0-9]{32}')+')')
            match=re.fullmatch(pattern,path)
            if method.lower()==verb and match:
                return template,schema,status,scope,match.groupdict()
        raise APIError(404,'not_found')

    def verify_result(self,db,tenant,value):
        """Saved responses and cache hits still resolve their pinned evidence."""
        if isinstance(value,dict):
            if isinstance(value.get('evidence'),dict) and 'segment_id' in value['evidence']:
                source=self.library.resolve(db,tenant,value['evidence'])
                if all(k in value for k in ('start_char','end_char','quote_context_sha256','quote')):
                    start,end=value['start_char'],value['end_char']
                    if not 0<=start<end<=len(source['quote']) or value['quote']!=source['quote'][start:end] or value['quote_context_sha256']!=digest(source['quote'].encode()) or value['source_sha256']!=source['source_sha256'] or canonical(value['locator'])!=canonical(source['locator']):
                        raise APIError(409,'evidence_integrity_failed')
                elif 'quote' in value or 'fields' in value or 'claim_kind' in value:
                    if any(k not in value or canonical(value[k])!=canonical(v) for k,v in source.items()):
                        raise APIError(409,'evidence_integrity_failed')
            if 'inputs' in value and isinstance(value['inputs'],list):
                for ref in value['inputs']:
                    if isinstance(ref,dict) and 'segment_id' in ref:
                        self.library.resolve(db,tenant,ref)
            for item in value.values():
                self.verify_result(db,tenant,item)
        elif isinstance(value,list):
            for item in value:
                self.verify_result(db,tenant,item)

    def verify_response(self,db,tenant,route,value):
        if route.startswith('/v1/jobs/'):
            self.library.verify_job(db,tenant,value)
        elif route=='/v1/assets/{id}':
            for version in value['versions']:
                self.library.verify_job(db,tenant,self.store.job(db,tenant,version['job_id']))
        elif route.startswith('/v1/xlsx-templates'):
            self.xlsx.verify_template(value)
        elif route.startswith('/v1/xlsx-reports'):
            self.xlsx.verify_saved(db,tenant,value)
        elif route.startswith('/v1/table-extractions'):
            self.tables.verify_saved(db,tenant,value)
        elif route.startswith('/v1/grounded-answers'):
            self.grounded.verify_saved(db,tenant,value)
        elif route.startswith('/v1/answers'):
            self.assist.verify_saved(db,tenant,value)
        elif route.startswith('/v1/processes'):
            self.diagnosis.verify_process(value)
        elif route.startswith('/v1/diagnoses'):
            self.diagnosis.verify_saved(db,tenant,value)
        elif route.startswith('/v1/templates'):
            self.reports.verify_template(value)
        elif route.startswith('/v1/reports'):
            self.reports.verify_draft(db,tenant,value,getattr(self.store.request_local,'context',None))
        self.verify_result(db,tenant,value)
        validate(value,SCHEMAS[response_schema('GET',route)])

    def dispatch(self,method,path,token,body=None,key=None,*,if_match=None):
        if path=='/openapi.json' and method=='GET':
            return 200,openapi(),{}
        ctx=self.auth.authenticate(token)
        with self.store.request_scope(ctx):
            return self._dispatch(method,path,token,body,key,if_match=if_match,ctx=ctx)

    def _dispatch(self,method,path,token,body=None,key=None,*,if_match=None,ctx):
        if path=='/openapi.json' and method=='GET':
            return 200,openapi(),{}
        route,schema,status,scope,params=self.match(method,path)
        scopes=required_scopes(route,scope)
        if not scopes<=set(ctx['scopes']):
            raise APIError(403,'scope_denied')
        def check_current():
            current=self.auth.authenticate(token)
            if any(current[k]!=ctx[k] for k in ('tenant','actor','revision')) or current.get('actor_type','machine')!=ctx.get('actor_type','machine') or not scopes<=set(current['scopes']):
                raise APIError(403,'authorization_changed')
        if schema:
            validate(body,SCHEMAS[schema])
        module=self.module_routes.get(route)
        if module is not None:
            def guard(db):
                check_current()
                if 'project_id' in params:
                    self.governance.project_guard(db,ctx,params['project_id'])
            readonly=method=='GET' or route.endswith(('/event-queries','/release-queries'))
            if readonly:
                with self.store.tx() as db:
                    guard(db)
                    result=module.handle(db,ctx,route,params,body,if_match)
                    status,value,_,headers=result
                    validate(value,SCHEMAS[response_schema(method,route)])
                    module.verify_response(db,ctx,value,route)
                    check_current()
                    return status,value,headers
            headers={}
            def focused_action(db):
                guard(db)
                status,value,deps,selected_headers=module.handle(db,ctx,route,params,body,if_match)
                headers.update(selected_headers)
                validate(value,SCHEMAS[response_schema(method,route)])
                module.verify_response(db,ctx,value,route,receipt_replay=True)
                check_current()
                return status,value,deps
            # A receipt is looked up before ETag and current execution gates.
            # Precondition headers govern a new execution, not recovery of an
            # already committed receipt for the same semantic payload.
            def current_view():
                check_current()
                if 'project_id' in params:
                    self.governance.project_guard(self.store.db,ctx,params['project_id'])
            status,value=self.store.mutate(ctx,key,method+' '+path,body,focused_action,authorize=current_view,response_headers=headers)
            with self.store.tx() as db:
                guard(db)
                module.verify_response(db,ctx,value,route,receipt_replay=True)
                validate(value,SCHEMAS[response_schema(method,route)])
                check_current()
            return status,value,headers
        if route in ('/v1/search','/v1/image-search'):
            with self.store.tx() as db:
                check_current()
                result=(self.library.search(db,ctx['tenant'],body) if route=='/v1/search' else self.image_search.search(db,ctx['tenant'],body))
                validate(result,SCHEMAS[response_schema(method,route)])
                check_current()
                return 200,result,{}
        if method in ('POST','DELETE'):
            def operation(db):
                # Check revocation again after waiting for the storage lock.
                check_current()
                if route=='/v1/assets':
                    return self.library.ingest(db,ctx,body)
                if route=='/v1/assets/{id}':
                    self.store.check_asset_edit(db,ctx,params['id'],'delete')
                    return self.library.delete(db,ctx['tenant'],params['id'])
                if route in ('/v1/jobs/{id}/cancel','/v1/jobs/{id}/retry'):
                    return self.library.control_job(db,ctx,params['id'],route.split('/')[-1],body)
                if route=='/v1/xlsx-templates':
                    return self.xlsx.template(db,ctx,body)
                if route=='/v1/xlsx-reports':
                    return self.xlsx.render(db,ctx,body)
                if route=='/v1/table-extractions':
                    return self.tables.extract(db,ctx,body)
                if route=='/v1/grounded-answers':
                    return self.grounded.answer(db,ctx,body)
                if route=='/v1/answers':
                    return self.assist.answer(db,ctx,body)
                if route=='/v1/processes':
                    return self.diagnosis.process(db,ctx,body)
                if route=='/v1/diagnoses':
                    return self.diagnosis.evaluate(db,ctx,body)
                if route=='/v1/templates':
                    return self.reports.template(db,ctx,body)
                if route=='/v1/reports':
                    return self.reports.render(db,ctx,body)
                raise APIError(404,'not_found')
            def action(db):
                result=operation(db)
                validate(result[1],SCHEMAS[response_schema(method,route)])
                check_current()
                return result
            status,value=self.store.mutate(ctx,key,method+' '+path,body,action,authorize=check_current)
            with self.store.tx() as db:
                check_current()
                if method!='DELETE':
                    self.verify_response(db,ctx['tenant'],route,value)
                check_current()
            if method=='DELETE':
                self.store.checkpoint()
            return status,value,{}
        with self.store.tx() as db:
            check_current()
            if route=='/v1/capabilities':
                value={**CAPABILITIES,'report':{**CAPABILITIES['report'],'native_xlsx_profile':'completion-inspection-reference-v1','xlsx_pdf_rendering':'xlsx_reviewed_reference_or_verified_calculation_template','native_xlsx_profiles':['completion-inspection-reference-v1','calculation-report-xlsx-v1'],'calculated_xlsx_numeric_fields_max':10,'calculated_xlsx_formula_support':'preserve_only_independent_simple_formulas'},'library':{**CAPABILITIES['library'],'native_pdf_runtime_available':bool(shutil.which('pdfinfo') and shutil.which('pdftotext'))}}
                value['library'].update(text_semantic_search_available=self.library.encoder is not None,
                                        retrieval_modes=['lexical','bm25']+(['hybrid'] if self.library.encoder is not None else []),
                                        project_acl='registered_members_and_explicit_source_readers',
                                        candidate_release_publication='separate_reviewed_atomic_sqlite_head',
                                        durable_maintenance='bounded_verified_segment_bm25_v1')
                value['assist']={**value['assist']}
                if self.evidence_assist.decision_model is not None:
                    value['assist'].update(generative=False,model_used=None,configured_model_profile=self.evidence_assist.decision_model.metadata['profile'],local_generation_diagnostic=True,decision_worker='short_lived_cpu_process_hard_timeout_30s',model_answer_candidates=False,general_language_decision=False,semantic_quality_validated=False)
                value['assist'].update(release_bound_answers='one_published_generation_literal_reference_only',
                                       review_proposal='native_pdf_midrail_reference_v1',
                                       conditional_confirmation='human_defined_reference_conditions_v1',
                                       operational_authority=False)
                value['assist'].update(short_answer_profile='local-short-answer-v1' if self.evidence_assist.short_answer_model is not None else None,short_answer_candidates=self.evidence_assist.short_answer_model is not None,short_answer_semantic_quality_validated=False,experimental_generation_available=self.evidence_assist.short_answer_model is not None)
                value['execution']={**CAPABILITIES['execution'],'large_model_loads':int(self.library.encoder is not None)}
                value['execution'].update(periodic_execution='explicit_local_occurrence_ledger',automatic_scheduler=False,
                                          human_approval_identity='local_registered_fixture_not_production_sso')
                if self.library.encoder is not None:
                    value['library']['retrieval_model_version']=self.library.encoder.metadata
            elif route=='/v1/xlsx-reports/{id}/file':
                value=self.store.record(db,ctx['tenant'],'xlsx_reports',params['id'])
                self.verify_response(db,ctx['tenant'],route,value)
                output=self.xlsx.verify_saved(db,ctx['tenant'],value)
                check_current()
                return 200,output,{'X-Output-SHA256':value['output_sha256'],'Content-Disposition':'attachment; filename=navigator-reference-draft.xlsx'}
            elif route=='/v1/assets/{id}':
                value=self.library.get_asset(db,ctx['tenant'],params['id'])
            elif route.endswith('/original'):
                raw,sha=self.library.original(db,ctx['tenant'],params['id'],int(params['version']))
                check_current()
                return 200,raw,{'X-Source-SHA256':sha,'Content-Disposition':'attachment'}
            elif route=='/v1/jobs/{id}':
                value=self.store.job(db,ctx['tenant'],params['id'])
            else:
                kind=route.split('/')[2].replace('-','_')
                value=self.store.record(db,ctx['tenant'],kind,params['id'])
            self.verify_response(db,ctx['tenant'],route,value)
            check_current()
            return 200,value,{}
