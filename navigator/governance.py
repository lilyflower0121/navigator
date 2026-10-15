"""Local project review contract for one grounded native-PDF reference criterion.

This is an explicit curated-reference workflow, not issuer authority, arbitrary
JSON process generation, or a determination of site safety. All writes share the
caller's Store transaction; immutable content revisions and mutable ETags differ.
"""
import json
import time
from .contracts import APIError, ID, LIMITS, array, canonical, digest, integer, obj, string, validate
from .grounded import Grounded, CLAIM
from .store import uid

ENUM = lambda values: dict(type='string', enum=values)
NUMBER = dict(type='number', minimum=0, maximum=253402300799)
NULLTIME = dict(type=['number', 'null'], minimum=0, maximum=253402300799)
SCAFFOLDS = ['suspended', 'frame', 'tube', 'wedge']
PERIOD = obj(dict(start_kind=ENUM(['known', 'unknown']), start_at=NULLTIME,
    end_kind=ENUM(['known', 'unknown', 'open_end']), end_at=NULLTIME,
    timezone=ENUM(['UTC']), precision=ENUM(['second', 'date'])))
CONTEXT = obj(dict(scaffold_type=ENUM(SCAFFOLDS)), [])
CONDITIONS = obj(dict(scaffold_type=obj(dict(operator=ENUM(['eq']), value=ENUM(SCAFFOLDS)))))
SOURCE_REF = obj(dict(resource_type=ENUM(['source']), resource_id=ID,
    revision=integer(1,100), content_hash=string(64,64), binding_revision=integer(1)))
BASIS = obj(dict(scope_epoch=integer(1), context_revision=integer(1),
    policy_revision=integer(1), inputs=array(SOURCE_REF,100), effective_at=NUMBER,
    known_at=NUMBER, context_hash=string(64,64), policy_hash=string(64,64)))
PAYLOAD = obj(dict(schema_version=ENUM(['midrail-reference-v1']), claim=CLAIM,
    grounded_answer_id=ID, conditions=CONDITIONS, valid_time=PERIOD,
    assessment=ENUM(['curated_reference_criterion'])))
RELEASE_PAYLOAD = obj(dict(release_id=ID,manifest_digest=string(64,64),expected_head_revision=integer(0),publish_intent=ENUM(['publish_curated_reference_release'])))
OPTION_IDS=['source-criterion','publish-release']
OPTION = obj(dict(option_id=ENUM(OPTION_IDS), payload={'anyOf':[PAYLOAD,RELEASE_PAYLOAD]},
    payload_digest=string(64,64), reasons=array(string(300),10),
    source_diff=ENUM(['literal_source_values_preserved']),
    missing_conditions=array(ENUM(['scaffold_type','valid_time']),2),
    unresolved_conflicts=array(ID,20), differences=array(string(300),10)))
TARGET = obj(dict(resource_type=ENUM(['candidate_reference_definition','library_release']),
    resource_id=ID, revision=integer(1), manifest_digest=string(64,64)))
PROPOSAL = obj(dict(id=ID, project_id=ID, revision=integer(1), state_version=integer(1),
    kind=ENUM(['structure','library_release']), target=TARGET, basis=BASIS,
    basis_digest=string(64,64), options=array(OPTION,1), recommended_option_id=ENUM(OPTION_IDS),
    blockers=array(string(100),10), expires_at=NUMBER, state=ENUM(['ready','needs_input','approved','applied','rejected','stale','expired']),
    validation_state=ENUM(['current','stale']), current_use_state=ENUM(['resolved','needs_input','conflicted','blocked','stale','expired']),
    selected_option_id=dict(type=['string','null']), approval_id=dict(type=['string','null']),
    recorded_at=NUMBER, recorded_sequence=integer(1), reevaluate_at=NULLTIME,
    notice=string(500)))
PROJECT = obj(dict(id=ID, tenant_id=string(100), name=string(160), scope_epoch=integer(1),
    context_revision=integer(1), policy_revision=integer(1), state_version=integer(1)))
REVIEW = obj(dict(id=ID, project_id=ID, proposal_id=ID, proposal_revision=integer(1),
    action=ENUM(['select','reject','request_changes']), option_id=ENUM(OPTION_IDS),
    reason=string(500), actor=string(100), recorded_at=NUMBER, state_version=integer(1)))
APPROVAL = obj(dict(id=ID, project_id=ID, proposal_id=ID, proposal_revision=integer(1),
    option_id=ENUM(OPTION_IDS), payload_digest=string(64,64), basis_digest=string(64,64),
    target=TARGET, actor=string(100), actor_type=ENUM(['human']), role=ENUM(['owner','reviewer']),
    recorded_at=NUMBER, expires_at=NUMBER, consumed=dict(type='boolean'), state_version=integer(1)))
DOMAIN = obj(dict(id=ID, project_id=ID, revision=integer(1), target_release_id=ID,
    payload=PAYLOAD, content_hash=string(64,64), proposal_id=ID, proposal_revision=integer(1),
    approval_id=ID, basis=BASIS, recorded_at=NUMBER, recorded_sequence=integer(1),
    historical_state=ENUM(['applied']), current_use_state=ENUM(['resolved','needs_input','conflicted','blocked','stale','expired'])))
STATE = obj(dict(project_id=ID, assessment=ENUM(['curated_reference_criterion']),
    current_use_state=ENUM(['resolved','needs_input','conflicted','blocked','stale','expired']),
    scope_epoch=integer(1), effective_at=NUMBER, known_at=NUMBER, blockers=array(string(100),10)))
