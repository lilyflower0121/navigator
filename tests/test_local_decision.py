"""Parser/boundary checks; these do not simulate or claim model quality."""
import json,unittest
from navigator.local_decision import parse_candidate
from navigator.contracts import digest
class DecisionCitationBoundaries(unittest.TestCase):
    def setUp(self):
        self.text='受付は午前9時からです。土日は休業です。'
        self.source=dict(evidence=dict(asset_id='a'*32,source_version=1,analysis_version='v1',segment_id='b'*32),source_sha256=digest(self.text.encode()),locator=dict(kind='text',start_line=1,end_line=1),quote=self.text)
    def parse(self,value):return parse_candidate(json.dumps(value,ensure_ascii=False),[self.source])
    def test_valid_quote_is_candidate_not_semantic_proof(self):
        r=self.parse(dict(status='answer',answer='土曜も開いているという誤った主張',citations=[dict(index=1,quote='土日は休業です。')]))
        self.assertEqual(r['status'],'candidate');self.assertFalse(r['semantic_support_verified'])
        p=r['supports'][0];self.assertEqual(self.text[p['start_char']:p['end_char']],p['quote'])
    def test_invented_quote_and_foreign_index_are_rejected(self):
        for index,quote in [(1,'午前3時'),(2,'土日は休業です。'),(True,'土日は休業です。'),(1,' ')]:
            self.assertEqual(self.parse(dict(status='answer',answer='test',citations=[dict(index=index,quote=quote)]))['status'],'rejected')
    def test_unknown_does_not_prove_absence(self):
        r=self.parse(dict(status='unknown',answer=None,citations=[]));self.assertEqual(r['reason'],'model_declined_no_source_absence_proof')
    def test_json_and_budget_boundaries(self):
        for raw in ['```json\n{}\n```','{} trailing','{"status":"unknown","status":"answer","answer":null,"citations":[]}','[]']:
            self.assertEqual(parse_candidate(raw,[self.source])['status'],'rejected')
        for v in [dict(status='answer',answer='x'*1201,citations=[dict(index=1,quote='受付')]),dict(status='answer',answer='x',citations=[]),dict(status='unknown',answer='invented',citations=[])]:
            self.assertEqual(self.parse(v)['status'],'rejected')
if __name__=='__main__':unittest.main()
