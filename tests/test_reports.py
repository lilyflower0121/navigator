"""JSON draft placement using evidence produced from actual input bytes."""
import base64
import csv
import copy
import io
import json
import unittest

from navigator.api import API,Auth
from navigator.contracts import APIError,canonical,digest
from navigator.store import Store


class ReportsTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        scopes=['read','write','delete','diagnose','export']
        self.api=API(self.store,Auth({
            'a':dict(tenant='tenant-a',actor='alice',revision=1,scopes=scopes),
            'b':dict(tenant='tenant-b',actor='bob',revision=1,scopes=scopes),
            'reader':dict(tenant='tenant-a',actor='reader',revision=1,scopes=['read']),
        }))
        self.counter=0

    def tearDown(self):
        self.store.close()

    def post(self,path,body,token='a',key=None):
        self.counter+=1
        return self.api.dispatch('POST',path,token,body,key or f'report-key-{self.counter}')[1]

    def assert_error(self,status,code,fn):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual((caught.exception.status,caught.exception.code),(status,code))

    def template(self,fields=None,raw=None,token='a'):
        if raw is None:
            raw=json.dumps(dict(format='navigator-cell-layout-v1',fields=fields or [
                dict(field='pressure',cell='B4',required=True),
                dict(field='comment',cell='C4',required=False),
                dict(field='inspector',cell='D4',required=True),
            ]),ensure_ascii=False,indent=2).encode()
        result=self.post('/v1/templates',dict(name='検査帳票',content_base64=base64.b64encode(raw).decode()),token)
        return result,raw

    def source(self,raw,media='text/csv',token='a',asset_id=None):
        body=dict(name='合成検査記録',media_type=media,content_base64=base64.b64encode(raw).decode())
        if asset_id:
            body['asset_id']=asset_id
        ingested=self.post('/v1/assets',body,token)
        self.assertTrue(self.api.library.run_once())
        job=self.api.dispatch('GET','/v1/jobs/'+ingested['job']['id'],token)[1]
        self.assertEqual(job['state'],'succeeded')
        return ingested,job['result']['segments']

    def render(self,template,segments,fields=None,token='a',key=None):
        assignments=fields or [('pressure','pressure_mpa'),('comment','comment')]
        return self.post('/v1/reports',dict(template_id=template['id'],assignments=[
            dict(field=field,source_field=source,evidence=segments[0]['evidence'])
            for field,source in assignments]),token,key)

    def test_csv_worker_places_literal_values_and_preserves_original_and_provenance(self):
        template,raw=self.template()
        source,segments=self.source(b'pressure_mpa,comment\r\n0.68,"first line\r\nsecond line"\r\n')
        report=self.render(template,segments)
        self.assertEqual(base64.b64decode(template['original_base64']),raw)
        self.assertEqual(template['version'],digest(raw))
        self.assertEqual(template['source_sha256'],digest(raw))
        self.assertEqual(template['bytes'],len(raw))
        self.assertEqual(template['source_version'],1)
        self.assertEqual(report['template_original_base64'],template['original_base64'])
        self.assertEqual(report['template_source_sha256'],digest(raw))
        pressure,comment,inspector=report['cells']
        self.assertEqual((pressure['field'],pressure['cell'],pressure['value'],pressure['status']),('pressure','B4','0.68','copied'))
        self.assertEqual(pressure['source_field'],'pressure_mpa')
        self.assertEqual(pressure['evidence']['asset_id'],source['asset_id'])
        self.assertEqual(pressure['evidence']['source_version'],1)
        self.assertEqual(pressure['evidence']['analysis_version'],segments[0]['evidence']['analysis_version'])
        self.assertEqual(pressure['source_sha256'],digest(b'pressure_mpa,comment\r\n0.68,"first line\r\nsecond line"\r\n'))
        self.assertEqual(pressure['locator']['line_start'],2)
        self.assertEqual(pressure['locator']['line_end'],3)
        self.assertEqual(pressure['source_quote'],segments[0]['quote'])
        self.assertEqual(comment['value'],'first line\r\nsecond line')
        self.assertIsNone(inspector['value'])
        self.assertIsNone(inspector['evidence'])
        self.assertEqual(inspector['uncertainty_reason'],'not_provided')
        self.assertEqual(report['required_unresolved_fields'],['inspector'])
        self.assertEqual(report['unresolved_fields'],['inspector'])
        self.assertEqual(report['state'],'draft')
        self.assertTrue(report['review_required'])
        self.assertFalse(report['rendered_office_file'])
        self.assertFalse(report['external_submission'])
        saved=self.api.dispatch('GET','/v1/reports/'+report['id'],'a')[1]
        self.assertEqual(saved,report)

    def test_formula_text_is_preserved_and_marked_without_execution(self):
        template,_=self.template([dict(field='literal',cell='A1',required=True)])
        for literal in ('=1+1',' +SUM(A1:A2)','-2','@SUM(A1)','\tordinary','\rordinary','\n=HYPERLINK("https://invalid.test")'):
            with self.subTest(literal=literal):
                buffer=io.StringIO(newline='')
                writer=csv.writer(buffer)
                writer.writerow(['raw'])
                writer.writerow([literal])
                _,segments=self.source(buffer.getvalue().encode())
                report=self.render(template,segments,[('literal','raw')])
                cell=report['cells'][0]
                self.assertEqual(cell['value'],literal)
                self.assertEqual(cell['status'],'needs_review')
                self.assertEqual(cell['value_type'],'literal')
                self.assertTrue(cell['formula_injection_risk'])
                self.assertEqual(report['required_unresolved_fields'],['literal'])

    def test_null_with_actual_json_evidence_remains_unknown(self):
        template,_=self.template([dict(field='literal',cell='A1',required=True)])
        _,segments=self.source(b'{"missing":null}',media='application/json')
        report=self.render(template,segments,[('literal','/missing')])
        cell=report['cells'][0]
        self.assertIsNone(cell['value'])
        self.assertEqual(cell['status'],'unknown')
        self.assertEqual(cell['uncertainty_reason'],'source_value_null')
        self.assertEqual(cell['source_quote'],'null')
        self.assertEqual(cell['source_field'],'/missing')
        self.assertEqual(cell['locator']['pointer'],'/missing')
        self.assertEqual(report['unresolved_fields'],['literal'])
        self.assertEqual(report['required_unresolved_fields'],['literal'])

    def test_json_numbers_and_booleans_keep_type_and_are_copied(self):
        for literal in (0,-2,False):
            with self.subTest(literal=literal):
                template,_=self.template([dict(field='literal',cell='A1',required=True)])
                _,segments=self.source(json.dumps({'value':literal}).encode(),media='application/json')
                cell=self.render(template,segments,[('literal','/value')])['cells'][0]
                self.assertEqual(cell['value'],literal)
                self.assertIs(type(cell['value']),type(literal))
                self.assertEqual(cell['status'],'copied')
                self.assertFalse(cell['formula_injection_risk'])

    def test_duplicate_and_invalid_template_mappings_are_rejected(self):
        for fields in ([dict(field='x',cell='A1',required=True),dict(field='x',cell='B1',required=False)],
                       [dict(field='x',cell='A1',required=True),dict(field='y',cell='A1',required=False)]):
            self.assert_error(422,'duplicate_template_mapping',lambda:self.template(fields))
        for cell in ('A0','a1','$A$1','Sheet1!A1','A1:B1','AAAA1'):
            self.assert_error(422,'invalid_input_text',lambda:self.template([dict(field='x',cell=cell,required=True)]))
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='templates'").fetchone()[0],0)

    def test_assignment_mapping_and_missing_source_field_fail_atomically(self):
        template,_=self.template()
        _,segments=self.source(b'pressure_mpa,comment\n0.68,safe\n')
        self.assert_error(422,'invalid_report_mapping',lambda:self.render(template,segments,[('pressure','pressure_mpa'),('pressure','comment')]))
        self.assert_error(422,'invalid_report_mapping',lambda:self.render(template,segments,[('undefined','pressure_mpa')]))
        self.assert_error(422,'source_field_not_found',lambda:self.render(template,segments,[('pressure','pressure_mpa'),('comment','absent')]))
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='reports'").fetchone()[0],0)

    def test_invalid_evidence_and_cross_tenant_ids_do_not_render(self):
        template,_=self.template()
        _,segments=self.source(b'pressure_mpa,comment\n0.68,safe\n')
        for key,value in [('asset_id','0'*32),('segment_id','0'*32),('analysis_version','unknown'),('source_version',2)]:
            refs=[dict(segments[0],evidence={**segments[0]['evidence'],key:value})]
            expected='not_found' if key=='asset_id' else 'evidence_not_found'
            self.assert_error(404,expected,lambda:self.render(template,refs))
        own_b,_=self.template(token='b')
        self.assert_error(404,'not_found',lambda:self.render(template,segments,token='b'))
        self.assert_error(404,'not_found',lambda:self.render(own_b,segments,token='b'))
        self.assert_error(404,'not_found',lambda:self.api.dispatch('GET','/v1/templates/'+template['id'],'b'))
        self.assert_error(403,'scope_denied',lambda:self.render(template,segments,token='reader'))

    def test_template_mapping_or_original_corruption_is_rejected(self):
        for corrupt in ('mapping','original','hash'):
            with self.subTest(corrupt=corrupt):
                template,_=self.template()
                altered=dict(template)
                if corrupt=='mapping':
                    altered['fields']=[{**f,'cell':'Z99'} for f in template['fields']]
                elif corrupt=='original':
                    altered['original_base64']=base64.b64encode(b'{}').decode()
                else:
                    altered['version']='0'*64
                with self.store.tx() as db:
                    db.execute('UPDATE records SET data=? WHERE kind=? AND id=?',(canonical(altered),'templates',template['id']))
                self.assert_error(409,'template_integrity_failed',lambda:self.post('/v1/reports',dict(template_id=template['id'],assignments=[])))

    def test_new_source_and_template_do_not_rewrite_existing_snapshot(self):
        template,raw=self.template()
        source,segments=self.source(b'pressure_mpa,comment\n0.68,old\n')
        report=self.render(template,segments)
        self.source(b'pressure_mpa,comment\n9.99,new\n',asset_id=source['asset_id'])
        new_template,new_raw=self.template(raw=raw+b'\n')
        self.assertNotEqual(template['id'],new_template['id'])
        self.assertNotEqual(template['version'],new_template['version'])
        self.assertEqual(new_template['source_sha256'],digest(new_raw))
        saved=self.api.dispatch('GET','/v1/reports/'+report['id'],'a')[1]
        self.assertEqual(saved,report)
        self.assertEqual(saved['cells'][0]['value'],'0.68')
        self.assertEqual(saved['cells'][0]['evidence']['source_version'],1)

    def test_source_deletion_invalidates_report_and_idempotent_replay(self):
        template,_=self.template()
        source,segments=self.source(b'pressure_mpa,comment\n0.68,safe\n')
        report=self.render(template,segments,key='stable-render-key')
        self.api.dispatch('DELETE','/v1/assets/'+source['asset_id'],'a',{},'delete-source-key')
        self.assert_error(410,'deleted_dependency',lambda:self.api.dispatch('GET','/v1/reports/'+report['id'],'a'))
        self.assert_error(410,'deleted_dependency',lambda:self.render(template,segments,key='stable-render-key'))
        self.assert_error(410,'source_deleted',lambda:self.render(template,segments))
        with self.store.tx() as db:
            stored=json.loads(db.execute("SELECT data FROM records WHERE kind='reports' AND id=?",(report['id'],)).fetchone()[0])
            self.assertEqual(stored,dict(id=report['id'],state='invalidated'))

    def test_raw_quote_does_not_authorize_forged_structured_value(self):
        template,_=self.template()
        _,segments=self.source(b'pressure_mpa,comment\n0.68,safe\n')
        altered={**segments[0],'fields':{**segments[0]['fields'],'pressure_mpa':'99.99'}}
        with self.store.tx() as db:
            db.execute('UPDATE segments SET data=? WHERE id=?',(canonical(altered),segments[0]['id']))
        self.assert_error(409,'evidence_integrity_failed',lambda:self.render(template,segments))

    def test_saved_draft_integrity_checks_values_and_mapping_against_evidence(self):
        template,_=self.template()
        _,segments=self.source(b'pressure_mpa,comment\n0.68,safe\n')
        report=self.render(template,segments)
        for change in ('value','source_field','source_quote','source_sha256','locator','status','cell','required','unresolved'):
            with self.subTest(change=change):
                altered=copy.deepcopy(report)
                cell=altered['cells'][0]
                if change=='value':
                    cell['value']='99.99'
                elif change=='source_field':
                    cell['source_field']='nonexistent'
                elif change=='source_quote':
                    cell['source_quote']='forged quote'
                elif change=='source_sha256':
                    cell['source_sha256']='0'*64
                elif change=='locator':
                    cell['locator']['line_start']=1
                elif change=='status':
                    cell['status']='unknown'
                elif change=='cell':
                    cell['cell']='Z99'
                elif change=='required':
                    cell['required']=False
                else:
                    altered['unresolved_fields']=[]
                with self.store.tx() as db:
                    self.assert_error(409,'report_integrity_failed',lambda:self.api.reports.verify_draft(db,'tenant-a',altered))

    def test_all_missing_fields_keep_layout_and_nulls(self):
        template,_=self.template()
        report=self.post('/v1/reports',dict(template_id=template['id'],assignments=[]))
        self.assertEqual(report['required_unresolved_fields'],['pressure','inspector'])
        self.assertEqual(report['unresolved_fields'],['pressure','comment','inspector'])
        self.assertEqual([cell['cell'] for cell in report['cells']],['B4','C4','D4'])
        self.assertTrue(all(cell['status']=='unknown' and cell['value'] is None and cell['evidence'] is None for cell in report['cells']))
        with self.store.tx() as db:
            self.api.reports.verify_draft(db,'tenant-a',report)


if __name__=='__main__':
    unittest.main()