GOVERNANCE_SCHEMAS = {
    'Phase4ProjectInput':obj(dict(name=string(160))),
    'Phase4MemberInput':obj(dict(actor=string(100),role=ENUM(['owner','reviewer','proposer','reader']), active=dict(type='boolean'))),
    'Phase4SourceBindingInput':obj(dict(asset_id=ID,source_version=integer(1,100),
        required=dict(type='boolean'),readers=array(string(100),100),state=ENUM(['active','withdrawn','hidden'])),['asset_id','source_version','required','readers']),
    'Phase4ContextInput':obj(dict(context=CONTEXT,reason=string(500))),
    'Phase4GovernanceInput':obj(dict(kind=ENUM(['selection_policy','conflict_gate','authority_registry','revision_relations']),
        reason=string(500), blocked=dict(type='boolean'), conflict_kind=ENUM(['value','negation','order','revision_relation']),
        claims=array(obj(dict(grounded_answer_id=ID,claim_hash=string(64,64))),20)),['kind','reason']),
    'Phase4StructureInput':obj(dict(grounded_answer_id=ID,target_release_id=ID,valid_time=PERIOD,
        expires_at=NUMBER,effective_at=NUMBER,known_at=NUMBER),['grounded_answer_id','target_release_id']),
    'Phase4ProposalRevisionInput':obj(dict(conditions=CONDITIONS,valid_time=PERIOD,reason=string(500),
        expires_at=NUMBER,target_release_id=ID),['conditions','valid_time','reason']),
    'Phase4ProposalReviewInput':obj(dict(action=ENUM(['select','reject','request_changes']),option_id=ENUM(OPTION_IDS),reason=string(500))),
    'Phase4ProposalApprovalInput':obj(dict(revision=integer(1),option_id=ENUM(OPTION_IDS),
        payload_digest=string(64,64),basis_digest=string(64,64),expires_at=NUMBER)),
    'Phase4ProposalApplyInput':obj(dict(approval_id=ID,target_revision=integer(1),manifest_digest=string(64,64))),
    'Phase4Project':PROJECT,'Phase4Proposal':PROPOSAL,'Phase4Review':REVIEW,'Phase4Approval':APPROVAL,
    'Phase4DomainVersion':DOMAIN,'Phase4State':STATE,'EmptyInput':obj({}),
}
_PREFIX='/v1/projects/{project_id}'
GOVERNANCE_OPERATIONS = [('post','/v1/projects','Phase4ProjectInput',201,'write'),('get',_PREFIX,None,200,'read')]
for suffix,schema,status in [('members','Phase4MemberInput',200),('source-bindings','Phase4SourceBindingInput',200),
    ('contexts','Phase4ContextInput',201),('governance-revisions','Phase4GovernanceInput',201),
    ('structure-runs','Phase4StructureInput',201),('proposals/{id}/revisions','Phase4ProposalRevisionInput',201),
    ('proposals/{id}/reviews','Phase4ProposalReviewInput',201),('proposals/{id}/approvals','Phase4ProposalApprovalInput',201),
    ('proposals/{id}/apply','Phase4ProposalApplyInput',201)]:
    GOVERNANCE_OPERATIONS.append(('post',_PREFIX+'/'+suffix,schema,status,'write'))
GOVERNANCE_OPERATIONS += [('get',_PREFIX+'/proposals/{id}',None,200,'read'),('get',_PREFIX+'/states',None,200,'read'),('post',_PREFIX+'/library-releases/{id}/proposals','EmptyInput',201,'write')]
GOVERNANCE_OPERATIONS=tuple(GOVERNANCE_OPERATIONS)
GOVERNANCE_RESPONSE_SCHEMAS={(verb,path):('Phase4Proposal' if '/proposals/' in path and not path.endswith(('/reviews','/approvals','/apply')) or path.endswith(('/structure-runs','/proposals')) else 'Phase4Review' if path.endswith('/reviews') else 'Phase4Approval' if path.endswith('/approvals') else 'Phase4DomainVersion' if path.endswith('/apply') else 'Phase4State' if path.endswith('/states') else 'Phase4Project') for verb,path,*_ in GOVERNANCE_OPERATIONS}

DDL = [
'CREATE TABLE IF NOT EXISTS ph4_projects(tenant TEXT,project TEXT,id TEXT,name TEXT,scope_epoch INTEGER,context_revision INTEGER,policy_revision INTEGER,state_version INTEGER,PRIMARY KEY(tenant,project,id))',
'CREATE TABLE IF NOT EXISTS ph4_project_members(tenant TEXT,project TEXT,id TEXT,role TEXT,active INTEGER,revision INTEGER,PRIMARY KEY(tenant,project,id))',
'CREATE TABLE IF NOT EXISTS ph4_project_sources(tenant TEXT,project TEXT,id TEXT,version INTEGER,sha TEXT,required INTEGER,readers TEXT,state TEXT,revision INTEGER,PRIMARY KEY(tenant,project,id))',
'CREATE TABLE IF NOT EXISTS ph4_source_binding_versions(tenant TEXT,project TEXT,id TEXT,revision INTEGER,data TEXT,actor TEXT,recorded_at REAL,recorded_sequence INTEGER,PRIMARY KEY(tenant,project,id,revision))',
'CREATE TABLE IF NOT EXISTS ph4_context_versions(tenant TEXT,project TEXT,id TEXT,revision INTEGER,data TEXT,reason TEXT,actor TEXT,recorded_at REAL,PRIMARY KEY(tenant,project,revision))',
'CREATE TABLE IF NOT EXISTS ph4_policy_versions(tenant TEXT,project TEXT,id TEXT,revision INTEGER,data TEXT,reason TEXT,actor TEXT,recorded_at REAL,PRIMARY KEY(tenant,project,revision))',
'CREATE TABLE IF NOT EXISTS ph4_proposals(tenant TEXT,project TEXT,id TEXT,revision INTEGER,data TEXT,PRIMARY KEY(tenant,project,id,revision))',
'CREATE TABLE IF NOT EXISTS ph4_proposal_heads(tenant TEXT,project TEXT,id TEXT,revision INTEGER,state_version INTEGER,state TEXT,selected TEXT,approval TEXT,PRIMARY KEY(tenant,project,id))',
'CREATE TABLE IF NOT EXISTS ph4_reviews(tenant TEXT,project TEXT,id TEXT,data TEXT,PRIMARY KEY(tenant,project,id))',
'CREATE TABLE IF NOT EXISTS ph4_approvals(tenant TEXT,project TEXT,id TEXT,data TEXT,consumed INTEGER,PRIMARY KEY(tenant,project,id))',
'CREATE TABLE IF NOT EXISTS ph4_domain_versions(tenant TEXT,project TEXT,id TEXT,revision INTEGER,release_id TEXT,data TEXT,PRIMARY KEY(tenant,project,id,revision))',
'CREATE TABLE IF NOT EXISTS ph4_conflicts(tenant TEXT,project TEXT,id TEXT,kind TEXT,blocked INTEGER,data TEXT,actor TEXT,recorded_at REAL,PRIMARY KEY(tenant,project,id))',
'CREATE TABLE IF NOT EXISTS ph4_project_epochs(tenant TEXT,project TEXT,id TEXT,epoch INTEGER,reason TEXT,actor TEXT,recorded_at REAL,PRIMARY KEY(tenant,project,epoch))',
'CREATE TABLE IF NOT EXISTS ph4_governance_sequence(id INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT,project TEXT,kind TEXT,recorded_at REAL)',
]

def initialize(db):
    # executescript commits a transaction implicitly; never use it here.
    for statement in DDL:
        db.execute(statement)


def etag(value):
    return '"'+digest(value)+'"'


