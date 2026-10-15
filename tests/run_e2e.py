"""Save an inspectable real HTTP run over synthetic source files; no AI calls."""
import argparse
import base64
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from .test_http_e2e import HTTPIntegration,ROOT
from navigator.contracts import digest

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='evidence/http-e2e.json')
    args=parser.parse_args()
    path=Path(args.output)
    if path.exists():
        raise FileExistsError('Use a fresh evidence filename; previous runs are preserved.')
    run=HTTPIntegration();run.setUp();start=time.monotonic()
    try:
        raw,body,source,job=run.ingest('inspection.csv','text/csv','e2e-source-0001')
        _,_,secondary,secondary_job=run.ingest('handover.txt','text/plain','e2e-source-0002')
        ref=job['result']['segments'][0]['evidence']
        search=run.request('POST','/v1/search',dict(query='PIPE-012',filters=dict(labels=['synthetic'],media_types=['text/csv'])))
        answerbody=dict(query='PIPE-012 の pressure_mpa',filters=dict(asset_ids=[source['asset_id']]),mode='extractive')
        answer=run.request('POST','/v1/answers',answerbody,201,'e2e-answer-0001')
        original=run.request('GET',f"/v1/assets/{source['asset_id']}/versions/1/original",decode=False)
        assert original==raw
        process=run.request('POST','/v1/processes',dict(name='synthetic pressure check',required_media=['document'],max_delay_ms=10000,
                            criterion=dict(field='pressure_mpa',operator='gte',threshold=0.70,unit='MPa')),201)
        common=dict(process_id=process['id'],stream_id='synthetic-e2e',event_time=datetime.now(timezone.utc).isoformat(),input_status='complete',inputs=[ref])
        diagnosis=run.request('POST','/v1/diagnoses',dict(**common,sequence=1,mode='structured_rules'),201,'e2e-diag-0001')
        held=run.request('POST','/v1/diagnoses',dict(**common,sequence=2,mode='recognition'),201,'e2e-diag-0002')
        template_raw=(ROOT/'fixtures/report-template.json').read_bytes()
        template=run.request('POST','/v1/templates',dict(name='synthetic original template',content_base64=base64.b64encode(template_raw).decode()),201,'e2e-template-0001')
        reportbody=dict(template_id=template['id'],assignments=[dict(field='pressure',source_field='pressure_mpa',evidence=ref)])
        report=run.request('POST','/v1/reports',reportbody,201,'e2e-report-0001')
        run.host.close();run.store.close();run.start()
        persisted=run.request('GET','/v1/answers/'+answer['id'])
        assert persisted==answer
        run.request('DELETE','/v1/assets/'+source['asset_id'],{},200,'e2e-delete-0001')
        deleted={}
        for kind,value in [('answers',answer),('diagnoses',diagnosis),('reports',report)]:
            error=run.request('GET','/v1/'+kind+'/'+value['id'],status=410)
            deleted[kind]=error['code']
        deleted['original']=run.request('GET',f"/v1/assets/{source['asset_id']}/versions/1/original",status=410)['code']
        deleted['replayed_answer']=run.request('POST','/v1/answers',answerbody,410,'e2e-answer-0001')['code']
        deleted['replayed_report']=run.request('POST','/v1/reports',reportbody,410,'e2e-report-0001')['code']
        assert run.request('POST','/v1/search',dict(query='PIPE-012'))['results']==[]
        assert b'PIPE-012' not in run.db.read_bytes()
        value=dict(executed_at=datetime.now(timezone.utc).isoformat(),duration_seconds=round(time.monotonic()-start,4),
                   synthetic_only=True,execution_kind='actual_bytes_http_sqlite_parser',mock_ai=False,external_calls=0,model_loads=0,
                   source=dict(file='fixtures/inspection.csv',sha256=digest(raw),bytes=len(raw),asset_id=source['asset_id'],version=1),
                   secondary_source=dict(file='fixtures/handover.txt',job_state=secondary_job['state'],asset_id=secondary['asset_id']),
                   job=job,search=search,answer=answer,diagnosis=diagnosis,recognition_hold=held,report=report,
                   assertions=dict(original_bytes_equal=True,restart_answer_equal=True,source_delete_removes_search=True,sqlite_cleartext_marker_removed=True),
                   deletion=deleted,resources_closed=True,limitations=['No generative model','No multimodal semantic retrieval','No live perception','JSON layout only'])
    finally:
        run.tearDown()
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as output:
        output.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print('Saved synthetic HTTP E2E evidence: '+str(path))

if __name__=='__main__':
    main()
