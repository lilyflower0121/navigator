"""Synthetic boundary checks, not an embedding-quality benchmark."""
from array import array
import base64
import math
import unittest
from navigator.api import API, Auth
from navigator.contracts import APIError, SCHEMAS, validate
from navigator.retrieval import CHUNK_POLICY, FUSION, bm25, checked_vectors, chunks
from navigator.store import Store


class DeterministicVectorFixture:
    """Software fixture only. Vector fixtures do not establish embedding quality."""
    metadata=dict(model_id='synthetic-software-fixture',revision='fixture-v1',dimension=2,
                  precision='float32',query_prefix='q: ',document_prefix='d: ',
                  chunk_policy=CHUNK_POLICY,metric='cosine',fusion=FUSION)
    def __init__(self):
        self.inputs=[]
        self.on_query=lambda: None
    def documents(self,documents):
        self.inputs.extend(documents)
        return [[1.0,0.0] if 'pump' in text else [0.0,1.0] for _,text in documents]
    def query(self,text):
        self.on_query()
        return [[1.0,0.0]]


class TextRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.encoder=DeterministicVectorFixture()
        scopes=['read','write','delete','export']
        self.auth=Auth({'owner':dict(tenant='one',actor='a',revision=1,scopes=scopes),
                        'other':dict(tenant='two',actor='a',revision=1,scopes=scopes)})
        self.api=API(self.store,self.auth,self.encoder);self.key=0
    def tearDown(self):
        self.store.close()
    def post(self,path,body,token='owner'):
        self.key+=1
        return self.api.dispatch('POST',path,token,body,f'fixture-key-{self.key}')[1]
    def ingest(self,text,token='owner',asset=None):
        body=dict(name='DO NOT ENCODE THIS FILENAME pump',media_type='text/plain',
                  content_base64=base64.b64encode(text.encode()).decode())
        if asset:body['asset_id']=asset
        result=self.post('/v1/assets',body,token);self.api.library.run_once()
        return result['asset_id']
    def search(self,**kw):
        return self.post('/v1/search',dict(query='pump',ranking_mode='hybrid',**kw))
    def test_bm25_terms_frequency_empty_and_real_parent_quote(self):
        scores=bm25('pump',['pump pump','valve'])
        self.assertGreater(scores[0],0);self.assertEqual(scores[1],0)
        self.assertEqual(bm25('pump',[]),[])
        asset=self.ingest('title\npump inspected')
        result=self.post('/v1/search',dict(query='pump',ranking_mode='bm25'))
        validate(result,SCHEMAS['Search'])
        self.assertEqual(result['results'][0]['quote'],'pump inspected')
        self.assertIsNone(result['model_used'])
    def test_explicit_empty_allowlists_never_encode_or_broaden(self):
        self.ingest('pump')
        for mode in ('lexical','bm25','hybrid'):
            for field in ('asset_ids','media_types'):
                result=self.post('/v1/search',dict(query='pump',ranking_mode=mode,filters={field:[]}))
                self.assertEqual(result['results'],[]);self.assertIsNone(result['model_used'])
        self.assertEqual(self.encoder.inputs,[])
    def test_tenant_filter_and_current_version_precede_encoding(self):
        asset=self.ingest('pump old');self.ingest('secret foreign pump','other')
        self.ingest('valve new',asset=asset)
        result=self.search(filters=dict(asset_ids=[asset]))
        self.assertTrue(result['results'])
        self.assertTrue(all(r['evidence']['source_version']==2 for r in result['results']))
        self.assertTrue(all('old' not in text and 'foreign' not in text for _,text in self.encoder.inputs))
        self.assertTrue(all('FILENAME' not in title for title,_ in self.encoder.inputs))
    def test_filter_excluded_corruption_does_not_enter_encoder(self):
        good=self.ingest('pump');bad=self.ingest('valve')
        with self.store.tx() as db:
            db.execute('UPDATE segments SET data=? WHERE tenant=? AND asset=?',('broken','one',bad))
        self.assertTrue(self.search(filters=dict(asset_ids=[good]))['results'])
    def test_corrupt_cached_vectors_are_rejected_and_isolated(self):
        self.ingest('pump');self.search()
        cache=self.api.library.ranking.cache
        cache[next(iter(cache))]=array('f',[math.nan,0])
        with self.assertRaisesRegex(APIError,'retrieval_vector_integrity_failed'):self.search()
        self.assertFalse(cache)
        self.assertTrue(self.search()['results'])
    def test_shape_dimension_norm_and_non_finite_are_fail_closed(self):
        for vectors,count,dim in [([[1,0]],2,2),([[1]],1,2),([[0,0]],1,2),([[math.inf,0]],1,2),([[2,0]],1,2)]:
            with self.assertRaises(APIError):checked_vectors(vectors,count,dim)
    def test_revision_change_never_reuses_old_vector_cache(self):
        self.ingest('pump');self.search();old=len(self.encoder.inputs)
        self.encoder.metadata={**self.encoder.metadata,'revision':'fixture-v2'}
        self.search();self.assertGreater(len(self.encoder.inputs),old)
    def test_delete_removes_vectors_and_invalidates_saved_derived_result(self):
        asset=self.ingest('pump');self.search()
        answer=self.post('/v1/answers',dict(query='pump',ranking_mode='hybrid'))
        self.key+=1
        self.api.dispatch('DELETE','/v1/assets/'+asset,'owner',{},f'delete-key-{self.key}')
        self.assertFalse(self.api.library.ranking.cache)
        self.assertEqual(self.search()['results'],[])
        with self.assertRaisesRegex(APIError,'deleted_dependency'):
            self.api.dispatch('GET','/v1/answers/'+answer['id'],'owner')
    def test_revocation_during_encoding_cannot_return_or_persist(self):
        self.ingest('pump')
        self.encoder.on_query=lambda:self.auth.revoke('owner')
        with self.assertRaisesRegex(APIError,'authentication_required'):self.search()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM records').fetchone()[0],0)
    def test_chunk_preserves_coverage_and_unicode_offsets(self):
        text='あ'*900
        parts=chunks(text)
        self.assertEqual(parts[0],(0,400,'あ'*400));self.assertEqual(parts[-1][1],900)
        for start,end,part in parts:self.assertEqual(text[start:end],part)

if __name__=='__main__':unittest.main()