class Governance:
    def __init__(self,store,library,auth,clock=time.time):
        self.store,self.library,self.auth,self.clock=store,library,auth,clock
        self.grounded=Grounded(store,library)
        self.releases=None
        with store.tx() as db:
            initialize(db)

    def _capacity(self,db,table,tenant,project=None,maximum=100):
        query='SELECT count(*) FROM '+table+' WHERE tenant=?'
        args=[tenant]
        if project is not None:
            query+=' AND project=?'
            args.append(project)
        if db.execute(query,args).fetchone()[0]>=maximum:
            raise APIError(413,'governance_capacity_limit')

    def _principal(self,ctx,human=False):
        # Actor type is taken only from the server registry, never request JSON.
        with self.auth.lock:
            matches=[p for p in self.auth.principals.values() if p['tenant']==ctx['tenant'] and
                p['actor']==ctx['actor'] and p['revision']==ctx['revision'] and p.get('active',True) and
                p.get('expires_at',float('inf'))>time.time()]
            if not matches:
                raise APIError(403,'permission_revoked')
            if human and (ctx.get('actor_type','machine')!='human' or not any(p.get('actor_type','machine')=='human' and 'write' in p['scopes'] for p in matches)):
                raise APIError(403,'human_approval_required')

    def project_guard(self,db,ctx,project,roles=None,human=False):
        self._principal(ctx,human)
        row=db.execute('SELECT * FROM ph4_projects WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,project)).fetchone()
        member=db.execute('SELECT * FROM ph4_project_members WHERE tenant=? AND project=? AND id=? AND active=1',(ctx['tenant'],project,ctx['actor'])).fetchone()
        if row is None or member is None:
            raise APIError(404,'not_found')
        if roles and member['role'] not in roles:
            raise APIError(403,'project_role_denied')
        return row

    def asset_allowed(self,db,tenant,actor,asset):
        bindings=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND id=?',(tenant,asset)).fetchall()
        if not bindings:
            return True
        for binding in bindings:
            member=db.execute('SELECT active FROM ph4_project_members WHERE tenant=? AND project=? AND id=?',(tenant,binding['project'],actor)).fetchone()
            if binding['state']=='active' and member and member['active'] and actor in json.loads(binding['readers']):
                return True
        return False

    def asset_writable(self,db,tenant,actor,asset,action='write'):
        bindings=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND id=?',(tenant,asset)).fetchall()
        if not bindings:
            return True
        roles=['owner'] if action=='delete' else ['owner','proposer']
        for binding in bindings:
            member=db.execute('SELECT role,active FROM ph4_project_members WHERE tenant=? AND project=? AND id=?',(tenant,binding['project'],actor)).fetchone()
            if binding['state']=='active' and member and member['active'] and member['role'] in roles and actor in json.loads(binding['readers']):
                return True
        return False

    def worker_allowed(self,tenant,actor,asset):
        # Called inside Store's existing transaction/lock, not a nested tx.
        return self.asset_writable(self.store.db,tenant,actor,asset)

    def _project(self,row):
        return dict(id=row['id'],tenant_id=row['tenant'],name=row['name'],scope_epoch=row['scope_epoch'],
            context_revision=row['context_revision'],policy_revision=row['policy_revision'],state_version=row['state_version'])

    def _match(self,value,if_match):
        if if_match is None:
            raise APIError(428,'precondition_required')
        if if_match!=etag(value):
            raise APIError(412,'revision_mismatch')

    def _sequence(self,db,ctx,project,kind):
        self._capacity(db,'ph4_governance_sequence',ctx['tenant'],project,16384)
        return db.execute('INSERT INTO ph4_governance_sequence(tenant,project,kind,recorded_at) VALUES(?,?,?,?)',
            (ctx['tenant'],project,kind,self.clock())).lastrowid

    def _epoch(self,db,ctx,project,reason):
        self._capacity(db,'ph4_project_epochs',ctx['tenant'],project,4096)
        db.execute('UPDATE ph4_projects SET scope_epoch=scope_epoch+1,state_version=state_version+1 WHERE tenant=? AND project=?',(ctx['tenant'],project))
        epoch=db.execute('SELECT scope_epoch FROM ph4_projects WHERE tenant=? AND project=?',(ctx['tenant'],project)).fetchone()[0]
        db.execute('INSERT INTO ph4_project_epochs VALUES(?,?,?,?,?,?,?)',(ctx['tenant'],project,uid(),epoch,reason,ctx['actor'],self.clock()))

    def source_changed(self,db,ctx,asset,event):
        """An arrival changes knowledge, not the selected source revision.

        The library calls this in its ingest/delete transaction after validating
        current source write authority. Every bound scope notices new knowledge;
        explicit succession and source selection remain separate operations.
        """
        if ctx is None:
            return
        projects=db.execute('SELECT DISTINCT project FROM ph4_project_sources WHERE tenant=? AND id=?',(ctx['tenant'],asset)).fetchall()
        for row in projects:
            project=row['project']
            if event in ('delete','deleted','source_deleted'):
                self._capacity(db,'ph4_source_binding_versions',ctx['tenant'],project,4096)
                db.execute("UPDATE ph4_project_sources SET state='withdrawn',revision=revision+1 WHERE tenant=? AND project=? AND id=?",(ctx['tenant'],project,asset))
                binding=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,asset)).fetchone()
                db.execute('INSERT INTO ph4_source_binding_versions VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,asset,binding['revision'],canonical(dict(binding)),ctx['actor'],self.clock(),self._sequence(db,ctx,project,'source_tombstone')))
            self._epoch(db,ctx,project,'source_'+str(event))

    def _bindings(self,db,ctx,project):
        return db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND project=? ORDER BY id',(ctx['tenant'],project)).fetchall()

    def _basis(self,db,ctx,project,effective_at=None,known_at=None):
        row=self.project_guard(db,ctx,project)
        context=db.execute('SELECT data FROM ph4_context_versions WHERE tenant=? AND project=? AND revision=?',(ctx['tenant'],project,row['context_revision'])).fetchone()
        policy=db.execute('SELECT data FROM ph4_policy_versions WHERE tenant=? AND project=? AND revision=?',(ctx['tenant'],project,row['policy_revision'])).fetchone()
        return dict(scope_epoch=row['scope_epoch'],context_revision=row['context_revision'],policy_revision=row['policy_revision'],
            context_hash=digest(json.loads(context[0])),policy_hash=digest(json.loads(policy[0])),
            inputs=[dict(resource_type='source',resource_id=b['id'],revision=b['version'],content_hash=b['sha'],binding_revision=b['revision']) for b in self._bindings(db,ctx,project)],
            effective_at=self.clock() if effective_at is None else effective_at,known_at=self.clock() if known_at is None else known_at)

    def _period(self,period):
        validate(period,PERIOD)
        if (period['start_kind']=='known')!=(period['start_at'] is not None) or (period['end_kind']=='known')!=(period['end_at'] is not None):
            raise APIError(422,'invalid_condition')
        if period['start_at'] is not None and period['end_at'] is not None and period['start_at']>=period['end_at']:
            raise APIError(422,'invalid_condition')
        if period['precision']=='date' and any(t is not None and t%86400 for t in (period['start_at'],period['end_at'])):
            raise APIError(422,'invalid_condition')
        return period

    def _candidate(self,db,ctx,project,id):
        if self.releases is None:
            raise APIError(501,'candidate_release_unavailable')
        return self.releases.candidate(db,ctx,project,id)

    def conflict_blocked(self,db,tenant,project):
        latest=db.execute('SELECT blocked FROM ph4_conflicts WHERE tenant=? AND project=? ORDER BY rowid DESC LIMIT 1',(tenant,project)).fetchone()
        return bool(latest and latest['blocked'])

    def _conflicted(self,db,ctx,project):
        return self.conflict_blocked(db,ctx['tenant'],project)

    def _complete(self,db,ctx,project):
        for binding in self._bindings(db,ctx,project):
            if not binding['required']:
                continue
            if binding['state']!='active' or ctx['actor'] not in json.loads(binding['readers']):
                return False
            try:
                self.store.live(db,ctx['tenant'],binding['id'])
                raw,sha=self.library.original(db,ctx['tenant'],binding['id'],binding['version'])
                if sha!=binding['sha']:
                    return False
            except APIError:
                return False
        return True

    def _gate(self,db,ctx,project,value,current_time=True):
        # Ordering prevents leaking details of an unseen dependency or conflict.
        if not self._complete(db,ctx,project):
            return 'blocked',['prerequisites_unverifiable'],None
        if self._conflicted(db,ctx,project):
            return 'blocked',['prerequisites_unverifiable'],None
        basis=value['basis']
        current=self._basis(db,ctx,project,basis['effective_at'],basis['known_at'])
        if canonical(current)!=canonical(basis):
            activated=None
            if value.get('state')=='applied' and self.releases is not None and hasattr(self.releases,'activation_epoch'):
                activated=self.releases.activation_epoch(db,ctx,project,value['target']['resource_id'])
            adjusted={**basis,'scope_epoch':activated}
            if activated is None or canonical(current)!=canonical(adjusted):
                return 'stale',['proposal_stale'],None
        now=self.clock()
        if value.get('state')!='applied' and now>=value['expires_at']:
            return 'expired',['proposal_expired'],None
        approval_end=None
        if value.get('state') not in ('applied','rejected') and value.get('approval_id'):
            approval=db.execute('SELECT data,consumed FROM ph4_approvals WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,value['approval_id'])).fetchone()
            if approval and not approval['consumed']:
                approval_end=json.loads(approval['data'])['expires_at']
                if now>=approval_end:
                    return 'expired',['approval_expired'],None
        if value['kind']=='library_release':
            return 'resolved',[],None if value.get('state')=='applied' else min(value['expires_at'],approval_end) if approval_end is not None else value['expires_at']
        payload=value['options'][0]['payload']
        self._period(payload['valid_time'])
        context=json.loads(db.execute('SELECT data FROM ph4_context_versions WHERE tenant=? AND project=? AND revision=?',(ctx['tenant'],project,basis['context_revision'])).fetchone()[0])
        missing=[]
        if 'scaffold_type' not in context:
            missing.append('scaffold_type')
        elif context['scaffold_type']!=payload['conditions']['scaffold_type']['value']:
            return 'needs_input',['condition_false'],None
        period=payload['valid_time']
        if period['start_kind']=='unknown' or period['end_kind']=='unknown':
            missing.append('valid_time')
        if missing:
            return 'needs_input',missing,None
        instant=now if current_time else basis['effective_at']
        if instant<period['start_at'] or period['end_kind']=='known' and instant>=period['end_at']:
            return 'expired',['outside_valid_time'],period['start_at'] if instant<period['start_at'] else None
        boundaries=([] if value.get('state')=='applied' else [value['expires_at']])+([approval_end] if approval_end is not None else [])+([period['end_at']] if period['end_kind']=='known' else [])
        return 'resolved',[],min(boundaries) if boundaries else None

    def _get(self,db,ctx,project,id):
        self.project_guard(db,ctx,project)
        head=db.execute('SELECT * FROM ph4_proposal_heads WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,id)).fetchone()
        if head is None:
            raise APIError(404,'not_found')
        value=json.loads(db.execute('SELECT data FROM ph4_proposals WHERE tenant=? AND project=? AND id=? AND revision=?',(ctx['tenant'],project,id,head['revision'])).fetchone()[0])
        value.update(state_version=head['state_version'],state=head['state'],selected_option_id=head['selected'],approval_id=head['approval'])
        state,blockers,next_at=self._gate(db,ctx,project,value)
        value.update(current_use_state=state,blockers=blockers,reevaluate_at=next_at,validation_state='stale' if state=='stale' else 'current')
        if head['state'] not in ('applied','rejected') and state in ('stale','expired'):
            value['state']=state
        return value

    def _save_proposal(self,db,ctx,project,value,new=True):
        self._capacity(db,'ph4_proposals',ctx['tenant'],project,4096)
        if new:
            self._capacity(db,'ph4_proposal_heads',ctx['tenant'],project,LIMITS['derived_records'])
        db.execute('INSERT INTO ph4_proposals VALUES(?,?,?,?,?)',(ctx['tenant'],project,value['id'],value['revision'],canonical(value)))
        if new:
            db.execute('INSERT INTO ph4_proposal_heads VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,value['id'],value['revision'],1,value['state'],None,None))
        else:
            db.execute('UPDATE ph4_proposal_heads SET revision=?,state_version=state_version+1,state=?,selected=NULL,approval=NULL WHERE tenant=? AND project=? AND id=?',
                (value['revision'],value['state'],ctx['tenant'],project,value['id']))
        return self._get(db,ctx,project,value['id'])

    def _verified_answer(self,db,ctx,project,id):
        answer=self.store.record(db,ctx['tenant'],'grounded_answers',id)
        self.grounded.verify_saved(db,ctx['tenant'],answer)
        if answer['status']!='supported' or answer['resolved_field']!='midrail_height' or answer['scaffold_type'] is None:
            raise APIError(409,'qualified_claim_required')
        claim=answer['claims'][0]
        support=claim['support_evidence']
        ref=support['evidence']
        binding=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,ref['asset_id'])).fetchone()
        if binding is None or binding['version']!=ref['source_version'] or binding['sha']!=support['source_sha256']:
            raise APIError(409,'project_source_binding_required')
        source=db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',(ctx['tenant'],ref['asset_id'],ref['source_version'])).fetchone()
        if json.loads(source[0])['media_type']!='application/pdf' or support['locator']['kind']!='pdf_page' or 'ocr' in ref['analysis_version'].lower():
            raise APIError(409,'native_pdf_claim_required')
        if binding['state']!='active' or ctx['actor'] not in json.loads(binding['readers']):
            raise APIError(404,'not_found')
        return answer

    def structure(self,db,ctx,project,body):
        self.project_guard(db,ctx,project,['owner','reviewer','proposer'])
        if not self._complete(db,ctx,project):
            raise APIError(409,'prerequisites_unverifiable')
        answer=self._verified_answer(db,ctx,project,body['grounded_answer_id'])
        candidate=self._candidate(db,ctx,project,body['target_release_id'])
        basis=self._basis(db,ctx,project,body.get('effective_at'),body.get('known_at'))
        if any(body.get(field) is not None and abs(body[field]-self.clock())>1 for field in ('known_at','effective_at')):
            # Full policy/relations as-of registry is deliberately out of scope.
            raise APIError(501,'historical_authority_unavailable')
        payload=dict(schema_version='midrail-reference-v1',grounded_answer_id=answer['id'],claim=answer['claims'][0],
            conditions=dict(scaffold_type=dict(operator='eq',value=answer['scaffold_type'])),
            valid_time=self._period(body.get('valid_time',dict(start_kind='unknown',start_at=None,end_kind='unknown',end_at=None,timezone='UTC',precision='second'))),
            assessment='curated_reference_criterion')
        now=self.clock()
        value=dict(id=uid(),project_id=project,revision=1,state_version=1,kind='structure',
            target=dict(resource_type='candidate_reference_definition',resource_id=body['target_release_id'],revision=candidate['revision'],manifest_digest=candidate['manifest_digest']),
            basis=basis,basis_digest=digest(basis),options=[dict(option_id='source-criterion',payload=payload,payload_digest=digest(payload),
            reasons=['Bounded native-PDF claim; literal values and source role preserved.'],source_diff='literal_source_values_preserved',missing_conditions=[],unresolved_conflicts=[],differences=[])],
            recommended_option_id='source-criterion',blockers=[],expires_at=body.get('expires_at',now+3600),state='ready',validation_state='current',
            current_use_state='resolved',selected_option_id=None,approval_id=None,recorded_at=now,recorded_sequence=self._sequence(db,ctx,project,'proposal'),reevaluate_at=None,
            notice='資料の点検内容例を参照基準として整理。発行者権威・公式適用指示・実現場の安全や合否を認定しない。')
        state,blockers,next_at=self._gate(db,ctx,project,value)
        value.update(state='ready' if state=='resolved' else 'needs_input',current_use_state=state,blockers=blockers,reevaluate_at=next_at)
        value['options'][0]['missing_conditions']=[b for b in blockers if b in ('scaffold_type','valid_time')]
        return self._save_proposal(db,ctx,project,value)

    def _option(self,value,option_id):
        matches=[option for option in value['options'] if option['option_id']==option_id]
        if len(matches)!=1:
            raise APIError(422,'invalid_option')
        return matches[0]

    def _ready(self,db,ctx,project,value):
        state,_,_=self._gate(db,ctx,project,value)
        errors=dict(stale='proposal_stale',expired='approval_expired',needs_input='needs_input',blocked='prerequisites_unverifiable',conflicted='unresolved_conflict')
        if state!='resolved':
            raise APIError(409,errors[state])
        if value['state'] not in ('ready','approved'):
            raise APIError(409,'proposal_not_actionable')
        if digest(value['basis'])!=value['basis_digest'] or digest(value['options'][0]['payload'])!=value['options'][0]['payload_digest']:
            raise APIError(409,'proposal_integrity_failed')
        if value['kind']=='structure':
            answer=self._verified_answer(db,ctx,project,value['options'][0]['payload']['grounded_answer_id'])
            if canonical(answer['claims'][0])!=canonical(value['options'][0]['payload']['claim']):
                raise APIError(409,'grounded_answer_integrity_failed')
        candidate=self._candidate(db,ctx,project,value['target']['resource_id']) if value['kind']=='structure' else self.releases.release_for_approval(db,ctx,project,value['target']['resource_id'])
        if candidate['revision']!=value['target']['revision'] or candidate['manifest_digest']!=value['target']['manifest_digest']:
            raise APIError(409,'target_release_changed')

    def handle(self,db,ctx,route,params,body,if_match=None):
        project=params.get('project_id')
        schema=next((schema for verb,path,schema,*_ in GOVERNANCE_OPERATIONS if path==route and schema),None)
        if body is not None and schema:
            validate(body,GOVERNANCE_SCHEMAS[schema])
        assets=[]
        if route=='/v1/projects':
            self._principal(ctx,True)
            self._capacity(db,'ph4_projects',ctx['tenant'])
            project=uid()
            db.execute('INSERT INTO ph4_projects VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,project,body['name'],1,1,1,1))
            db.execute('INSERT INTO ph4_project_members VALUES(?,?,?,?,?,?)',(ctx['tenant'],project,ctx['actor'],'owner',1,1))
            for table,data in [('ph4_context_versions',{}),('ph4_policy_versions',{'selection':'curated-reference-only-v1'})]:
                db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,uid(),1,canonical(data),'initial',ctx['actor'],self.clock()))
            value=self._project(self.project_guard(db,ctx,project))
            return 201,value,[],{'ETag':etag(value)}
        row=self.project_guard(db,ctx,project)
        if route==_PREFIX:
            value=self._project(row)
            return 200,value,[],{'ETag':etag(value)}
        if route.endswith(('/members','/source-bindings','/contexts','/governance-revisions')):
            self.project_guard(db,ctx,project,['owner'],True)
            self._match(self._project(row),if_match)
            if route.endswith('/members'):
                if not db.execute('SELECT 1 FROM ph4_project_members WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,body['actor'])).fetchone():
                    self._capacity(db,'ph4_project_members',ctx['tenant'],project)
                with self.auth.lock:
                    if not any(p['tenant']==ctx['tenant'] and p['actor']==body['actor'] for p in self.auth.principals.values()):
                        raise APIError(422,'registered_principal_required')
                db.execute('INSERT INTO ph4_project_members VALUES(?,?,?,?,?,1) ON CONFLICT(tenant,project,id) DO UPDATE SET role=excluded.role,active=excluded.active,revision=revision+1',
                    (ctx['tenant'],project,body['actor'],body['role'],int(body['active'])))
                self._epoch(db,ctx,project,'membership_changed')
            elif route.endswith('/source-bindings'):
                self._capacity(db,'ph4_source_binding_versions',ctx['tenant'],project,4096)
                if not db.execute('SELECT 1 FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,body['asset_id'])).fetchone():
                    self._capacity(db,'ph4_project_sources',ctx['tenant'],project)
                existing=db.execute('SELECT version,sha FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,body['asset_id'])).fetchone()
                if existing and existing['version']==body['source_version']:
                    # An authorized owner can restore/manage the ACL for the
                    # pinned binding without receiving currently hidden bytes.
                    sha=existing['sha']
                else:
                    _,sha=self.library.original(db,ctx['tenant'],body['asset_id'],body['source_version'])
                for actor in body['readers']:
                    if not db.execute('SELECT 1 FROM ph4_project_members WHERE tenant=? AND project=? AND id=? AND active=1',(ctx['tenant'],project,actor)).fetchone():
                        raise APIError(422,'project_member_required')
                db.execute('INSERT INTO ph4_project_sources VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT(tenant,project,id) DO UPDATE SET version=excluded.version,sha=excluded.sha,required=excluded.required,readers=excluded.readers,state=excluded.state,revision=revision+1',
                    (ctx['tenant'],project,body['asset_id'],body['source_version'],sha,int(body['required']),canonical(body['readers']),body.get('state','active')))
                binding=db.execute('SELECT * FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,body['asset_id'])).fetchone()
                db.execute('INSERT INTO ph4_source_binding_versions VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,body['asset_id'],binding['revision'],canonical(dict(binding)),ctx['actor'],self.clock(),self._sequence(db,ctx,project,'source_binding')))
                self._epoch(db,ctx,project,'source_binding_changed')
            elif route.endswith('/contexts'):
                self._capacity(db,'ph4_context_versions',ctx['tenant'],project,4096)
                revision=row['context_revision']+1
                db.execute('INSERT INTO ph4_context_versions VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,uid(),revision,canonical(body['context']),body['reason'],ctx['actor'],self.clock()))
                db.execute('UPDATE ph4_projects SET context_revision=? WHERE tenant=? AND project=?',(revision,ctx['tenant'],project))
                self._epoch(db,ctx,project,'context_changed')
            else:
                if body['kind'] in ('authority_registry','revision_relations'):
                    raise APIError(501,'authority_registry_unavailable')
                if body['kind']=='conflict_gate':
                    self._capacity(db,'ph4_conflicts',ctx['tenant'],project,4096)
                    if 'blocked' not in body or 'conflict_kind' not in body:
                        raise APIError(422,'invalid_condition')
                    # An owner explicitly records an internal typed impact gate;
                    # this does not fabricate a conflict from unrelated PDFs.
                    db.execute('INSERT INTO ph4_conflicts VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,uid(),body['conflict_kind'],int(body['blocked']),canonical(body),ctx['actor'],self.clock()))
                    self._epoch(db,ctx,project,'conflict_gate_changed')
                else:
                    self._capacity(db,'ph4_policy_versions',ctx['tenant'],project,4096)
                    revision=row['policy_revision']+1
                    db.execute('INSERT INTO ph4_policy_versions VALUES(?,?,?,?,?,?,?,?)',(ctx['tenant'],project,uid(),revision,canonical({'selection':'curated-reference-only-v1'}),body['reason'],ctx['actor'],self.clock()))
                    db.execute('UPDATE ph4_projects SET policy_revision=? WHERE tenant=? AND project=?',(revision,ctx['tenant'],project))
                    self._epoch(db,ctx,project,'policy_changed')
            value=self._project(self.project_guard(db,ctx,project))
            return (200 if route.endswith(('/members','/source-bindings')) else 201),value,[],{'ETag':etag(value)}
        if route.endswith('/library-releases/{id}/proposals'):
            value=self.release_proposal(db,ctx,project,params['id'])
            return 201,value,[ref['resource_id'] for ref in value['basis']['inputs']],{'ETag':etag(value)}
        if route.endswith('/structure-runs'):
            value=self.structure(db,ctx,project,body)
            assets=[ref['resource_id'] for ref in value['basis']['inputs']]
            return 201,value,assets,{'ETag':etag(value)}
        if route.endswith('/states'):
            value=dict(project_id=project,assessment='curated_reference_criterion',current_use_state='needs_input',scope_epoch=row['scope_epoch'],effective_at=self.clock(),known_at=self.clock(),blockers=['no_published_reference'])
            if not self._complete(db,ctx,project) or self._conflicted(db,ctx,project):
                value.update(current_use_state='blocked',blockers=['prerequisites_unverifiable'])
            elif self.releases is not None and hasattr(self.releases,'published_reference_state'):
                try:
                    published=self.releases.published_reference_state(db,ctx,project)
                except APIError:
                    published=None
                    value.update(current_use_state='blocked',blockers=['prerequisites_unverifiable'])
                if published and published['domains']:
                    if published['current_use_state']!='resolved':
                        state=published['current_use_state']
                        value.update(current_use_state=state,blockers=['prerequisites_unverifiable'] if state=='blocked' else ['published_reference_unusable'])
                    else:
                        value.update(current_use_state='resolved',blockers=[])
                        for ref in published['domains']:
                            stored=db.execute('SELECT data FROM ph4_domain_versions WHERE tenant=? AND project=? AND id=? AND revision=? AND release_id=?',
                                (ctx['tenant'],project,ref['resource_id'],ref['revision'],published['release_id'])).fetchone()
                            if stored is None:
                                value.update(current_use_state='blocked',blockers=['prerequisites_unverifiable']);break
                            domain=json.loads(stored['data'])
                            if domain['content_hash']!=ref['content_hash'] or digest(domain['payload'])!=ref['content_hash'] or domain['proposal_id']!=ref['proposal_id'] or domain['proposal_revision']!=ref['proposal_revision']:
                                value.update(current_use_state='blocked',blockers=['prerequisites_unverifiable']);break
                            try:
                                self.verify_response(db,ctx,domain,route,receipt_replay=True)
                            except APIError:
                                value.update(current_use_state='blocked',blockers=['prerequisites_unverifiable']);break
                            proposal=self._get(db,ctx,project,domain['proposal_id'])
                            if proposal['current_use_state']!='resolved':
                                value.update(current_use_state=proposal['current_use_state'],blockers=proposal['blockers']);break
            return 200,value,[],{}
        value=self._get(db,ctx,project,params['id'])
        if route==_PREFIX+'/proposals/{id}':
            return 200,value,[ref['resource_id'] for ref in value['basis']['inputs']],{'ETag':etag(value)}
        self.project_guard(db,ctx,project,['owner','reviewer','proposer'])
        if if_match is None:
            raise APIError(428,'precondition_required')
        if not route.endswith('/revisions'):
            self._ready(db,ctx,project,value)
        self._match(value,if_match)
        if route.endswith('/revisions'):
            if value['kind']=='library_release':
                raise APIError(409,'release_reproposal_required')
            if value['state']=='applied':
                raise APIError(409,'proposal_already_applied')
            self._period(body['valid_time'])
            answer=self._verified_answer(db,ctx,project,value['options'][0]['payload']['grounded_answer_id'])
            if body['conditions']['scaffold_type']['value']!=answer['scaffold_type']:
                raise APIError(422,'source_condition_mismatch')
            value['revision']+=1
            value['basis']=self._basis(db,ctx,project)
            value['basis_digest']=digest(value['basis'])
            release_id=body.get('target_release_id',value['target']['resource_id'])
            candidate=self._candidate(db,ctx,project,release_id)
            value['target'].update(resource_id=release_id,revision=candidate['revision'],manifest_digest=candidate['manifest_digest'])
            value['options'][0]['payload'].update(conditions=body['conditions'],valid_time=body['valid_time'])
            value['options'][0]['payload_digest']=digest(value['options'][0]['payload'])
            value['options'][0]['differences']=[body['reason']]
            value.update(state='ready',approval_id=None,selected_option_id=None,recorded_at=self.clock(),recorded_sequence=self._sequence(db,ctx,project,'revision'),expires_at=body.get('expires_at',self.clock()+3600))
            state,blockers,_=self._gate(db,ctx,project,value)
            value['state']='ready' if state=='resolved' else 'needs_input'
            value['options'][0]['missing_conditions']=[b for b in blockers if b in ('scaffold_type','valid_time')]
            result=self._save_proposal(db,ctx,project,value,False)
        elif route.endswith('/reviews'):
            self._option(value,body['option_id'])
            self.project_guard(db,ctx,project,['owner','reviewer'],True)
            self._ready(db,ctx,project,value)
            result=dict(id=uid(),project_id=project,proposal_id=value['id'],proposal_revision=value['revision'],action=body['action'],option_id=body['option_id'],reason=body['reason'],actor=ctx['actor'],recorded_at=self.clock(),state_version=value['state_version']+1)
            self._capacity(db,'ph4_reviews',ctx['tenant'],project,LIMITS['derived_records'])
            db.execute('INSERT INTO ph4_reviews VALUES(?,?,?,?)',(ctx['tenant'],project,result['id'],canonical(result)))
            db.execute('UPDATE ph4_proposal_heads SET state_version=state_version+1,state=?,selected=?,approval=NULL WHERE tenant=? AND project=? AND id=?',
                ('rejected' if body['action']=='reject' else 'needs_input' if body['action']=='request_changes' else 'ready',body['option_id'] if body['action']=='select' else None,ctx['tenant'],project,value['id']))
        elif route.endswith('/approvals'):
            option=self._option(value,body['option_id'])
            self.project_guard(db,ctx,project,['owner','reviewer'],True)
            self._ready(db,ctx,project,value)
            if value['selected_option_id']!=body['option_id']:
                raise APIError(409,'review_selection_required')
            if body['revision']!=value['revision'] or body['payload_digest']!=option['payload_digest'] or body['basis_digest']!=value['basis_digest']:
                raise APIError(409,'approval_digest_mismatch')
            if not self.clock()<body['expires_at']<=value['expires_at']:
                raise APIError(409,'approval_expired')
            role=db.execute('SELECT role FROM ph4_project_members WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,ctx['actor'])).fetchone()[0]
            result=dict(id=uid(),project_id=project,proposal_id=value['id'],proposal_revision=value['revision'],option_id=body['option_id'],payload_digest=body['payload_digest'],basis_digest=body['basis_digest'],target=value['target'],actor=ctx['actor'],actor_type='human',role=role,recorded_at=self.clock(),expires_at=body['expires_at'],consumed=False,state_version=value['state_version']+1)
            self._capacity(db,'ph4_approvals',ctx['tenant'],project,LIMITS['derived_records'])
            db.execute('INSERT INTO ph4_approvals VALUES(?,?,?,?,0)',(ctx['tenant'],project,result['id'],canonical(result)))
            db.execute('UPDATE ph4_proposal_heads SET state_version=state_version+1,state=?,approval=? WHERE tenant=? AND project=? AND id=?',('approved',result['id'],ctx['tenant'],project,value['id']))
        elif route.endswith('/apply'):
            self._ready(db,ctx,project,value)
            if value['kind']=='library_release':
                raise APIError(409,'publish_required')
            approval=self.validate_approval(db,ctx,project,value,body['approval_id'])
            candidate=self._candidate(db,ctx,project,value['target']['resource_id'])
            if body['target_revision']!=candidate['revision'] or body['manifest_digest']!=candidate['manifest_digest'] or candidate['revision']!=value['target']['revision'] or candidate['manifest_digest']!=value['target']['manifest_digest']:
                raise APIError(409,'target_release_changed')
            payload=value['options'][0]['payload']
            result=dict(id=uid(),project_id=project,revision=1,target_release_id=value['target']['resource_id'],payload=payload,content_hash=digest(payload),proposal_id=value['id'],proposal_revision=value['revision'],approval_id=approval['id'],basis=value['basis'],recorded_at=self.clock(),recorded_sequence=self._sequence(db,ctx,project,'apply'),historical_state='applied',current_use_state='resolved')
            self._capacity(db,'ph4_domain_versions',ctx['tenant'],project,LIMITS['derived_records'])
            self.releases.adopt(db,ctx,project,value['target']['resource_id'],result)
            db.execute('INSERT INTO ph4_domain_versions VALUES(?,?,?,?,?,?)',(ctx['tenant'],project,result['id'],1,result['target_release_id'],canonical(result)))
            self.consume_approval(db,ctx,project,approval['id'])
            db.execute('UPDATE ph4_proposal_heads SET state_version=state_version+1,state=? WHERE tenant=? AND project=? AND id=?',('applied',ctx['tenant'],project,value['id']))
            # Candidate domain storage does not alter the live head or epoch.
        else:
            raise APIError(404,'not_found')
        assets=[ref['resource_id'] for ref in value['basis']['inputs']]
        return 201,result,assets,{'ETag':etag(result),'X-Target-ETag':etag(self._get(db,ctx,project,value['id']))}

    def release_proposal(self,db,ctx,project,release_id):
        self.project_guard(db,ctx,project,['owner','reviewer','proposer'])
        if not self._complete(db,ctx,project):
            raise APIError(409,'prerequisites_unverifiable')
        if self.releases is None:
            raise APIError(501,'candidate_release_unavailable')
        release=self.releases.release_for_approval(db,ctx,project,release_id)
        basis=self._basis(db,ctx,project)
        payload=dict(release_id=release_id,manifest_digest=release['manifest_digest'],expected_head_revision=release['head_revision'],publish_intent='publish_curated_reference_release')
        now=self.clock()
        value=dict(id=uid(),project_id=project,revision=1,state_version=1,kind='library_release',
            target=dict(resource_type='library_release',resource_id=release_id,revision=release['revision'],manifest_digest=release['manifest_digest']),
            basis=basis,basis_digest=digest(basis),options=[dict(option_id='publish-release',payload=payload,payload_digest=digest(payload),
                reasons=['Publish the reviewed immutable generation and coverage manifest.'],source_diff='literal_source_values_preserved',missing_conditions=[],unresolved_conflicts=[],differences=[])],
            recommended_option_id='publish-release',blockers=[],expires_at=now+3600,state='ready',validation_state='current',current_use_state='resolved',
            selected_option_id=None,approval_id=None,recorded_at=now,recorded_sequence=self._sequence(db,ctx,project,'release_proposal'),reevaluate_at=now+3600,
            notice='参照ライブラリの公開承認。候補内の構造承認とは別に、索引・coverage・manifest・期待head版・公開intentを固定する。')
        return self._save_proposal(db,ctx,project,value)

    def admit_publish(self,db,ctx,project,proposal_id,approval_id,release_id,manifest_digest,expected_head_revision):
        self.project_guard(db,ctx,project,['owner','reviewer','proposer'])
        value=self._get(db,ctx,project,proposal_id)
        if value['kind']!='library_release':
            raise APIError(409,'publish_approval_required')
        self._ready(db,ctx,project,value)
        payload=value['options'][0]['payload']
        if payload['release_id']!=release_id or payload['manifest_digest']!=manifest_digest or payload['expected_head_revision']!=expected_head_revision:
            raise APIError(409,'publish_manifest_mismatch')
        # Each adopted criterion must still be usable now. Candidate storage did
        # not advance live epoch, but elapsed time can independently invalidate it.
        for row in db.execute('SELECT data FROM ph4_domain_versions WHERE tenant=? AND project=? AND release_id=?',(ctx['tenant'],project,release_id)):
            domain=json.loads(row['data'])
            original=self._get(db,ctx,project,domain['proposal_id'])
            state,_,_=self._gate(db,ctx,project,original)
            if state!='resolved':
                raise APIError(409,'candidate_domain_unusable')
            self.verify_response(db,ctx,domain,_PREFIX+'/proposals/{id}',receipt_replay=True)
        return self.validate_approval(db,ctx,project,value,approval_id)

    def mark_published(self,db,ctx,project,proposal_id):
        db.execute('UPDATE ph4_proposal_heads SET state_version=state_version+1,state=? WHERE tenant=? AND project=? AND id=?',
            ('applied',ctx['tenant'],project,proposal_id))

    def validate_approval(self,db,ctx,project,value,id):
        row=db.execute('SELECT * FROM ph4_approvals WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,id)).fetchone()
        if not row:
            raise APIError(404,'not_found')
        approval=json.loads(row['data'])
        if row['consumed']:
            raise APIError(409,'approval_consumed')
        if self.clock()>=approval['expires_at']:
            raise APIError(409,'approval_expired')
        try:
            option=self._option(value,approval['option_id'])
        except APIError as error:
            raise APIError(409,'approval_digest_mismatch') from error
        if value['selected_option_id']!=option['option_id'] or approval['option_id']!=value['selected_option_id']:
            raise APIError(409,'approval_digest_mismatch')
        if value['state']!='approved' or value['approval_id']!=id or approval['proposal_id']!=value['id'] or approval['proposal_revision']!=value['revision'] or approval['payload_digest']!=option['payload_digest'] or approval['basis_digest']!=value['basis_digest'] or canonical(approval['target'])!=canonical(value['target']):
            raise APIError(409,'approval_digest_mismatch')
        member=db.execute('SELECT role,active FROM ph4_project_members WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,approval['actor'])).fetchone()
        with self.auth.lock:
            human=any(p['tenant']==ctx['tenant'] and p['actor']==approval['actor'] and p.get('actor_type','machine')=='human' and 'write' in p['scopes'] and p.get('active',True) and p.get('expires_at',float('inf'))>time.time() for p in self.auth.principals.values())
        if not human or not member or not member['active'] or member['role'] not in ('owner','reviewer'):
            raise APIError(403,'permission_revoked')
        return approval

    def consume_approval(self,db,ctx,project,id):
        changed=db.execute('UPDATE ph4_approvals SET consumed=1 WHERE tenant=? AND project=? AND id=? AND consumed=0',(ctx['tenant'],project,id)).rowcount
        if changed!=1:
            raise APIError(409,'approval_consumed')

    def verify_response(self,db,ctx,value,route,receipt_replay=False):
        project=value.get('project_id',value.get('id') if route in (_PREFIX,'/v1/projects') else None)
        if project:
            self.project_guard(db,ctx,project)
        # Replay validates immutable evidence/current ACL, not old temporal or
        # basis eligibility: receipt recovery never executes an operation again.
        def walk(item):
            if isinstance(item,dict):
                if 'resource_type' in item and item['resource_type']=='source':
                    binding=db.execute('SELECT state,readers FROM ph4_project_sources WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,item['resource_id'])).fetchone()
                    if not self.asset_allowed(db,ctx['tenant'],ctx['actor'],item['resource_id']) or binding is not None and (binding['state']!='active' or ctx['actor'] not in json.loads(binding['readers'])):
                        raise APIError(404,'not_found')
                    _,sha=self.library.original(db,ctx['tenant'],item['resource_id'],item['revision'])
                    if sha!=item['content_hash']:
                        raise APIError(409,'source_integrity_failed')
                if 'grounded_answer_id' in item:
                    answer=self.store.record(db,ctx['tenant'],'grounded_answers',item['grounded_answer_id'])
                    self.grounded.verify_saved(db,ctx['tenant'],answer)
                    if canonical(item.get('claim'))!=canonical(answer['claims'][0]):
                        raise APIError(409,'grounded_answer_integrity_failed')
                if 'basis_digest' in item and 'basis' in item and digest(item['basis'])!=item['basis_digest']:
                    raise APIError(409,'basis_integrity_failed')
                if 'payload_digest' in item and 'payload' in item and digest(item['payload'])!=item['payload_digest']:
                    raise APIError(409,'payload_integrity_failed')
                for child in item.values():
                    walk(child)
            elif isinstance(item,list):
                for child in item:
                    walk(child)
        walk(value)
        return value
