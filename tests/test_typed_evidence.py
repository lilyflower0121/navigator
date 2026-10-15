"""D1 synthetic arithmetic and API boundary checks; no quality gold is read."""
import base64,io,json,unittest,zipfile
from navigator.api import API,Auth
from navigator.store import Store
from navigator.contracts import APIError,canonical
from tests.test_spreadsheet_source import workbook,MEDIA


def table(two=False):
    rows='<row r="1"><c r="A1" t="str"><v>Item</v></c><c r="B1" t="str"><v>Amount</v></c></row><row r="2"><c r="A2" t="str"><v>Alpha</v></c><c r="B2"><v>12.5</v></c></row><row r="3"><c r="A3" t="str"><v>Beta</v></c><c r="B3"><v>2.5</v></c></row>'
    with zipfile.ZipFile(io.BytesIO(workbook(rows,columns=False))) as z:parts={n:z.read(n).decode() for n in z.namelist()}
    parts['xl/worksheets/sheet1.xml']=parts['xl/worksheets/sheet1.xml'].replace('<mergeCells><mergeCell ref="A1:C1"/></mergeCells>','')
    if two:
        parts['xl/workbook.xml']=parts['xl/workbook.xml'].replace('</sheets>','<sheet name="Second" sheetId="2" r:id="s2"/></sheets>')
        parts['xl/_rels/workbook.xml.rels']=parts['xl/_rels/workbook.xml.rels'].replace('</Relationships>','<Relationship Id="s2" Target="worksheets/sheet2.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>')
        parts['xl/worksheets/sheet2.xml']=parts['xl/worksheets/sheet1.xml'].replace('12.5','25')
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as z:
        for n,v in parts.items():z.writestr(n,v)
    return raw.getvalue()

class EvidenceTaskTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.n=0
        self.api=API(self.store,Auth({'a':dict(tenant='A',actor='reader',revision=1,scopes=['read','write','delete']),'b':dict(tenant='B',actor='reader',revision=1,scopes=['read'])}))
    def tearDown(self):self.store.close()
    def call(self,method,path,body=None,key=None,token='a'):
        self.n+=1
        return self.api.dispatch(method,path,token,body,('phase7-'+key) if key else ('phase7-d1-'+str(self.n) if method in ('POST','DELETE') else None))[1]
    def ingest(self,two=False):
        v=self.call('POST','/v1/assets',dict(name='synthetic table',media_type=MEDIA,content_base64=base64.b64encode(table(two)).decode()));self.api.library.run_once();return v['asset_id']
    def body(self,asset,mode='typed',sheet='Fixture'):
        return dict(query='Item Amount Alpha Beta',asset_ids=[asset],memory_mode=mode,retrieval_policy='active',task=dict(kind='table_calculation',operation='ratio',selectors=[dict(row=r,column='Amount',**(dict(sheet=sheet) if sheet else {})) for r in ('Alpha','Beta')],precision=2))
    def test_same_arithmetic_kernel_flat_and_typed_correct_values_sources(self):
        asset=self.ingest()
        for mode in ('flat','typed'):
            v=self.call('POST','/v1/evidence-tasks',self.body(asset,mode))
            self.assertEqual(v['state'],'answered_calculation');self.assertEqual(v['claims'][0]['value'],'5.00')
            self.assertEqual([o['value'] for o in v['claims'][0]['operands']],['12.5','2.5'])
            self.assertEqual({o['support_evidence']['evidence']['asset_id'] for o in v['claims'][0]['operands']},{asset})
            self.assertEqual(self.call('GET','/v1/evidence-tasks/'+v['id']),v)
    def test_confirmation_sheet_not_answer_then_revision_replay_and_delete(self):
        a=self.ingest(True);body=self.body(a,sheet=None);v=self.call('POST','/v1/evidence-tasks',body,key='create')
        self.assertEqual(v['state'],'needs_confirmation')
        body['task']['selectors']=body['task']['selectors'][:1];body['task']['operation']='identity'
        v=self.call('POST','/v1/evidence-tasks',body);path='/v1/evidence-tasks/'+v['id']
        update=dict(expected_revision=1,observations=[dict(key='sheet:0',status='provided',value='Second')])
        r=self.call('POST',path+'/observations',update,key='sheet');self.assertEqual(r['claims'][0]['value'],'25.00')
        self.assertEqual(r,self.call('POST',path+'/observations',update,key='sheet'))
        self.call('DELETE','/v1/assets/'+a,{})
        with self.assertRaises(APIError) as e:self.call('GET',path)
        self.assertEqual(e.exception.status,410)
    def test_unavailable_model_and_unknown_unit_are_not_fabricated(self):
        a=self.ingest();b=self.body(a);b['task']['unit']='user asserted kg';b['task']['operation']='identity';b['task']['selectors']=b['task']['selectors'][:1]
        v=self.call('POST','/v1/evidence-tasks',b)
        self.assertEqual(v['claims'][0]['unit_status'],'not_semantically_verified');self.assertIsNone(v['generated_answer'])
        with self.assertRaises(APIError) as e:self.call('POST','/v1/evidence-tasks',dict(b,ranking_mode='hybrid'))
        self.assertEqual(e.exception.code,'text_embedding_model_unavailable')
    def test_foreign_and_stale_revision_fail(self):
        a=self.ingest();v=self.call('POST','/v1/evidence-tasks',self.body(a));path='/v1/evidence-tasks/'+v['id']
        with self.assertRaises(APIError) as e:self.call('GET',path,token='b')
        self.assertEqual(e.exception.status,404)
        with self.assertRaises(APIError) as e:self.call('POST',path+'/observations',dict(expected_revision=2,observations=[]))
        self.assertEqual(e.exception.code,'revision_conflict')

if __name__=='__main__':unittest.main()
