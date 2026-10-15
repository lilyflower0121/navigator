"""Persistent local maintenance, immutable BM25 generations and reviewed publication.

The host owns transaction/idempotency/authentication. No scheduler, external I/O,
model or generic workflow is started here. Candidate data becomes readable through
normal project queries only after an atomic, separately approved head switch.
"""
import json
import math
import time
from .contracts import APIError, ID, EVIDENCE, SCHEMAS, array, canonical, digest, integer, obj, string, validate
from .governance import BASIS, ENUM, NUMBER, NULLTIME, etag
from .retrieval import CHUNK_POLICY, MAX_CHUNKS, chunks, term_counts
from .store import uid

PROFILE='verified-segment-bm25-v1'
PLAN='maintenance-sqlite-v1'
NID=dict(type=['string','null'],minLength=32,maxLength=32,pattern='^[a-f0-9]{32}$')
HASH=string(64,64)
ITEM=obj(dict(id=ID,source=EVIDENCE['properties']['asset_id'],source_version=integer(1,100),source_hash=HASH,
    required=dict(type='boolean'),profile=ENUM([PROFILE]),state=ENUM(['queued','running','succeeded','failed','skipped','cancelled']),
    attempt=integer(),checkpoint=ENUM(['pending','indexed']),error=dict(type=['string','null']),result_hash=dict(type=['string','null']),
    reused=dict(type='boolean')))
COUNTS=obj({s:integer() for s in ['queued','running','succeeded','failed','skipped','cancelled']})
COVERAGE=obj(dict(items=array(obj(dict(item_id=ID,source_id=ID,source_version=integer(1,100),source_hash=HASH,required=dict(type='boolean'),
    state=ENUM(['succeeded','failed','skipped','cancelled']),result_hash=dict(type=['string','null']),exclusion_reason=dict(type=['string','null']))),100),
    required_complete=dict(type='boolean'),source_coverage=ENUM(['project_bindings_snapshot']),optional_exclusions=integer()))
DOMAIN_REF=obj(dict(resource_type=ENUM(['candidate_reference_definition']),resource_id=ID,revision=integer(1),content_hash=HASH,
    proposal_id=ID,proposal_revision=integer(1),approval_id=ID))
MANIFEST=obj(dict(schema_version=ENUM(['library-release-v1']),release_id=ID,run_id=ID,basis=BASIS,profile=ENUM([PROFILE]),
    index_generation_id=NID,index_hash=dict(type=['string','null']),coverage=COVERAGE,domains=array(DOMAIN_REF,100),
    base_release_id=NID,expected_head_revision=integer(),captured_watermark=HASH,publication_intent=ENUM(['activate_verified_library'])))
RUN=obj(dict(id=ID,project_id=ID,release_id=ID,state=ENUM(['queued','running','succeeded','completed_with_errors','failed','blocked_permission','cancel_requested','cancelled']),
    state_version=integer(1),epoch=integer(),attempt=integer(),lease_until=NUMBER,plan_version=ENUM([PLAN]),profile=ENUM([PROFILE]),
    trigger=ENUM(['manual','schedule']),schedule_id=NID,schedule_revision=dict(type=['integer','null']),scheduled_for=NULLTIME,
    captured_watermark=HASH,started_at=NULLTIME,finished_at=NULLTIME,blocker=dict(type=['string','null']),counts=COUNTS,
    cancel_effect=ENUM(['none','publication_fenced','already_committed']),processing_stop=ENUM(['not_started','not_confirmed','observed_stopped'])))
RELEASE=obj(dict(id=ID,project_id=ID,revision=integer(1),state_version=integer(1),state=ENUM(['building','ready','published','failed','stale','withdrawn']),
    manifest=MANIFEST,manifest_digest=HASH,freshness=ENUM(['current','pending_update','stale','blocked']),
    current_use_state=ENUM(['resolved','blocked','stale','expired','needs_input']),published_at=NULLTIME))
HEAD=obj(dict(project_id=ID,release_id=NID,head_revision=integer(),state_version=integer(1),published_watermark=dict(type=['string','null']),
    freshness=ENUM(['current','pending_update','stale','blocked']),current_use_state=ENUM(['resolved','blocked','stale','expired','needs_input'])))
EVENT=obj(dict(event_id=ID,seq=integer(1),run_id=ID,run_revision=integer(1),attempt=integer(),type=string(80),recorded_at=NUMBER,
    final=dict(type='boolean'),result_release_id=NID))
SCHEDULE_INPUT=obj(dict(name=string(160),timezone=ENUM(['UTC']),frequency=ENUM(['manual_ticks']),next_scheduled_for=NUMBER,
    catch_up=ENUM(['latest']),allowed_lateness_seconds=integer(0,86400),max_backlog=integer(1,100),
    profile=ENUM([PROFILE]),publication_policy=ENUM(['human_review']),active=dict(type='boolean')))
SCHEDULE=obj(dict(id=ID,project_id=ID,revision=integer(1),state_version=integer(1),actor=string(100),auth_revision=integer(1),
    definition=SCHEDULE_INPUT,implementation=ENUM(['definition_and_occurrence_ledger_only'])))
OCCURRENCE=obj(dict(project_id=ID,schedule_id=ID,schedule_revision=integer(1),scheduled_for=NUMBER,run_id=NID,state=ENUM(['accepted','skipped_late','cancelled']),reason=dict(type=['string','null'])))
BINDING=obj(dict(id=ID,project_id=ID,release_id=ID,head_revision=integer(1),effective_at=NUMBER,known_at=NUMBER,context_revision=integer(1),
    business_policy_revision=integer(1),manifest_digest=HASH,state_version=integer(1)))
