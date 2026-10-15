"""Actual bounded pixel-grid search. Scores have no semantic/factual meaning."""
import json
from .contracts import APIError,SCHEMAS,canonical,decode_source,validate
from .image_adapter import compute_image_signature
from .parsing import parse_source

class ImageSearch:
    def __init__(self,store,library):
        self.store,self.library=store,library

    def search(self,db,tenant,body):
        filters=body['filters'];ids=filters.get('asset_ids',[])
        if not ids or len(ids)>5 or len(set(ids))!=len(ids):
            raise APIError(422,'image_gallery_requires_one_to_five_assets')
        if filters.get('versions','latest')!='latest':
            raise APIError(422,'image_search_latest_versions_only')
        raw=decode_source(body['content_base64'],body['media_type'])
        query=compute_image_signature(raw,body['media_type'])
        if query.get('state')!='succeeded':
            raise APIError(503,'image_query_capability_unavailable')
        results=[]
        for asset in ids:
            self.store.live(db,tenant,asset)
            row=db.execute('SELECT * FROM sources WHERE tenant=? AND asset=? ORDER BY version DESC LIMIT 1',(tenant,asset)).fetchone()
            meta=json.loads(row['meta'])
            if meta['media_type'] not in ('image/png','image/jpeg'):
                continue
            if 'media_types' in filters and meta['media_type'] not in filters['media_types'] or not set(filters.get('labels',[]))<=set(meta['labels']):
                continue
            job=self.store.job(db,tenant,meta['job_id'])
            if job['state']!='succeeded':
                continue
            original,sha=self.library.original(db,tenant,asset,row['version'])
            expected=parse_source(dict(asset=asset,version=row['version'],raw=original,sha=sha,meta=meta))
            saved=job['result']
            if expected['state']!='succeeded' or canonical({k:v for k,v in expected.items() if k!='state'})!=canonical(saved):
                raise APIError(409,'image_analysis_integrity_failed')
            signature=expected['visual_signature'];values=signature['values']
            if signature['format']!='rgb-grid16-v1':
                raise APIError(409,'image_feature_integrity_failed')
            score=1-sum(abs(a-b) for a,b in zip(values,query['visual_signature']['values']))/(256*255)
            results.append(dict(asset_id=asset,source_version=row['version'],analysis_version=expected['analysis_version'],
                source_sha256=sha,pixel_sha256=expected['pixel_sha256'],image_width=expected['image_width'],image_height=expected['image_height'],
                retrieval_score=round(score,6),media_type=meta['media_type'],labels=meta['labels']))
        results.sort(key=lambda item:(-item['retrieval_score'],item['asset_id']))
        value=dict(mode='pixel_grid_similarity',query_source_sha256=query['source_sha256'],query_pixel_sha256=query['pixel_sha256'],
            feature_format='rgb-grid16-v1',filters=filters,results=results[:body.get('limit',5)],
            score_meaning='1 - mean grayscale grid distance / 255; not semantic similarity or factual confidence',semantic_search=False,model_used=None)
        validate(value,SCHEMAS['ImageSearch'])
        return value
