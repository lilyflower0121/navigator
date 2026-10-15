"""Literal reference answers using one immutable published release binding."""
import json
from .contracts import APIError,ID,SCHEMAS,canonical,integer,obj,string,validate
from .governance import etag
from .maintenance import BINDING,P
from .retrieval import SCORE_MEANINGS

RELEASE_ASSIST_SCHEMAS={
    'Phase4ReleaseAnswerInput':obj(dict(binding_id=ID,query=string(1000),limit=integer(1,20)),['binding_id','query']),
    'Phase4ReleaseAnswer':obj(dict(id=ID,project_id=ID,binding=BINDING,answer={'$ref':'#/components/schemas/GroundedAnswer'},
        role=dict(type='string',const='source_reference_answer'),model_used=dict(type='null'),
        notice=string(500))),
}
RELEASE_ASSIST_OPERATIONS=(('post',P+'/release-answers','Phase4ReleaseAnswerInput',201,'read'),
                           ('get',P+'/release-answers/{id}',None,200,'read'))
RELEASE_ASSIST_RESPONSE_SCHEMAS={(method,path):'Phase4ReleaseAnswer' for method,path,*_ in RELEASE_ASSIST_OPERATIONS}


class ReleaseAssist:
    def __init__(self,store,grounded,maintenance):
        self.store,self.grounded,self.maintenance=store,grounded,maintenance

    def _current(self,db,ctx,project,binding):
        row=self.maintenance._release_row(db,ctx,project,binding['release_id'])
        state,_=self.maintenance._read_state(db,ctx,project,row)
        if state!='resolved':
            raise APIError(409,'prerequisites_unverifiable')

    def handle(self,db,ctx,route,params,body,if_match=None):
        project=params['project_id']
        if route.endswith('/{id}'):
            value=self.store.record(db,ctx['tenant'],'release_answers',params['id'])
            if value['project_id']!=project:
                raise APIError(404,'not_found')
            self.verify_response(db,ctx,value,route)
            return 200,value,[],{'ETag':etag(value)}
        validate(body,RELEASE_ASSIST_SCHEMAS['Phase4ReleaseAnswerInput'])
        # The maintenance query captures the binding once, verifies its actual
        # immutable generation and resolves current ACL before serving segments.
        result=self.maintenance.query(db,ctx,project,dict(**body,mode='evidence'))
        binding=result['binding']
        self._current(db,ctx,project,binding)
        matches=[]
        for item in result['results']:
            ref=item['evidence']
            row=db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',
                (ctx['tenant'],ref['asset_id'],ref['source_version'])).fetchone()
            if row is None:
                raise APIError(410,'deleted_dependency')
            meta=json.loads(row['meta'])
            matches.append(dict(**item['segment'],retrieval_score=item['score'],media_type=meta['media_type'],labels=meta['labels']))
        assets=sorted({item['evidence']['asset_id'] for item in matches})
        filters=dict(asset_ids=assets,versions='all')
        request=dict(query=body['query'],limit=body.get('limit',5),filters=filters,ranking_mode='bm25')
        search=dict(mode='bm25',query=body['query'],filters=filters,results=matches,
            score_meaning=SCORE_MEANINGS['bm25'],model_used=None)
        _,answer,_=self.grounded.answer_from_search(db,ctx,request,search,persist=False)
        value=dict(id=answer['id'],project_id=project,binding=binding,answer=answer,role='source_reference_answer',
            model_used=None,notice='公開済み索引の一つの版から抽出した原文の参照回答。現場の安全、合否、実測値、発行者権威を認定しない。')
        # Also bind invisible-to-search required inputs; current revocation of
        # any dependency closes saved results and idempotency receipts.
        release=self.maintenance._release_row(db,ctx,project,binding['release_id'])
        dependencies=[ref['resource_id'] for ref in json.loads(release['data'])['basis']['inputs']]
        self.store.save_record(db,ctx['tenant'],'release_answers',value,dependencies)
        return 201,value,dependencies,{'ETag':etag(value)}

    def verify_response(self,db,ctx,value,route,receipt_replay=False):
        self.maintenance.gov.project_guard(db,ctx,value['project_id'])
        binding=self.maintenance._binding(db,ctx,value['project_id'],value['binding']['id'])
        if canonical(binding)!=canonical(value['binding']):
            raise APIError(409,'binding_integrity_failed')
        self.grounded.verify_saved(db,ctx['tenant'],value['answer'])
        if value['id']!=value['answer']['id']:
            raise APIError(409,'answer_integrity_failed')
        allowed={canonical(chunk['evidence']) for chunk in self.maintenance._verify_index(db,ctx,value['project_id'],
            json.loads(self.maintenance._release_row(db,ctx,value['project_id'],binding['release_id'])['data']))}
        if any(canonical(item['evidence']) not in allowed for item in value['answer']['excerpts']):
            raise APIError(409,'binding_integrity_failed')
        if not receipt_replay:
            self._current(db,ctx,value['project_id'],binding)
        validate(value,RELEASE_ASSIST_SCHEMAS['Phase4ReleaseAnswer'])
        return value