RESULT=obj(dict(evidence=EVIDENCE,segment=SCHEMAS['Segment'],score=dict(type='number',minimum=0)))
MAINTENANCE_SCHEMAS={
 'Phase4MaintenanceInput':obj(dict(profile=ENUM([PROFILE]),base_release_id=NID,effective_at=NUMBER,known_at=NUMBER),['profile']),
 'Phase4MaintenanceControl':obj(dict(reason=string(500)),[]),'Phase4ReleaseValidate':obj({}),
 'Phase4ReleasePublish':obj(dict(proposal_id=ID,approval_id=ID,manifest_digest=HASH,expected_head_revision=integer())),
 'Phase4ScheduleInput':SCHEDULE_INPUT,'Phase4OccurrenceInput':obj(dict(scheduled_for=NUMBER)),
 'Phase4EventQuery':obj(dict(after=integer(),limit=integer(1,100)),['after']),
 'Phase4ReadBindingInput':obj(dict(effective_at=NUMBER,known_at=NUMBER),[]),
 'Phase4ReleaseQueryInput':obj(dict(binding_id=ID,query=string(1000),limit=integer(1,20),mode=ENUM(['evidence','operational'])),['binding_id','query','mode']),
 'Phase4Run':RUN,'Phase4RunItems':obj(dict(project_id=ID,run_id=ID,items=array(ITEM,100),state_version=integer(1))),
 'Phase4RunEvents':obj(dict(project_id=ID,run_id=ID,events=array(EVENT,100),cursor=integer(),state_version=integer(1))),
 'Phase4Release':RELEASE,'Phase4LibraryHead':HEAD,'Phase4Schedule':SCHEDULE,'Phase4Occurrence':OCCURRENCE,
 'Phase4ReadBinding':BINDING,'Phase4ReleaseQuery':obj(dict(project_id=ID,binding=BINDING,mode=ENUM(['evidence','operational']),
    results=array(RESULT,20),status=ENUM(['supported','unsupported','blocked']),blockers=array(string(100),10),model_used=dict(type='null'),
    score_meaning=ENUM(['BM25 relevance; not factual confidence']),state_version=integer(1))),
}
P='/v1/projects/{project_id}'
MAINTENANCE_OPERATIONS=tuple((verb,P+'/'+suffix,schema,status,scope) for verb,suffix,schema,status,scope in [
 ('post','maintenance-runs','Phase4MaintenanceInput',202,'write'),('get','runs/{id}',None,200,'read'),
 ('get','runs/{id}/items',None,200,'read'),('post','runs/{id}/event-queries','Phase4EventQuery',200,'read'),
 ('post','runs/{id}/resume','Phase4MaintenanceControl',202,'write'),('post','runs/{id}/retry-failed-items','Phase4MaintenanceControl',202,'write'),
 ('post','runs/{id}/cancel','Phase4MaintenanceControl',200,'write'),('get','library-head',None,200,'read'),
 ('get','library-releases/{id}',None,200,'read'),('post','library-releases/{id}/validate','Phase4ReleaseValidate',200,'write'),
 ('post','library-releases/{id}/publish','Phase4ReleasePublish',200,'write'),
 ('post','maintenance-schedules','Phase4ScheduleInput',201,'write'),('get','maintenance-schedules/{id}',None,200,'read'),
 ('post','maintenance-schedules/{id}/revisions','Phase4ScheduleInput',200,'write'),
 ('post','maintenance-schedules/{id}/occurrences','Phase4OccurrenceInput',202,'write'),
 ('post','read-bindings','Phase4ReadBindingInput',201,'read'),('get','read-bindings/{id}',None,200,'read'),
 ('post','release-queries','Phase4ReleaseQueryInput',200,'read')])
MAINTENANCE_RESPONSE_SCHEMAS={}
MAINTENANCE_READONLY_POSTS={P+'/runs/{id}/event-queries',P+'/release-queries'}
for verb,path,*_ in MAINTENANCE_OPERATIONS:
    result='Phase4Run'
    if path.endswith('/items'): result='Phase4RunItems'
    elif path.endswith('/event-queries'): result='Phase4RunEvents'
    elif '/library-releases/' in path: result='Phase4Release'
    elif path.endswith('/library-head'): result='Phase4LibraryHead'
    elif '/maintenance-schedules' in path: result='Phase4Occurrence' if path.endswith('/occurrences') else 'Phase4Schedule'
    elif '/read-bindings' in path: result='Phase4ReadBinding'
    elif path.endswith('/release-queries'): result='Phase4ReleaseQuery'
    MAINTENANCE_RESPONSE_SCHEMAS[(verb,path)]=result

DDL=[
 'CREATE TABLE IF NOT EXISTS ph4_runs(tenant TEXT,project TEXT,id TEXT,release_id TEXT,actor TEXT,auth_revision INTEGER,state TEXT,state_version INTEGER,epoch INTEGER,attempt INTEGER,lease REAL,data TEXT,PRIMARY KEY(tenant,project,id))',
 'CREATE TABLE IF NOT EXISTS ph4_run_items(tenant TEXT,project TEXT,run TEXT,id TEXT,item_key TEXT,data TEXT,PRIMARY KEY(tenant,project,run,id))',
 'CREATE TABLE IF NOT EXISTS ph4_item_results(tenant TEXT,item_key TEXT,result_hash TEXT,data TEXT,PRIMARY KEY(tenant,item_key))',
 'CREATE TABLE IF NOT EXISTS ph4_releases(tenant TEXT,project TEXT,id TEXT,revision INTEGER,state_version INTEGER,state TEXT,data TEXT,manifest_digest TEXT,published_at REAL,PRIMARY KEY(tenant,project,id))',
 'CREATE TABLE IF NOT EXISTS ph4_heads(tenant TEXT,project TEXT,release_id TEXT,revision INTEGER,watermark TEXT,PRIMARY KEY(tenant,project))',
 'CREATE TABLE IF NOT EXISTS ph4_index_generations(tenant TEXT,project TEXT,id TEXT,release_id TEXT,profile TEXT,artifact_hash TEXT,chunk_count INTEGER,PRIMARY KEY(tenant,project,id))',
 'CREATE TABLE IF NOT EXISTS ph4_index_chunks(tenant TEXT,project TEXT,generation TEXT,id INTEGER,data TEXT,PRIMARY KEY(tenant,project,generation,id))',
 'CREATE TABLE IF NOT EXISTS ph4_run_events(tenant TEXT,project TEXT,run TEXT,seq INTEGER,id TEXT,data TEXT,PRIMARY KEY(tenant,project,run,seq))',
 'CREATE TABLE IF NOT EXISTS ph4_schedules(tenant TEXT,project TEXT,id TEXT,revision INTEGER,state_version INTEGER,data TEXT,PRIMARY KEY(tenant,project,id))',
 'CREATE TABLE IF NOT EXISTS ph4_schedule_versions(tenant TEXT,project TEXT,id TEXT,revision INTEGER,data TEXT,PRIMARY KEY(tenant,project,id,revision))',
 'CREATE TABLE IF NOT EXISTS ph4_occurrences(tenant TEXT,project TEXT,schedule TEXT,scheduled_for REAL,data TEXT,PRIMARY KEY(tenant,project,schedule,scheduled_for))',
 'CREATE TABLE IF NOT EXISTS ph4_read_bindings(tenant TEXT,project TEXT,id TEXT,data TEXT,PRIMARY KEY(tenant,project,id))',
]

