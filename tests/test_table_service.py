"""Synthetic explicit table plans only; no original corpus, gold or model loads."""
import base64
import copy
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from navigator.api import API,Auth
from navigator.contracts import APIError,canonical,digest
from navigator.store import Store
from navigator.xlsx_ingest import MEDIA
from tests.test_spreadsheet_boundaries import workbook


def sheet(values,name='Table'):
    return (name,[(1,[('A','text','Category'),('B','text','Amount')])]+[
        (i,[('A','text',label),('B','number',value)]) for i,(label,value) in enumerate(values,2)])


class TableServiceTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.n=0
        self.api=API(self.store,Auth({'owner':dict(tenant='table-service',actor='owner',revision=1,scopes=['read','write','delete']),
            'foreign':dict(tenant='outside',actor='owner',revision=1,scopes=['read'])}))
    def tearDown(self):self.store.close()
    def call(self,method,path,body=None,key=None,token='owner'):
        self.n+=1
        return self.api.dispatch(method,path,token,body,key or ('table-service-'+str(self.n) if method=='POST' or method=='DELETE' else None))[1]
    def ingest(self,sheets,finish=True):
        raw=workbook(sheets);value=self.call('POST','/v1/assets',dict(name='Synthetic explicit table',media_type=MEDIA,content_base64=base64.b64encode(raw).decode()))
        if finish:
            self.assertTrue(self.api.library.run_once())
            self.assertEqual(self.call('GET','/v1/jobs/'+value['job']['id'])['state'],'succeeded')
        return value['asset_id'],digest(raw)
    def selector(self,row,column='Amount',sheet='Table'):
        return dict(row=row,column=column,**(dict(sheet=sheet) if sheet else {}))
    def body(self,asset,rows,**changes):
        task=dict(kind='table_calculation',operation='sum',selectors=[self.selector(r) for r in rows]);task.update(changes)
        return dict(query='Unrelated words that do not retrieve the requested row',asset_ids=[asset],retrieval_policy='conditional',task=task)
    def create(self,body,key=None):return self.call('POST','/v1/evidence-tasks',body,key)
    def assert_answer(self,value,expected):
        self.assertEqual(value['state'],'answered_calculation',value)
        self.assertEqual([Decimal(c['value']) for c in value['claims']],[Decimal(str(v)) for v in expected])
        self.assertEqual(value['stages']['completion_scope'],'explicit_task_only')
        self.assertTrue(value['stages']['explicit_task_complete'])
    def error(self,code,fn):
        with self.assertRaises(APIError) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def test_exact_conditional_lookup_three_values_includes_zero_without_ranking(self):
        asset,sha=self.ingest([('Table',[(1,[('A','text','Category'),('B','text','North'),('C','text','South'),('D','text','Count')]),
            (2,[('A','text','Target'),('B','number',0),('C','number',13),('D','number',7)])])])
        body=self.body(asset,[],kind='table_lookup',operation='identity',selectors=[self.selector('Target',c) for c in ('North','South','Count')])
        body.update(memory_mode='flat',ranking_mode='hybrid')
        with patch.object(self.api.library,'search',side_effect=AssertionError('conditional must not perform ranked retrieval')):
            result=self.create(body)
            self.assertEqual(self.call('GET','/v1/evidence-tasks/'+result['id']),result)
        self.assert_answer(result,[0,13,7]);self.assertIsNone(result['model_used'])
        self.assertEqual(result['stages']['memory_kind'],'typed');self.assertEqual(result['stages']['requested_memory_mode'],'flat')
        stats=result['stages']['retrieval_stats'][0]
        self.assertEqual(stats['matching_rows'],1);self.assertEqual(stats['selected_rows'],1)
        self.assertEqual(result['budgets']['context_characters'],sum(len(s['quote']) for s in result['excerpts']))
        for c in result['claims']:self.assertEqual(c['operands'][0]['support_evidence']['source_sha256'],sha)

    def test_sum_four_and_compare_original_parent_not_lexical_top_k(self):
        asset,sha=self.ingest([sheet([('Alpha',2),('Beta',3),('Gamma',4),('Delta',5),('Published',14)])])
        body=self.body(asset,['Alpha','Beta','Gamma','Delta'],precision=0,compare_to=self.selector('Published'))
        result=self.create(body,key='table-service-parent')
        self.assert_answer(result,[14]);claim=result['claims'][0]
        self.assertEqual(claim['comparison']['result'],'equal')
        self.assertEqual(claim['comparison']['target']['value'],'14')
        self.assertEqual(claim['comparison']['target']['support_evidence']['source_sha256'],sha)
        self.assertEqual(len(claim['operands']),4)
        self.assertEqual(result['stages']['retrieval_stats'][0]['matching_rows'],5)
        self.assertEqual(self.create(body,key='table-service-parent'),result)

    def test_ratio_explicit_scale_then_precision_and_published_integer_comparison(self):
        asset,_=self.ingest([sheet([('Part',17),('Whole',300),('Published',567)])])
        body=self.body(asset,['Part','Whole'],operation='ratio',unit='per ten thousand',scale='10000',precision=3,
            compare_to=self.selector('Published'),comparison_precision=0)
        result=self.create(body);self.assert_answer(result,['566.667'])
        claim=result['claims'][0];comparison=claim['comparison']
        self.assertEqual(comparison['compared_value'],'567');self.assertEqual(comparison['result'],'equal')
        self.assertEqual([o['value'] for o in claim['operands']],['17','300'])
        self.assertEqual(claim['constants'],[dict(kind='requested_constant',role='scale',value='10000',provenance='explicit_task_input',source_evidence=None)])
        self.assertEqual(claim['unit_status'],'not_semantically_verified')

    def test_comparison_precision_uses_unrounded_scaled_result_and_different_is_complete(self):
        asset,_=self.ingest([sheet([('Precise','.145'),('Published','.1'),('Other','.2')])])
        result=self.create(self.body(asset,['Precise'],operation='identity',scale='1',precision=2,
            compare_to=self.selector('Published'),comparison_precision=1))
        self.assert_answer(result,['.15']);self.assertEqual(result['claims'][0]['comparison']['result'],'equal')
        different=self.create(self.body(asset,['Precise'],operation='identity',scale='1',precision=2,
            compare_to=self.selector('Other'),comparison_precision=1))
        self.assert_answer(different,['.15']);self.assertEqual(different['claims'][0]['comparison']['result'],'different')

    def test_unitful_ratio_and_percentage_point_require_explicit_scale(self):
        asset,_=self.ingest([sheet([('First',2718),('Second',2600)])])
        for op in ('ratio','percentage_point'):
            body=self.body(asset,['First','Second'],operation=op,unit='requested units')
            held=self.create(body);self.assertEqual(held['reason'],'unit_conversion_plan_not_implemented');self.assertEqual(held['claims'],[])
        scaled=self.create(self.body(asset,['First','Second'],operation='percentage_point',unit='percentage points',scale='.01',precision=2))
        self.assert_answer(scaled,['1.18'])

    def test_zero_scale_zero_numerator_and_zero_denominator(self):
        asset,_=self.ingest([sheet([('Zero',0),('Other',9)])])
        self.assert_answer(self.create(self.body(asset,['Other'],operation='identity',scale='0')),[0])
        self.assert_answer(self.create(self.body(asset,['Zero','Other'],operation='ratio',scale='100')),[0])
        held=self.create(self.body(asset,['Other','Zero'],operation='ratio',scale='100'))
        self.assertEqual(held['claims'],[]);self.assertFalse(held['stages']['explicit_task_complete'])

    def test_row_context_and_memory_cap_overflow_are_explicit_incomplete(self):
        asset,_=self.ingest([sheet([('Duplicate',i) for i in range(21)])])
        result=self.create(self.body(asset,['Duplicate']))
        self.assertEqual(result['reason'],'conditional_row_cap_exceeded');self.assertEqual(result['state'],'retrieval_incomplete')
        self.assertEqual(result['excerpts'],[]);self.assertEqual(result['stages']['retrieval_stats'][0]['matching_rows'],21)
        rows=[(1,[('A','text','Category'),('B','text','Amount')])]+[(i,[('A','text','Long'),('B','number',i),('C','text','x'*800)]) for i in range(2,14)]
        asset,_=self.ingest([('Table',rows)])
        result=self.create(self.body(asset,['Long']))
        self.assertEqual(result['reason'],'conditional_context_cap_exceeded');self.assertEqual(result['excerpts'],[])
        rows=[(1,[('A','text','Category'),('B','text','Amount')])]+[(i,[('A','text','Wide')]+[(chr(66+c),'number',c) for c in range(15)]) for i in range(2,22)]
        asset,_=self.ingest([('Table',rows)])
        result=self.create(self.body(asset,['Wide']))
        self.assertEqual(result['reason'],'conditional_memory_cap_exceeded');self.assertTrue(result['stages']['memory_truncated'])
        self.assertEqual(result['claims'],[])

    def test_period_source_confirmation_and_compare_sheet_confirmation_remain_controls(self):
        asset,_=self.ingest([('Table',[(1,[('A','text','2031年基準'),('B','text','Amount')]),(2,[('A','text','Target'),('B','number',4)])])])
        body=self.body(asset,['Target'],operation='identity',as_of='2030年基準')
        self.assertEqual(self.create(body)['reason'],'requested_valid_time_unverified')
        body['task']['as_of']='2031年基準';body['task']['selectors'][0]['source_title']='Caller alias'
        pending=self.create(body);self.assertEqual(pending['confirmation']['key'],'source_reference')
        answered=self.call('POST','/v1/evidence-tasks/'+pending['id']+'/observations',dict(expected_revision=1,
            observations=[dict(key='source_reference',status='provided',value=asset)]))
        self.assert_answer(answered,[4])
        asset,_=self.ingest([sheet([('Source',4),('Published',4)]),sheet([('Published',5)],'Other')])
        body=self.body(asset,['Source'],operation='identity',compare_to=self.selector('Published',sheet=None))
        pending=self.create(body);self.assertEqual(pending['confirmation']['key'],'sheet:compare_to')
        answered=self.call('POST','/v1/evidence-tasks/'+pending['id']+'/observations',dict(expected_revision=1,
            observations=[dict(key='sheet:compare_to',status='provided',value='Other')]))
        self.assert_answer(answered,[4]);self.assertEqual(answered['claims'][0]['comparison']['result'],'different')

    def test_extraction_not_ready_has_priority_over_context_and_conditional_matching(self):
        asset,_=self.ingest([sheet([('Target',4)])],finish=False)
        result=self.create(self.body(asset,['Target'],as_of='unknown',operation='identity'))
        self.assertEqual(result['state'],'extraction_incomplete');self.assertEqual(result['reason'],'source_processing_not_complete_or_supported')
        self.assertEqual(result['budgets']['searches'],0);self.assertEqual(result['claims'],[])

    def test_source_acl_receipt_delete_and_tampered_comparison_are_checked(self):
        asset,_=self.ingest([sheet([('Target',4),('Published',4)])])
        body=self.body(asset,['Target'],operation='identity',compare_to=self.selector('Published'))
        result=self.create(body,key='table-service-source-controls');path='/v1/evidence-tasks/'+result['id']
        self.error('not_found',lambda:self.call('GET',path,token='foreign'))
        self.store.resource_access=lambda db,tenant,actor,aid:False
        self.error('permission_revoked',lambda:self.call('GET',path))
        self.error('permission_revoked',lambda:self.create(body,key='table-service-source-controls'))
        self.store.resource_access=self.api.governance.asset_allowed
        with self.store.tx() as db:
            sid=digest([result['id'],1])[:32]
            snapshot=self.store.record(db,'table-service','evidence_task_snapshots',sid)
            snapshot['response']['claims'][0]['comparison']['result']='different'
            db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?',
                (canonical(snapshot),'table-service','evidence_task_snapshots',sid))
        self.error('evidence_task_decision_integrity',lambda:self.call('GET',path))
        self.call('DELETE','/v1/assets/'+asset,{})
        self.error('deleted_dependency',lambda:self.call('GET',path))
        self.error('deleted_dependency',lambda:self.create(body,key='table-service-source-controls'))

    def test_invalid_scale_paths_and_out_of_scope_assets_never_supply_values(self):
        asset,_=self.ingest([sheet([('Target',4)])])
        for scale in ('NaN','Infinity','1_0',1.5):
            with self.subTest(scale=scale),self.assertRaises(APIError):self.create(self.body(asset,['Target'],scale=scale))
        bound=self.create(self.body(asset,['Target'],scale='1e301'))
        self.assertEqual(bound['reason'],'arithmetic_numeric_bound');self.assertEqual(bound['claims'],[])
        body=self.body(asset,['Target'],compare_to=dict(row='Target',column='Amount',source_path='/untrusted/path'))
        self.error('invalid_input_fields',lambda:self.create(body))
        result=self.call('POST','/v1/evidence-tasks',self.body(asset,['Target']),token='foreign')
        self.assertEqual(result['scope'],[]);self.assertEqual(result['claims'],[])


if __name__=='__main__':unittest.main()
