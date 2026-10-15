"""Library and extractive assist share the same current source/tenant boundary."""
import json
import re
import time
from .contracts import APIError,LIMITS,SCHEMAS,canonical,decode_source,digest,validate
from .store import uid
from .parsing import parse_source
from .retrieval import TextRanking,SCORE_MEANINGS

CAPABILITIES = dict(
 library=dict(structural_parsers=['text/plain','text/markdown','text/csv','application/json','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
              retrieval='lexical',conditional_filters=True,native_pdf_adapter=True,local_image_ocr='tesseract_eng_review_required',image_query='pixel_grid_similarity',multimodal_semantic_search=False),
 assist=dict(extractive_quotes=True,bounded_fact_extraction=['maximum_allowable_pressure','power_off_wait','midrail_height'],generative=False,model_used=None,typed_evidence_tasks=True,table_arithmetic='explicit selectors and operations only',source_context_confirmation=True,general_language_decision=False),
 diagnosis=dict(structured_rules=True,live_recognition=False,model_used=None),
 report=dict(json_cell_layout=True,xlsx_pdf_rendering=False,external_submission=False),
 execution=dict(external_calls=0,large_model_loads=0,local_ocr_model='tesseract_eng',mode='local',mock=False),limits=LIMITS)

def family(media):
    return media.split('/')[0] if media.startswith(('image/','video/','audio/')) else 'document'

def tokens(text):
    # Lexical terms plus Japanese character bigrams. This is not an embedding.
    lower=text.casefold()
    terms=set(re.findall(r'[a-z0-9_]+',lower))
    for part in re.findall(r'[\u3040-\u30ff\u3400-\u9fff]+',lower):
        terms.update(part[i:i+2] for i in range(max(1,len(part)-1)))
    return terms

class Library:
    def __init__(self,store,authorize=lambda tenant,actor,revision:True,encoder=None):
        self.store,self.authorize=store,authorize
        self.encoder=encoder
        self.ranking=TextRanking(encoder)
        self.last_search_stats={}
        self.on_source_changed=None
        self.on_delete=None

    def ingest(self,db,ctx,body):
        if 'xlsx_selection' in body:
            if body['media_type']!='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':raise APIError(422,'xlsx_selection_media_mismatch')
            from .xlsx_ingest import selection_ranges
            selection_ranges(body['xlsx_selection'])
        raw=decode_source(body['content_base64'],body['media_type'])
        media=body['media_type']
        signatures={'image/png':b'\x89PNG\r\n\x1a\n','image/jpeg':b'\xff\xd8\xff','audio/wav':b'RIFF'}
        if media in signatures and not raw.startswith(signatures[media]):
            raise APIError(422,'media_signature_mismatch')
        if media=='video/mp4' and (len(raw)<12 or raw[4:8]!=b'ftyp'):
            raise APIError(422,'media_signature_mismatch')
        if media=='audio/wav' and (len(raw)<12 or raw[8:12]!=b'WAVE'):
            raise APIError(422,'media_signature_mismatch')
        tenant=ctx['tenant'];asset=body.get('asset_id') or uid()
        if body.get('asset_id'):
            self.store.check_asset_edit(db,ctx,asset,'write')
            self.store.live(db,tenant,asset)
            version=db.execute('SELECT max(version) FROM sources WHERE tenant=? AND asset=?',(tenant,asset)).fetchone()[0]+1
            if version>100:
                raise APIError(413,'source_version_limit')
        else:
            count=db.execute('SELECT count(*) FROM assets WHERE tenant=?',(tenant,)).fetchone()[0]
            if count>=LIMITS['assets']:
                raise APIError(413,'asset_capacity_limit')
            db.execute('INSERT INTO assets(tenant,id) VALUES(?,?)',(tenant,asset))
            version=1
        total=db.execute('SELECT coalesce(sum(length(raw)),0) FROM sources WHERE tenant=?',(tenant,)).fetchone()[0]
        if total+len(raw)>LIMITS['tenant_source_bytes']:
            raise APIError(413,'tenant_source_byte_limit')
        active=db.execute("SELECT count(*) FROM jobs WHERE tenant=? AND state IN ('queued','running')",(tenant,)).fetchone()[0]
        count=db.execute('SELECT count(*) FROM jobs WHERE tenant=?',(tenant,)).fetchone()[0]
        if active>=LIMITS['active_jobs'] or count>=LIMITS['jobs']:
            raise APIError(413,'job_capacity_limit')
        job=uid()
        meta=dict(name=body['name'],media_type=media,labels=body.get('labels',[]),authorization_revision=ctx['revision'],job_id=job)
        if 'xlsx_selection' in body:meta['xlsx_selection']=body['xlsx_selection']
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',(tenant,asset,version,digest(raw),raw,canonical(meta)))
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (tenant,job,asset,version,ctx['actor'],ctx['revision'],'queued',1,0,0,0,None,None))
        if self.on_source_changed is not None and version>1:
            self.on_source_changed(db,ctx,asset,'source_version_received')
        return 202,dict(asset_id=asset,source_version=version,job=self.store.job(db,tenant,job)),[asset]

    def get_asset(self,db,tenant,id):
        self.store.live(db,tenant,id)
        rows=db.execute('SELECT * FROM sources WHERE tenant=? AND asset=? ORDER BY version',(tenant,id)).fetchall()
        versions=[]
        for row in rows:
            meta=json.loads(row['meta']);job=self.store.job(db,tenant,meta['job_id'])
            versions.append(dict(source_version=row['version'],sha256=row['sha'],bytes=len(row['raw']),**meta,
                                 analysis=dict(state=job['state'],result=job['result'],error=job['error'])))
        return dict(id=id,versions=versions)

    def original(self,db,tenant,id,version):
        self.store.live(db,tenant,id)
        row=db.execute('SELECT raw,sha FROM sources WHERE tenant=? AND asset=? AND version=?',(tenant,id,version)).fetchone()
        if row is None:
            raise APIError(404,'not_found')
        raw=bytes(row['raw'])
        if digest(raw)!=row['sha']:
            raise APIError(409,'source_integrity_failed')
        return raw,row['sha']

    def resolve(self,db,tenant,ref):
        return self._resolve(db,tenant,ref,self.store.parse_cache)

    def _resolve(self,db,tenant,ref,parsed):
        validate(ref,SCHEMAS['Evidence'])
        self.store.live(db,tenant,ref['asset_id'])
        row=db.execute('SELECT data FROM segments WHERE tenant=? AND asset=? AND version=? AND analysis=? AND id=?',
                       (tenant,ref['asset_id'],ref['source_version'],ref['analysis_version'],ref['segment_id'])).fetchone()
        if row is None:
            raise APIError(404,'evidence_not_found')
        source=(tenant,ref['asset_id'],ref['source_version'])
        current_meta=json.loads(db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',source).fetchone()['meta'])
        cached=self.store.extraction_cache.get(source)
        if cached is not None and canonical(cached.get('processor_detail',{}).get('xlsx_selection'))!=canonical(current_meta.get('xlsx_selection')):
            raise APIError(409,'analysis_selection_integrity')
        # Parsed segments alone do not retain the selection contract. Rebuild
        # them when the extraction receipt was evicted independently.
        if cached is None:
            parsed.pop(source,None)
        if source not in parsed:
            raw,sha=self.original(db,tenant,ref['asset_id'],ref['source_version'])
            meta=db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',source).fetchone()
            try:
                result=self.store.extraction_cache.get(source)
                if result is None:
                    result=parse_source(dict(asset=ref['asset_id'],version=ref['source_version'],raw=raw,sha=sha,meta=json.loads(meta['meta'])))
                    self.store.extraction_cache[source]=result
                parsed[source]={s['id']:s for s in result['segments']}
            except (APIError,ValueError,KeyError,TypeError) as error:
                raise APIError(409,'evidence_integrity_failed') from error
        expected=parsed[source].get(ref['segment_id'])
        # Verify the entire deterministic extraction, including CSV/JSON fields,
        # locator, source hash and all four evidence pins. Quotes alone cannot
        # support structured diagnosis or report values.
        try:
            segment=json.loads(row['data'])
            if expected is None or canonical(expected['evidence'])!=canonical(ref) or canonical(segment)!=canonical(expected):
                raise APIError(409,'evidence_integrity_failed')
        except (ValueError,TypeError,RecursionError) as error:
            raise APIError(409,'evidence_integrity_failed') from error
        return expected

    def verify_job(self,db,tenant,job):
        if job['state']!='succeeded':
            return
        source=(tenant,job['asset_id'],job['source_version'])
        expected=self.store.extraction_cache.get(source)
        current_meta=json.loads(db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',source).fetchone()['meta'])
        if expected is not None and canonical(expected.get('processor_detail',{}).get('xlsx_selection'))!=canonical(current_meta.get('xlsx_selection')):
            raise APIError(409,'analysis_selection_integrity')
        if expected is None:
            raw,sha=self.original(db,tenant,source[1],source[2])
            row=db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',source).fetchone()
            try:
                expected=parse_source(dict(asset=source[1],version=source[2],raw=raw,sha=sha,meta=json.loads(row['meta'])))
            except APIError as error:
                raise APIError(409,'analysis_integrity_failed') from error
            self.store.extraction_cache[source]=expected
        if expected['state']!='succeeded' or canonical(job['result'])!=canonical({k:v for k,v in expected.items() if k!='state'}):
            raise APIError(409,'analysis_integrity_failed')

    def search(self,db,tenant,body):
        if body.get('modality','text')!='text':
            raise APIError(501,'multimodal_search_unavailable')
        started=time.perf_counter()
        filters=body.get('filters',{});query=body['query'];terms=tokens(query)
        mode=body.get('ranking_mode','lexical')
        if mode not in SCORE_MEANINGS:
            raise APIError(422,'unsupported_ranking_mode')
        if mode=='hybrid' and self.encoder is None:
            raise APIError(503,'text_embedding_model_unavailable')
        descriptor=dict(retrieval_model_version=self.encoder.metadata) if mode=='hybrid' else {}
        if 'asset_ids' in filters and not filters['asset_ids'] or 'media_types' in filters and not filters['media_types']:
            self.last_search_stats=dict(eligible_segments=0,chunks=0,total_ms=(time.perf_counter()-started)*1000)
            return dict(mode=mode,query=query,filters=filters,results=[],score_meaning=SCORE_MEANINGS[mode],
                        model_used=None,**descriptor)
        rows=db.execute('''SELECT s.data, s.asset,s.version,s.id,s.analysis,v.meta FROM segments s
          JOIN sources v ON v.tenant=s.tenant AND v.asset=s.asset AND v.version=s.version
          JOIN assets a ON a.tenant=s.tenant AND a.id=s.asset
          WHERE s.tenant=? AND a.deleted=0''',(tenant,)).fetchall()
        latest={r['asset']:r['version'] for r in db.execute('SELECT asset,max(version) AS version FROM sources WHERE tenant=? GROUP BY asset',(tenant,))}
        matches=[];candidates=[];source_titles={};eligible_count=0
        for row in rows:
            if not self.store.asset_readable(db,tenant,row['asset']):
                continue
            meta=json.loads(row['meta'])
            if filters.get('versions','latest')=='latest' and row['version']!=latest[row['asset']]:
                continue
            if 'asset_ids' in filters and row['asset'] not in filters['asset_ids'] or 'media_types' in filters and meta['media_type'] not in filters['media_types'] or not set(filters.get('labels',[]))<=set(meta['labels']):
                continue
            eligible_count+=1
            try:
                segment=json.loads(row['data']);search_text=segment['quote']+' '+canonical(segment['fields'])
            except (ValueError,TypeError,KeyError,RecursionError) as error:
                raise APIError(409,'evidence_integrity_failed') from error
            ref=dict(asset_id=row['asset'],source_version=row['version'],analysis_version=row['analysis'],segment_id=row['id'])
            if mode!='lexical':
                segment=self._resolve(db,tenant,ref,self.store.parse_cache)
                source_key=(row['asset'],row['version'])
                if source_key not in source_titles:
                    parsed=self.store.parse_cache[(tenant,row['asset'],row['version'])]
                    first=next(iter(parsed.values()))['quote'] if parsed else ''
                    source_titles[source_key]=next((line.strip()[:160] for line in first.splitlines() if line.strip()),'')
                candidates.append(dict(ref=ref,meta=meta,segment=segment,title=source_titles[(row['asset'],row['version'])]))
                continue
            score=1.0 if query.casefold() in search_text.casefold() else len(terms&tokens(search_text))/max(1,len(terms))
            if score<0.25:
                continue
            matches.append((round(score,6),ref,meta))
        filter_ms=(time.perf_counter()-started)*1000
        if mode!='lexical':
            # All encodings/scoring operate only on the eligible tenant/version
            # set. Explicit empty allowlists never invoke an unfiltered search.
            ranking=self.ranking.rank(tenant,query,candidates,mode) if candidates else []
            matches=[(score,candidates[index]['ref'],candidates[index]['meta']) for index,score in ranking]
        matches.sort(key=lambda s:(-s[0],s[1]['asset_id'],s[1]['segment_id']))
        parsed=self.store.parse_cache;results=[]
        # Bound integrity re-extraction to served results, reusing a source only
        # within this transaction. The pins come from DB columns, not row JSON.
        for score,ref,meta in matches[:body.get('limit',5)]:
            segment=self._resolve(db,tenant,ref,parsed)
            results.append(dict(**segment,retrieval_score=score,media_type=meta['media_type'],labels=meta['labels']))
        self.last_search_stats=dict(self.ranking.stats if mode!='lexical' and candidates else {},
                                    eligible_segments=eligible_count,
                                    filter_and_integrity_ms=filter_ms,total_ms=(time.perf_counter()-started)*1000)
        return dict(mode=mode,query=query,filters=filters,results=results,
                    score_meaning=SCORE_MEANINGS[mode],model_used=self.encoder.metadata['model_id'] if mode=='hybrid' and candidates else None,**descriptor)

    def control_job(self,db,ctx,id,action,body):
        job=self.store.job(db,ctx['tenant'],id)
        self.store.check_asset_edit(db,ctx,job['asset_id'],'write')
        if job['revision']!=body['expected_revision']:
            raise APIError(409,'revision_conflict')
        if action=='cancel':
            if job['state'] not in ('queued','running','blocked','failed'):
                raise APIError(409,'job_state_conflict')
            state,error='cancelled','cancelled_by_actor'
        else:
            if job['state'] not in ('failed','blocked','cancelled') or job['attempt']>=3:
                raise APIError(409,'retry_not_allowed')
            active=db.execute("SELECT count(*) FROM jobs WHERE tenant=? AND state IN ('queued','running')",(ctx['tenant'],)).fetchone()[0]
            if active>=LIMITS['active_jobs']:
                raise APIError(413,'job_capacity_limit')
            state,error='queued',None
        db.execute('UPDATE jobs SET state=?,error=?,result=NULL,lease=0,revision=revision+1,epoch=epoch+1,actor=?,auth_revision=? WHERE tenant=? AND id=?',
                   (state,error,ctx['actor'],ctx['revision'],ctx['tenant'],id))
        return 200,self.store.job(db,ctx['tenant'],id),[job['asset_id']]

    def delete(self,db,tenant,id):
        self.store.live(db,tenant,id)
        ctx=getattr(self.store.request_local,'context',None)
        if ctx is not None and self.on_source_changed is not None:
            self.on_source_changed(db,ctx,id,'source_deleted')
        if self.on_delete is not None:
            self.on_delete(db,tenant,id)
        refs=db.execute('SELECT kind,id FROM dependencies WHERE tenant=? AND asset=?',(tenant,id)).fetchall()
        for ref in refs:
            if ref['kind']=='idempotency':
                # Idempotency keys forbid colons; injected actor identifiers may
                # contain them (for example an identity-provider namespace).
                actor,key=ref['id'].rsplit(':',1)
                db.execute('UPDATE idempotency SET status=410,data=? WHERE tenant=? AND actor=? AND key=?',
                           (canonical(dict(code='deleted_dependency')),tenant,actor,key))
            else:
                db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?',
                           (canonical(dict(id=ref['id'],state='invalidated')),tenant,ref['kind'],ref['id']))
        db.execute('DELETE FROM dependencies WHERE tenant=? AND asset=?',(tenant,id))
        db.execute('DELETE FROM sources WHERE tenant=? AND asset=?',(tenant,id))
        db.execute("UPDATE jobs SET state='cancelled',error='source_deleted',result=NULL,revision=revision+1,epoch=epoch+1,lease=0 WHERE tenant=? AND asset=?",(tenant,id))
        db.execute('UPDATE assets SET deleted=1 WHERE tenant=? AND id=?',(tenant,id))
        self.ranking.remove_asset(tenant,id)
        return 200,dict(asset_id=id,state='deleted',originals_and_derivatives_removed=True),[]

    def run_once(self):
        claim=self.store.claim(self.authorize)
        if claim is None:
            return False
        try:
            if digest(claim['raw'])!=claim['sha']:
                raise APIError(409,'source_integrity_failed')
            result=parse_source(claim)
        except APIError as error:
            result=dict(state='failed',error=error.code,segments=[])
        except Exception:
            result=dict(state='failed',error='processing_failed',segments=[])
        self.store.finish(claim,result,self.authorize)
        return True