class Maintenance:
    def __init__(self,store,library,governance,auth,clock=time.time):
        self.store,self.library,self.gov,self.auth,self.clock=store,library,governance,auth,clock
        with store.tx() as db:
            for statement in DDL: db.execute(statement)
        governance.releases=self

    def _capacity(self,db,tenant,table,maximum):
        if db.execute('SELECT count(*) FROM '+table+' WHERE tenant=?',(tenant,)).fetchone()[0]>=maximum:
            raise APIError(413,'maintenance_capacity_limit')

    def _current_times(self,body):
        # Context and business policy are current immutable revisions. Full
        # as-of registry selection is not implemented for either time axis.
        now=self.clock()
        if any(axis in body and abs(body[axis]-now)>1 for axis in ('effective_at','known_at')):
            raise APIError(501,'historical_authority_unavailable')

    def _row(self,db,ctx,project,table,id):
        self.gov.project_guard(db,ctx,project)
        row=db.execute('SELECT * FROM '+table+' WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,id)).fetchone()
        if row is None: raise APIError(404,'not_found')
        return row

    def _access(self,db,ctx,project,manifest):
        self.gov.project_guard(db,ctx,project)
        for ref in manifest['basis']['inputs']:
            b=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,ref['resource_id'])).fetchone()
            if b is None or b['state']!='active' or ctx['actor'] not in json.loads(b['readers']): raise APIError(404,'not_found')
            self.store.live(db,ctx['tenant'],ref['resource_id'])

    def _run(self,db,ctx,project,id):
        row=self._row(db,ctx,project,'ph4_runs',id)
        release=self._release_row(db,ctx,project,row['release_id'])
        self._access(db,ctx,project,json.loads(release['data']))
        value=json.loads(row['data']);value.update(state=row['state'],state_version=row['state_version'],epoch=row['epoch'],attempt=row['attempt'],lease_until=row['lease'])
        items=self._items(db,ctx,project,id)
        value['counts']={state:sum(i['state']==state for i in items) for state in COUNTS['properties']}
        return value

    def _items(self,db,ctx,project,id):
        return [json.loads(row['data']) for row in db.execute('SELECT data FROM ph4_run_items WHERE tenant=? AND project=? AND run=? ORDER BY id',(ctx['tenant'],project,id))]

    def _event(self,db,ctx,project,run,kind,final=False):
        row=db.execute('SELECT * FROM ph4_runs WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,run)).fetchone()
        seq=db.execute('SELECT coalesce(max(seq),0)+1 FROM ph4_run_events WHERE tenant=? AND project=? AND run=?',(ctx['tenant'],project,run)).fetchone()[0]
        if seq>1024: raise APIError(413,'maintenance_event_capacity_limit')
        value=dict(event_id=uid(),seq=seq,run_id=run,run_revision=row['state_version'],attempt=row['attempt'],type=kind,recorded_at=self.clock(),final=final,result_release_id=row['release_id'] if final else None)
        db.execute('INSERT INTO ph4_run_events VALUES(?,?,?,?,?,?)',(ctx['tenant'],project,run,seq,value['event_id'],canonical(value)))

    def _release_row(self,db,ctx,project,id): return self._row(db,ctx,project,'ph4_releases',id)

    def _head_row(self,db,ctx,project):
        self.gov.project_guard(db,ctx,project)
        row=db.execute('SELECT * FROM ph4_heads WHERE tenant=? AND project=?',(ctx['tenant'],project)).fetchone()
        return dict(release_id=row['release_id'],revision=row['revision'],watermark=row['watermark']) if row else dict(release_id=None,revision=0,watermark=None)

    def _match(self,value,match):
        if match is None: raise APIError(428,'precondition_required')
        if match!=etag(value): raise APIError(412,'revision_mismatch')

    def _current(self,db,ctx,project,manifest,domains=True):
        if not self.gov._complete(db,ctx,project): return 'blocked'
        if self.gov.conflict_blocked(db,ctx['tenant'],project): return 'blocked'
        current=self.gov._basis(db,ctx,project,manifest['basis']['effective_at'],manifest['basis']['known_at'])
        if canonical(current)!=canonical(manifest['basis']): return 'stale'
        if domains:
            for ref in manifest['domains']:
                proposal=self.gov._get(db,ctx,project,ref['proposal_id'])
                state,_,_=self.gov._gate(db,ctx,project,proposal)
                if state!='resolved': return state
        return 'resolved'

    def _read_state(self,db,ctx,project,row):
        manifest=json.loads(row['data'])
        self._access(db,ctx,project,manifest)
        state=self._current(db,ctx,project,manifest)
        # Publication's own epoch increment does not obsolete its approved input.
        if row['state']=='published' and state=='stale':
            current=self.gov._basis(db,ctx,project,manifest['basis']['effective_at'],manifest['basis']['known_at'])
            expected=dict(manifest['basis'],scope_epoch=manifest['basis']['scope_epoch']+1)
            if canonical(current)==canonical(expected):
                state='resolved'
                for ref in manifest['domains']:
                    domain=db.execute('SELECT data FROM ph4_domain_versions WHERE tenant=? AND project=? AND id=? AND revision=?',(ctx['tenant'],project,ref['resource_id'],ref['revision'])).fetchone()
                    if domain is None: raise APIError(409,'domain_integrity_failed')
                    p=json.loads(domain[0]);period=p['payload']['valid_time'];now=self.clock()
                    if period['start_kind']!='known' or period['end_kind']=='unknown': state='needs_input'
                    elif now<period['start_at'] or period['end_kind']=='known' and now>=period['end_at']: state='expired'
        pending=db.execute("SELECT 1 FROM ph4_runs WHERE tenant=? AND project=? AND state IN ('queued','running')",(ctx['tenant'],project)).fetchone()
        freshness='blocked' if state=='blocked' else 'stale' if state!='resolved' else 'pending_update' if pending else 'current'
        return state,freshness

    def _require_current(self,db,ctx,project,manifest):
        state=self._current(db,ctx,project,manifest)
        if state!='resolved':
            raise APIError(409,'proposal_stale' if state=='stale' else 'prerequisites_unverifiable' if state=='blocked' else 'candidate_domain_unusable')

    def _release(self,db,ctx,project,id):
        row=self._release_row(db,ctx,project,id);state,freshness=self._read_state(db,ctx,project,row)
        return dict(id=id,project_id=project,revision=row['revision'],state_version=row['state_version'],state=row['state'],
            manifest=json.loads(row['data']),manifest_digest=row['manifest_digest'],freshness=freshness,current_use_state=state,published_at=row['published_at'])

    def candidate(self,db,ctx,project,id):
        row=self._release_row(db,ctx,project,id);self._access(db,ctx,project,json.loads(row['data']))
        if row['state']!='building': raise APIError(409,'candidate_not_writable')
        return dict(revision=row['revision'],state=row['state'],manifest_digest=row['manifest_digest'])

    def adopt(self,db,ctx,project,id,domain):
        self.candidate(db,ctx,project,id);row=self._release_row(db,ctx,project,id);manifest=json.loads(row['data'])
        if self._current(db,ctx,project,manifest,False)!='resolved': raise APIError(409,'proposal_stale')
        manifest['domains'].append(dict(resource_type='candidate_reference_definition',resource_id=domain['id'],revision=domain['revision'],
            content_hash=domain['content_hash'],proposal_id=domain['proposal_id'],proposal_revision=domain['proposal_revision'],approval_id=domain['approval_id']))
        db.execute('UPDATE ph4_releases SET revision=revision+1,state_version=state_version+1,data=?,manifest_digest=? WHERE tenant=? AND project=? AND id=?',
            (canonical(manifest),digest(manifest),ctx['tenant'],project,id))

    def release_for_approval(self,db,ctx,project,id):
        row=self._release_row(db,ctx,project,id);manifest=json.loads(row['data']);self._access(db,ctx,project,manifest)
        if row['state']!='ready': raise APIError(409,'release_not_ready')
        self._require_current(db,ctx,project,manifest)
        self._verify_index(db,ctx,project,manifest)
        return dict(revision=row['revision'],manifest_digest=row['manifest_digest'],head_revision=manifest['expected_head_revision'])

    def create(self,db,ctx,project,body,trigger='manual',schedule=None,scheduled_for=None):
        self.gov.project_guard(db,ctx,project,['owner','proposer'])
        self._current_times(body)
        self._capacity(db,ctx['tenant'],'ph4_runs',256)
        busy=db.execute("SELECT id FROM ph4_runs WHERE tenant=? AND project=? AND (state IN ('queued','running') OR (state='cancel_requested' AND lease>?)) ORDER BY rowid LIMIT 1",(ctx['tenant'],project,self.clock())).fetchone()
        if busy and trigger=='manual': raise APIError(409,'resource_busy')
        basis=self.gov._basis(db,ctx,project,body.get('effective_at'),body.get('known_at'))
        head=self._head_row(db,ctx,project)
        if 'base_release_id' in body and body['base_release_id']!=head['release_id']: raise APIError(409,'base_release_changed')
        for ref in basis['inputs']:
            b=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,ref['resource_id'])).fetchone()
            if b['state']!='active' or ctx['actor'] not in json.loads(b['readers']): raise APIError(409,'prerequisites_unverifiable')
        run,release=uid(),uid();watermark=digest(basis['inputs'])
        manifest=dict(schema_version='library-release-v1',release_id=release,run_id=run,basis=basis,profile=PROFILE,index_generation_id=None,index_hash=None,
            coverage=dict(items=[],required_complete=False,source_coverage='project_bindings_snapshot',optional_exclusions=0),domains=[],
            base_release_id=head['release_id'],expected_head_revision=head['revision'],captured_watermark=watermark,publication_intent='activate_verified_library')
        value=dict(id=run,project_id=project,release_id=release,state='queued',state_version=1,epoch=0,attempt=0,lease_until=0,
            plan_version=PLAN,profile=PROFILE,trigger=trigger,schedule_id=schedule['id'] if schedule else None,schedule_revision=schedule['revision'] if schedule else None,
            scheduled_for=scheduled_for,captured_watermark=watermark,started_at=None,finished_at=None,blocker='resource_busy' if busy else None,
            counts={s:0 for s in COUNTS['properties']},cancel_effect='none',processing_stop='not_started')
        db.execute('INSERT INTO ph4_releases VALUES(?,?,?,1,1,?,?,?,NULL)',(ctx['tenant'],project,release,'building',canonical(manifest),digest(manifest)))
        db.execute('INSERT INTO ph4_runs VALUES(?,?,?,?,?,?,?,1,0,0,0,?)',(ctx['tenant'],project,run,release,ctx['actor'],ctx['revision'],'queued',canonical(value)))
        for b in self.gov._bindings(db,ctx,project):
            item=dict(id=uid(),source=b['id'],source_version=b['version'],source_hash=b['sha'],required=bool(b['required']),profile=PROFILE,state='queued',attempt=0,
                checkpoint='pending',error=None,result_hash=None,reused=False)
            key=digest(dict(asset=b['id'],source_version=b['version'],source_hash=b['sha'],profile=PROFILE,chunk_policy=CHUNK_POLICY))
            db.execute('INSERT INTO ph4_run_items VALUES(?,?,?,?,?,?)',(ctx['tenant'],project,run,item['id'],key,canonical(item)))
        self._event(db,ctx,project,run,'accepted')
        return self._run(db,ctx,project,run)

    def _principal_ctx(self,row):
        with self.auth.lock:
            principals=[p for p in self.auth.principals.values() if p['tenant']==row['tenant'] and p['actor']==row['actor'] and p['revision']==row['auth_revision'] and
                'write' in p['scopes'] and p.get('active',True) and p.get('expires_at',float('inf'))>time.time()]
        if not principals: raise APIError(403,'permission_revoked')
        return {**principals[0],'scopes':list(principals[0]['scopes'])}

    def claim(self,lease_seconds=30):
        now=self.clock()
        with self.store.tx() as db:
            for cancelled in db.execute("SELECT * FROM ph4_runs WHERE state='cancel_requested' AND lease<=?",(now,)).fetchall():
                # Expiry ends logical cancellation; it does not prove that a
                # vanished process physically stopped. Publication is fenced.
                db.execute("UPDATE ph4_runs SET state='cancelled',state_version=state_version+1,lease=0 WHERE tenant=? AND project=? AND id=?",(cancelled['tenant'],cancelled['project'],cancelled['id']))
                self._event(db,dict(tenant=cancelled['tenant']),cancelled['project'],cancelled['id'],'cancellation_lease_expired',True)
            rows=db.execute("SELECT * FROM ph4_runs WHERE state='queued' OR (state='running' AND lease<=?) ORDER BY rowid",(now,)).fetchall()
            for row in rows:
                if db.execute("SELECT 1 FROM ph4_runs WHERE tenant=? AND project=? AND id!=? AND state IN ('running','cancel_requested') AND lease>?",(row['tenant'],row['project'],row['id'],now)).fetchone(): continue
                try:
                    ctx=self._principal_ctx(row);self.gov.project_guard(db,ctx,row['project'],['owner','proposer'])
                    manifest=json.loads(db.execute('SELECT data FROM ph4_releases WHERE tenant=? AND project=? AND id=?',(row['tenant'],row['project'],row['release_id'])).fetchone()[0])
                    self._access(db,ctx,row['project'],manifest)
                except APIError:
                    db.execute("UPDATE ph4_runs SET state='blocked_permission',state_version=state_version+1,epoch=epoch+1,lease=0 WHERE tenant=? AND project=? AND id=?",(row['tenant'],row['project'],row['id']))
                    self._event(db,dict(tenant=row['tenant']),row['project'],row['id'],'blocked_permission',True)
                    continue
                value=json.loads(row['data'])
                if value['plan_version']!=PLAN: raise APIError(409,'resume_incompatible')
                value['started_at']=value['started_at'] or now;value['blocker']=None;value['processing_stop']='not_confirmed'
                db.execute("UPDATE ph4_runs SET state='running',state_version=state_version+1,epoch=epoch+1,attempt=attempt+1,lease=?,data=? WHERE tenant=? AND project=? AND id=?",
                    (now+lease_seconds,canonical(value),row['tenant'],row['project'],row['id']))
                self._event(db,ctx,row['project'],row['id'],'claimed')
                return dict(ctx=ctx,project_id=row['project'],run_id=row['id'],epoch=row['epoch']+1,attempt=row['attempt']+1,lease_seconds=lease_seconds)
        return None

    def _fence(self,db,claim):
        row=self._row(db,claim['ctx'],claim['project_id'],'ph4_runs',claim['run_id'])
        if row['state']!='running' or row['epoch']!=claim['epoch'] or row['attempt']!=claim['attempt'] or row['lease']<=self.clock(): raise APIError(409,'worker_fenced')
        self._principal_ctx(row)
        self.gov.project_guard(db,claim['ctx'],claim['project_id'],['owner','proposer'])
        manifest=json.loads(self._release_row(db,claim['ctx'],claim['project_id'],row['release_id'])['data'])
        self._access(db,claim['ctx'],claim['project_id'],manifest)
        for ref in manifest['basis']['inputs']:
            worker_gate=getattr(self.store,'worker_access',None)
            if worker_gate is not None and not worker_gate(claim['ctx']['tenant'],claim['ctx']['actor'],ref['resource_id']):
                raise APIError(403,'permission_revoked')
        return row

    def _item_chunks(self,db,ctx,project,item):
        b=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,item['source'])).fetchone()
        if b is None or b['state']!='active' or ctx['actor'] not in json.loads(b['readers']): raise APIError(403,'permission_revoked')
        raw,sha=self.library.original(db,ctx['tenant'],item['source'],item['source_version'])
        if sha!=item['source_hash']: raise APIError(409,'source_integrity_failed')
        meta=json.loads(db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',(ctx['tenant'],item['source'],item['source_version'])).fetchone()[0])
        job=self.store.job(db,ctx['tenant'],meta['job_id'])
        if job['state']!='succeeded' or job['asset_id']!=item['source'] or job['source_version']!=item['source_version']:
            raise APIError(409,'source_analysis_unavailable')
        self.library.verify_job(db,ctx['tenant'],job)
        rows=db.execute('SELECT * FROM segments WHERE tenant=? AND asset=? AND version=? ORDER BY id',(ctx['tenant'],item['source'],item['source_version'])).fetchall()
        if not rows: raise APIError(409,'source_analysis_unavailable')
        if {r['id'] for r in rows}!={s['id'] for s in job['result']['segments']}:
            raise APIError(409,'analysis_integrity_failed')
        result=[]
        for row in rows:
            ref=dict(asset_id=row['asset'],source_version=row['version'],analysis_version=row['analysis'],segment_id=row['id'])
            segment=self.library.resolve(db,ctx['tenant'],ref)
            for start,end,text in chunks(segment['quote']):
                result.append(dict(evidence=ref,source_hash=sha,start=start,end=end,text=text,counts=dict(term_counts(text)),length=sum(term_counts(text).values())))
        if len(result)>MAX_CHUNKS: raise APIError(413,'text_retrieval_chunk_limit')
        return result

    def process_item(self,claim):
        with self.store.tx() as db:
            row=self._fence(db,claim);ctx,project,run=claim['ctx'],claim['project_id'],claim['run_id']
            pending=db.execute('SELECT * FROM ph4_run_items WHERE tenant=? AND project=? AND run=? ORDER BY id',(ctx['tenant'],project,run)).fetchall()
            pending=[r for r in pending if json.loads(r['data'])['state'] in ('queued','running')]
            if not pending: return False
            itemrow=pending[0];item=json.loads(itemrow['data']);item['attempt']+=1;item['state']='running'
            try:
                data=self._item_chunks(db,ctx,project,item)
                cached=db.execute('SELECT * FROM ph4_item_results WHERE tenant=? AND item_key=?',(ctx['tenant'],itemrow['item_key'])).fetchone()
                if cached:
                    if digest(json.loads(cached['data']))!=cached['result_hash'] or canonical(data)!=cached['data']: raise APIError(409,'index_integrity_failed')
                    item['reused']=True
                else:
                    self._capacity(db,ctx['tenant'],'ph4_item_results',512)
                    db.execute('INSERT INTO ph4_item_results VALUES(?,?,?,?)',(ctx['tenant'],itemrow['item_key'],digest(data),canonical(data)))
                item.update(state='succeeded',checkpoint='indexed',error=None,result_hash=digest(data))
            except APIError as error:
                item.update(state='failed',error=error.code,checkpoint='pending',result_hash=None)
            # Native parsing and integrity checks may outlast the lease or
            # coincide with registry revocation. This fence is deliberately
            # outside the item-error catch: failure rolls back cached results,
            # checkpoints and events together instead of recording success.
            self._fence(db,claim)
            db.execute('UPDATE ph4_run_items SET data=? WHERE tenant=? AND project=? AND run=? AND id=?',(canonical(item),ctx['tenant'],project,run,item['id']))
            db.execute('UPDATE ph4_runs SET state_version=state_version+1,lease=? WHERE tenant=? AND project=? AND id=?',(self.clock()+claim.get('lease_seconds',30),ctx['tenant'],project,run))
            self._event(db,ctx,project,run,'item_'+item['state'])
            return True

    def finish(self,claim):
        with self.store.tx() as db:
            row=self._fence(db,claim);ctx,project,run=claim['ctx'],claim['project_id'],claim['run_id'];release=self._release_row(db,ctx,project,row['release_id']);manifest=json.loads(release['data'])
            items=self._items(db,ctx,project,run)
            if any(i['state'] in ('queued','running') for i in items): raise APIError(409,'items_pending')
            coverage=dict(items=[dict(item_id=i['id'],source_id=i['source'],source_version=i['source_version'],source_hash=i['source_hash'],required=i['required'],
                state=i['state'],result_hash=i['result_hash'],exclusion_reason=i['error']) for i in items],required_complete=bool(items) and all(not i['required'] or i['state']=='succeeded' for i in items),
                source_coverage='project_bindings_snapshot',optional_exclusions=sum(not i['required'] and i['state']!='succeeded' for i in items))
            manifest['coverage']=coverage
            indexed=[]
            for r in db.execute('SELECT * FROM ph4_run_items WHERE tenant=? AND project=? AND run=? ORDER BY id',(ctx['tenant'],project,run)):
                item=json.loads(r['data'])
                if item['state']=='succeeded':
                    result=db.execute('SELECT * FROM ph4_item_results WHERE tenant=? AND item_key=?',(ctx['tenant'],r['item_key'])).fetchone()
                    if result is None or result['result_hash']!=item['result_hash'] or digest(json.loads(result['data']))!=item['result_hash']: raise APIError(409,'index_integrity_failed')
                    indexed.extend(json.loads(result['data']))
            if len(indexed)>MAX_CHUNKS: raise APIError(413,'text_retrieval_chunk_limit')
            generation=uid();artifact=digest(indexed)
            self._capacity(db,ctx['tenant'],'ph4_index_generations',512)
            db.execute('INSERT INTO ph4_index_generations VALUES(?,?,?,?,?,?,?)',(ctx['tenant'],project,generation,release['id'],PROFILE,artifact,len(indexed)))
            for n,chunk in enumerate(indexed): db.execute('INSERT INTO ph4_index_chunks VALUES(?,?,?,?,?)',(ctx['tenant'],project,generation,n,canonical(chunk)))
            manifest.update(index_generation_id=generation,index_hash=artifact)
            state='completed_with_errors' if any(i['state']!='succeeded' for i in items) else 'succeeded'
            release_state='building' if coverage['required_complete'] else 'failed'
            if self._current(db,ctx,project,manifest,False)!='resolved': release_state='stale'
            # Generation rows were staged in this transaction, so an expired
            # or revoked worker leaves no artifact or terminal metadata behind.
            self._fence(db,claim)
            db.execute('UPDATE ph4_releases SET revision=revision+1,state_version=state_version+1,state=?,data=?,manifest_digest=? WHERE tenant=? AND project=? AND id=?',(release_state,canonical(manifest),digest(manifest),ctx['tenant'],project,release['id']))
            value=json.loads(row['data']);value['finished_at']=self.clock();value['processing_stop']='observed_stopped'
            db.execute('UPDATE ph4_runs SET state=?,state_version=state_version+1,lease=0,data=? WHERE tenant=? AND project=? AND id=?',(state,canonical(value),ctx['tenant'],project,run))
            self._event(db,ctx,project,run,'completed',True)
            return self._run(db,ctx,project,run)

    def run_once(self):
        claim=self.claim()
        if claim is None: return False
        try:
            while self.process_item(claim): pass
            self.finish(claim)
        except APIError as error:
            if error.code=='worker_fenced':
                self.ack_cancel(claim)
                return True
            with self.store.tx() as db:
                row=db.execute('SELECT * FROM ph4_runs WHERE tenant=? AND project=? AND id=?',(claim['ctx']['tenant'],claim['project_id'],claim['run_id'])).fetchone()
                if row['state']=='running' and row['epoch']==claim['epoch']:
                    value=json.loads(row['data']);value.update(blocker=error.code,finished_at=self.clock())
                    db.execute("UPDATE ph4_runs SET state=?,state_version=state_version+1,epoch=epoch+1,lease=0,data=? WHERE tenant=? AND project=? AND id=?",('blocked_permission' if error.status in (403,404,410) else 'failed',canonical(value),row['tenant'],row['project'],row['id']))
                    self._event(db,claim['ctx'],row['project'],row['id'],'worker_blocked',True)
        return True

    def _verify_index(self,db,ctx,project,manifest,expected_current=True):
        if expected_current and digest(manifest)!=self._release_row(db,ctx,project,manifest['release_id'])['manifest_digest']: raise APIError(409,'manifest_integrity_failed')
        generation=db.execute('SELECT * FROM ph4_index_generations WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,manifest['index_generation_id'])).fetchone()
        if generation is None or generation['release_id']!=manifest['release_id'] or generation['profile']!=PROFILE: raise APIError(409,'index_integrity_failed')
        indexed=[json.loads(r[0]) for r in db.execute('SELECT data FROM ph4_index_chunks WHERE tenant=? AND project=? AND generation=? ORDER BY id',(ctx['tenant'],project,generation['id']))]
        if len(indexed)!=generation['chunk_count'] or digest(indexed)!=generation['artifact_hash'] or generation['artifact_hash']!=manifest['index_hash']: raise APIError(409,'index_integrity_failed')
        expected=[]
        for item in manifest['coverage']['items']:
            if item['state']=='succeeded':
                expected.extend(self._item_chunks(db,ctx,project,dict(source=item['source_id'],source_version=item['source_version'],source_hash=item['source_hash'])))
        if canonical(expected)!=canonical(indexed): raise APIError(409,'index_integrity_failed')
        for ref in manifest['domains']:
            row=db.execute('SELECT data FROM ph4_domain_versions WHERE tenant=? AND project=? AND id=? AND revision=?',(ctx['tenant'],project,ref['resource_id'],ref['revision'])).fetchone()
            if row is None: raise APIError(409,'domain_integrity_failed')
            domain=json.loads(row[0])
            if digest(domain['payload'])!=ref['content_hash'] or domain['target_release_id']!=manifest['release_id'] or domain['proposal_id']!=ref['proposal_id'] or domain['approval_id']!=ref['approval_id']: raise APIError(409,'domain_integrity_failed')
            self.gov.verify_response(db,ctx,domain,P+'/proposals/{id}/apply')
        return indexed

    def head(self,db,ctx,project):
        head=self._head_row(db,ctx,project);state,freshness='blocked','current'
        if head['release_id']:
            row=self._release_row(db,ctx,project,head['release_id']);state,freshness=self._read_state(db,ctx,project,row)
        return dict(project_id=project,release_id=head['release_id'],head_revision=head['revision'],state_version=head['revision']+1,
            published_watermark=head['watermark'],freshness=freshness,current_use_state=state)

    def _schedule(self,db,ctx,project,id): return json.loads(self._row(db,ctx,project,'ph4_schedules',id)['data'])

    def _binding(self,db,ctx,project,id):
        value=json.loads(self._row(db,ctx,project,'ph4_read_bindings',id)['data']);row=self._release_row(db,ctx,project,value['release_id'])
        self._access(db,ctx,project,json.loads(row['data']))
        if value['manifest_digest']!=row['manifest_digest']: raise APIError(409,'manifest_integrity_failed')
        return value

    def query(self,db,ctx,project,body):
        if body['mode']=='operational': raise APIError(501,'operational_authority_unavailable')
        binding=self._binding(db,ctx,project,body['binding_id']);row=self._release_row(db,ctx,project,binding['release_id']);manifest=json.loads(row['data'])
        state,_=self._read_state(db,ctx,project,row)
        result=dict(project_id=project,binding=binding,mode=body['mode'],results=[],status='unsupported',blockers=[],model_used=None,
            score_meaning='BM25 relevance; not factual confidence',state_version=1)
        if body['mode']=='operational' and state!='resolved': result.update(status='blocked',blockers=['prerequisites_unverifiable']);return result
        indexed=self._verify_index(db,ctx,project,manifest)
        # Persisted corpus counts are used directly; no encode/model/index surrogate.
        q=term_counts(body['query']);n=len(indexed);avg=sum(c['length'] for c in indexed)/max(1,n) or 1
        dfs={t:sum(t in c['counts'] for c in indexed) for t in q};best={}
        for c in indexed:
            score=sum(math.log(1+(n-dfs[t]+0.5)/(dfs[t]+0.5))*c['counts'].get(t,0)*2.2/(c['counts'].get(t,0)+1.2*(0.25+0.75*c['length']/avg)) for t in q if c['counts'].get(t,0))
            if score>0:
                key=canonical(c['evidence']);best[key]=max(score,best.get(key,0))
        for key,score in sorted(best.items(),key=lambda p:(-p[1],p[0]))[:body.get('limit',5)]:
            ref=json.loads(key);segment=self.library.resolve(db,ctx['tenant'],ref)
            result['results'].append(dict(evidence=ref,segment=segment,score=score))
        if result['results']: result['status']='supported'
        if state!='resolved': result['blockers']=['prerequisites_unverifiable']
        return result

    def handle(self,db,ctx,route,params,body,if_match=None):
        project=params['project_id'];self.gov.project_guard(db,ctx,project)
        schema=next((s for _,p,s,*_ in MAINTENANCE_OPERATIONS if p==route and s),None)
        if body is not None and schema: validate(body,MAINTENANCE_SCHEMAS[schema])
        status=200;assets=[];headers={};id=params.get('id')
        if route.endswith('/maintenance-runs'):
            value=self.create(db,ctx,project,body);status=202;headers['Location']='/v1/projects/'+project+'/runs/'+value['id']
        elif '/runs/' in route:
            value=self._run(db,ctx,project,id);release=self._release_row(db,ctx,project,value['release_id']);manifest=json.loads(release['data'])
            if route.endswith('/items'): value=dict(project_id=project,run_id=id,items=self._items(db,ctx,project,id),state_version=value['state_version'])
            elif route.endswith('/event-queries'):
                events=[json.loads(r[0]) for r in db.execute('SELECT data FROM ph4_run_events WHERE tenant=? AND project=? AND run=? AND seq>? ORDER BY seq LIMIT ?',(ctx['tenant'],project,id,body['after'],body.get('limit',100)))]
                maximum=db.execute('SELECT coalesce(max(seq),0) FROM ph4_run_events WHERE tenant=? AND project=? AND run=?',(ctx['tenant'],project,id)).fetchone()[0]
                if body['after']>maximum: raise APIError(422,'invalid_cursor')
                value=dict(project_id=project,run_id=id,events=events,cursor=events[-1]['seq'] if events else body['after'],state_version=value['state_version'])
            elif route.endswith(('/cancel','/resume','/retry-failed-items')):
                self.gov.project_guard(db,ctx,project,['owner','reviewer','proposer']);self._match(value,if_match)
                if route.endswith('/cancel'):
                    if release['state']=='published': value['cancel_effect']='already_committed'
                    else:
                        if value['state'] not in ('cancelled','cancel_requested'):
                            for item in self._items(db,ctx,project,id):
                                if item['state'] in ('queued','running'): item['state']='cancelled';db.execute('UPDATE ph4_run_items SET data=? WHERE tenant=? AND project=? AND run=? AND id=?',(canonical(item),ctx['tenant'],project,id,item['id']))
                            next_state='cancel_requested' if value['state']=='running' else 'cancelled'
                            value.update(cancel_effect='publication_fenced',finished_at=self.clock())
                            db.execute('UPDATE ph4_runs SET state=?,state_version=state_version+1,epoch=epoch+1,lease=?,data=? WHERE tenant=? AND project=? AND id=?',(next_state,value['lease_until'] if next_state=='cancel_requested' else 0,canonical(value),ctx['tenant'],project,id))
                            db.execute("UPDATE ph4_releases SET state='withdrawn',state_version=state_version+1 WHERE tenant=? AND project=? AND id=?",(ctx['tenant'],project,release['id']))
                            self._event(db,ctx,project,id,next_state,next_state=='cancelled')
                            for occurrence in db.execute('SELECT * FROM ph4_occurrences WHERE tenant=? AND project=?',(ctx['tenant'],project)).fetchall():
                                saved=json.loads(occurrence['data'])
                                if saved['run_id']==id:
                                    saved['state']='cancelled';saved['reason']='run_cancelled'
                                    db.execute('UPDATE ph4_occurrences SET data=? WHERE tenant=? AND project=? AND schedule=? AND scheduled_for=?',(canonical(saved),ctx['tenant'],project,occurrence['schedule'],occurrence['scheduled_for']))
                        value=self._run(db,ctx,project,id)
                else:
                    if value['plan_version']!=PLAN: raise APIError(409,'resume_incompatible')
                    if release['state'] not in ('building','failed'): raise APIError(409,'resume_not_allowed')
                    if value['state'] not in ('failed','completed_with_errors','blocked_permission') and not (value['state']=='running' and value['lease_until']<=self.clock()): raise APIError(409,'resume_not_allowed')
                    if self._current(db,ctx,project,manifest,False)!='resolved': raise APIError(409,'proposal_stale')
                    for item in self._items(db,ctx,project,id):
                        if item['state'] in ('failed','running'): item.update(state='queued',error=None);db.execute('UPDATE ph4_run_items SET data=? WHERE tenant=? AND project=? AND run=? AND id=?',(canonical(item),ctx['tenant'],project,id,item['id']))
                    value['finished_at']=None;value['blocker']=None
                    db.execute("UPDATE ph4_runs SET state='queued',state_version=state_version+1,epoch=epoch+1,lease=0,data=? WHERE tenant=? AND project=? AND id=?",(canonical(value),ctx['tenant'],project,id))
                    db.execute("UPDATE ph4_releases SET state='building',state_version=state_version+1 WHERE tenant=? AND project=? AND id=?",(ctx['tenant'],project,release['id']))
                    self._event(db,ctx,project,id,'resumed');value=self._run(db,ctx,project,id);status=202
            assets=[r['resource_id'] for r in manifest['basis']['inputs']]
        elif route.endswith('/library-head'): value=self.head(db,ctx,project)
        elif '/library-releases/' in route:
            value=self._release(db,ctx,project,id);manifest=value['manifest'];assets=[r['resource_id'] for r in manifest['basis']['inputs']]
            if route.endswith('/validate'):
                self.gov.project_guard(db,ctx,project,['owner','reviewer','proposer']);self._match(value,if_match)
                if value['state']!='building' or not manifest['coverage']['required_complete']: raise APIError(409,'release_not_ready')
                self._require_current(db,ctx,project,manifest)
                self._verify_index(db,ctx,project,manifest)
                db.execute("UPDATE ph4_releases SET state='ready',state_version=state_version+1 WHERE tenant=? AND project=? AND id=?",(ctx['tenant'],project,id));value=self._release(db,ctx,project,id)
            elif route.endswith('/publish'):
                self.gov.project_guard(db,ctx,project,['owner','reviewer'],True)
                head=self.head(db,ctx,project);self._match(head,if_match)
                self.release_for_approval(db,ctx,project,id)
                if body['expected_head_revision']!=head['head_revision'] or manifest['expected_head_revision']!=head['head_revision'] or body['manifest_digest']!=value['manifest_digest']: raise APIError(409,'head_changed')
                run=self._run(db,ctx,project,manifest['run_id'])
                if run['state'] not in ('succeeded','completed_with_errors'): raise APIError(409,'run_not_complete')
                self.gov.admit_publish(db,ctx,project,body['proposal_id'],body['approval_id'],id,value['manifest_digest'],head['head_revision'])
                self.gov.consume_approval(db,ctx,project,body['approval_id']);self.gov.mark_published(db,ctx,project,body['proposal_id'])
                db.execute('INSERT INTO ph4_heads VALUES(?,?,?,?,?) ON CONFLICT(tenant,project) DO UPDATE SET release_id=excluded.release_id,revision=excluded.revision,watermark=excluded.watermark',
                    (ctx['tenant'],project,id,head['head_revision']+1,manifest['captured_watermark']))
                db.execute("UPDATE ph4_releases SET state='published',state_version=state_version+1,published_at=? WHERE tenant=? AND project=? AND id=?",(self.clock(),ctx['tenant'],project,id))
                self.gov._epoch(db,ctx,project,'library_release_published');self._event(db,ctx,project,manifest['run_id'],'published',True)
                value=self._release(db,ctx,project,id)
        elif '/maintenance-schedules' in route:
            if route.endswith('/maintenance-schedules'):
                self.gov.project_guard(db,ctx,project,['owner'],True)
                self._capacity(db,ctx['tenant'],'ph4_schedules',100)
                value=dict(id=uid(),project_id=project,revision=1,state_version=1,actor=ctx['actor'],auth_revision=ctx['revision'],definition=body,implementation='definition_and_occurrence_ledger_only')
                db.execute('INSERT INTO ph4_schedules VALUES(?,?,?,1,1,?)',(ctx['tenant'],project,value['id'],canonical(value)))
                db.execute('INSERT INTO ph4_schedule_versions VALUES(?,?,?,1,?)',(ctx['tenant'],project,value['id'],canonical(value)));status=201
            else:
                value=self._schedule(db,ctx,project,id)
                if route.endswith('/revisions'):
                    self.gov.project_guard(db,ctx,project,['owner'],True);self._match(value,if_match);value.update(revision=value['revision']+1,state_version=value['state_version']+1,definition=body)
                    self._capacity(db,ctx['tenant'],'ph4_schedule_versions',4096)
                    db.execute('UPDATE ph4_schedules SET revision=?,state_version=?,data=? WHERE tenant=? AND project=? AND id=?',(value['revision'],value['state_version'],canonical(value),ctx['tenant'],project,id))
                    db.execute('INSERT INTO ph4_schedule_versions VALUES(?,?,?,?,?)',(ctx['tenant'],project,id,value['revision'],canonical(value)))
                elif route.endswith('/occurrences'):
                    self.gov.project_guard(db,ctx,project,['owner','reviewer','proposer'])
                    existing=db.execute('SELECT data FROM ph4_occurrences WHERE tenant=? AND project=? AND schedule=? AND scheduled_for=?',(ctx['tenant'],project,id,body['scheduled_for'])).fetchone()
                    if existing: value=json.loads(existing[0]);status=202
                    else:
                        self._capacity(db,ctx['tenant'],'ph4_occurrences',512)
                        if not value['definition']['active']: raise APIError(409,'schedule_paused')
                        if ctx['actor']!=value['actor'] or ctx['revision']!=value['auth_revision']: raise APIError(403,'schedule_actor_required')
                        if body['scheduled_for']>self.clock(): raise APIError(422,'occurrence_not_due')
                        late=self.clock()-body['scheduled_for']>value['definition']['allowed_lateness_seconds']
                        backlog=db.execute("SELECT count(*) FROM ph4_runs WHERE tenant=? AND project=? AND state='queued'",(ctx['tenant'],project)).fetchone()[0]
                        if not late and backlog>=value['definition']['max_backlog']: raise APIError(409,'blocked_backlog')
                        run=None if late else self.create(db,ctx,project,dict(profile=PROFILE),trigger='schedule',schedule=value,scheduled_for=body['scheduled_for'])
                        value=dict(project_id=project,schedule_id=id,schedule_revision=value['revision'],scheduled_for=body['scheduled_for'],run_id=run['id'] if run else None,state='skipped_late' if late else 'accepted',reason='allowed_lateness_exceeded' if late else None)
                        db.execute('INSERT INTO ph4_occurrences VALUES(?,?,?,?,?)',(ctx['tenant'],project,id,body['scheduled_for'],canonical(value)));status=202
        elif '/read-bindings' in route:
            if route.endswith('/read-bindings'):
                self._current_times(body)
                self._capacity(db,ctx['tenant'],'ph4_read_bindings',512)
                head=self._head_row(db,ctx,project)
                if not head['release_id']: raise APIError(409,'library_not_published')
                release=self._release(db,ctx,project,head['release_id']);basis=release['manifest']['basis']
                value=dict(id=uid(),project_id=project,release_id=head['release_id'],head_revision=head['revision'],effective_at=body.get('effective_at',self.clock()),known_at=body.get('known_at',self.clock()),
                    context_revision=basis['context_revision'],business_policy_revision=basis['policy_revision'],manifest_digest=release['manifest_digest'],state_version=1)
                db.execute('INSERT INTO ph4_read_bindings VALUES(?,?,?,?)',(ctx['tenant'],project,value['id'],canonical(value)));status=201
            else: value=self._binding(db,ctx,project,id)
        elif route.endswith('/release-queries'): value=self.query(db,ctx,project,body)
        else: raise APIError(404,'not_found')
        if route.endswith('/maintenance-runs'):
            assets=[ref['resource_id'] for ref in json.loads(self._release_row(db,ctx,project,value['release_id'])['data'])['basis']['inputs']]
        if 'state_version' in value: headers['ETag']=etag(value)
        return status,value,assets,headers

    def verify_response(self,db,ctx,value,route,receipt_replay=False):
        project=value.get('project_id')
        if project: self.gov.project_guard(db,ctx,project)
        release_id=value.get('release_id')
        if 'manifest' in value:
            manifest=value['manifest'];self._access(db,ctx,project,manifest)
            if digest(manifest)!=value['manifest_digest']: raise APIError(409,'manifest_integrity_failed')
            if manifest['index_generation_id']: self._verify_index(db,ctx,project,manifest,expected_current=not receipt_replay)
        elif release_id:
            row=self._release_row(db,ctx,project,release_id);self._access(db,ctx,project,json.loads(row['data']))
        elif value.get('run_id'):
            self._run(db,ctx,project,value['run_id'])
        if 'binding' in value: self._binding(db,ctx,project,value['binding']['id'])
        if value.get('schedule_id'):
            self._schedule(db,ctx,project or db.execute('SELECT project FROM ph4_schedules WHERE tenant=? AND id=?',(ctx['tenant'],value['schedule_id'])).fetchone()[0],value['schedule_id'])
        return value

    def activation_epoch(self,db,ctx,project,release_id):
        row=self._release_row(db,ctx,project,release_id)
        return json.loads(row['data'])['basis']['scope_epoch']+1 if row['state']=='published' else None

    def published_reference_state(self,db,ctx,project):
        """Governance's live projection uses only domains in the current head."""
        head=self._head_row(db,ctx,project)
        if head['release_id'] is None: return None
        row=self._release_row(db,ctx,project,head['release_id'])
        if row['state']!='published': raise APIError(409,'head_integrity_failed')
        manifest=json.loads(row['data']);self._access(db,ctx,project,manifest)
        self._verify_index(db,ctx,project,manifest)
        state,_=self._read_state(db,ctx,project,row)
        return dict(release_id=head['release_id'],domains=manifest['domains'],current_use_state=state)

    def ack_cancel(self,claim):
        """An owned worker reports observing its fence, without undoing results."""
        with self.store.tx() as db:
            row=db.execute('SELECT * FROM ph4_runs WHERE tenant=? AND project=? AND id=?',(claim['ctx']['tenant'],claim['project_id'],claim['run_id'])).fetchone()
            if row and row['state']=='cancel_requested' and row['attempt']==claim['attempt'] and row['epoch']==claim['epoch']+1:
                value=json.loads(row['data']);value['processing_stop']='observed_stopped'
                db.execute("UPDATE ph4_runs SET state='cancelled',state_version=state_version+1,lease=0,data=? WHERE tenant=? AND project=? AND id=?",(canonical(value),row['tenant'],row['project'],row['id']))
                self._event(db,claim['ctx'],row['project'],row['id'],'cancelled',True)

    def on_delete(self,db,tenant,asset):
        """Called in the original deletion transaction; immediately fence workers."""
        for row in db.execute("SELECT * FROM ph4_runs WHERE tenant=? AND state IN ('queued','running','blocked_permission','failed','completed_with_errors')",(tenant,)).fetchall():
            manifest=json.loads(db.execute('SELECT data FROM ph4_releases WHERE tenant=? AND project=? AND id=?',(tenant,row['project'],row['release_id'])).fetchone()[0])
            if not any(ref['resource_id']==asset for ref in manifest['basis']['inputs']): continue
            value=json.loads(row['data']);value.update(cancel_effect='publication_fenced',blocker='source_deleted',finished_at=self.clock())
            next_state='cancel_requested' if row['state']=='running' else 'cancelled'
            db.execute('UPDATE ph4_runs SET state=?,epoch=epoch+1,state_version=state_version+1,lease=?,data=? WHERE tenant=? AND project=? AND id=?',(next_state,row['lease'] if next_state=='cancel_requested' else 0,canonical(value),tenant,row['project'],row['id']))
            for item in self._items(db,dict(tenant=tenant),row['project'],row['id']):
                if item['state'] in ('queued','running'): item['state']='cancelled';db.execute('UPDATE ph4_run_items SET data=? WHERE tenant=? AND project=? AND run=? AND id=?',(canonical(item),tenant,row['project'],row['id'],item['id']))
            db.execute("UPDATE ph4_releases SET state='withdrawn',state_version=state_version+1 WHERE tenant=? AND project=? AND id=? AND state!='published'",(tenant,row['project'],row['release_id']))
            self._event(db,dict(tenant=tenant),row['project'],row['id'],'source_deleted',True)
