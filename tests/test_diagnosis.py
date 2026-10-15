"""Diagnosis integration with synthetic bytes, real ingestion and worker parsing."""
import base64
import copy
from datetime import datetime,timezone,timedelta
import unittest

from navigator.api import API,Auth
from navigator.contracts import APIError,canonical,digest
from navigator.store import Store


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        scopes=['read','write','diagnose','delete','export']
        self.api=API(self.store,Auth({
            'a':dict(tenant='a',actor='operator',revision=1,scopes=scopes),
            'b':dict(tenant='b',actor='operator',revision=1,scopes=scopes),
        }))
        self.now=datetime(2026,10,13,12,tzinfo=timezone.utc)
        self.api.diagnosis.clock=lambda:self.now
        self.keys=0

    def tearDown(self):
        self.store.close()

    def post(self,path,body,token='a',key=None):
        self.keys+=1
        status,value,_=self.api.dispatch('POST',path,token,body,key or f'diag_key_{self.keys}')
        return status,value

    def process(self,**changes):
        body=dict(name='Synthetic pressure rule',required_media=['document'],max_delay_ms=1000,
                  criterion=dict(field='pressure_mpa',operator='gte',threshold=.65,unit='MPa'))
        body.update(changes)
        return self.post('/v1/processes',body)[1]

    def inputs(self,raw=b'pressure_mpa,unit\n0.68,MPa\n',media='text/csv',token='a'):
        status,ingested=self.post('/v1/assets',dict(name='Synthetic source',media_type=media,
            content_base64=base64.b64encode(raw).decode()),token)
        self.assertEqual(status,202)
        self.assertEqual(ingested['job']['state'],'queued')
        self.assertTrue(self.api.library.run_once())
        _,job,_=self.api.dispatch('GET',f"/v1/jobs/{ingested['job']['id']}",token)
        self.assertEqual(job['state'],'succeeded')
        self.assertIsNone(job['model_used'])
        return [s['evidence'] for s in job['result']['segments']],ingested

    def diagnose(self,process,inputs,**changes):
        body=dict(process_id=process['id'],stream_id='synthetic-stream',sequence=1,
            event_time=(self.now-timedelta(milliseconds=500)).isoformat(),input_status='complete',
            inputs=inputs,mode='structured_rules')
        body.update(changes)
        return self.post('/v1/diagnoses',body)[1]

    def assert_error(self,status,code,fn):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual((caught.exception.status,caught.exception.code),(status,code))

    def test_real_csv_rule_preserves_raw_unit_and_pinned_evidence(self):
        process=self.process()
        refs,ingested=self.inputs()
        result=self.diagnose(process,refs)
        self.assertEqual(result['decision'],'meets_rule')
        self.assertEqual(result['hold_reasons'],[])
        self.assertEqual(result['event_time'],'2026-10-13T11:59:59.500000+00:00')
        self.assertEqual(result['received_time'],self.now.isoformat())
        self.assertEqual(result['delay_ms'],500)
        self.assertEqual(result['business_approval'],'not_granted')
        self.assertIsNone(result['model_used'])
        observation=result['observations'][0]
        self.assertEqual(observation['original_value'],'0.68')
        self.assertEqual(observation['value'],.68)
        self.assertEqual(observation['unit'],'MPa')
        self.assertEqual(observation['unit_source'],'unit')
        self.assertEqual(observation['original_units'],{'unit':'MPa'})
        self.assertEqual(observation['evidence'],refs[0])
        with self.store.tx() as db:
            source=self.api.library.resolve(db,'a',refs[0])
        self.assertEqual(source['source_sha256'],digest(b'pressure_mpa,unit\n0.68,MPa\n'))
        self.assertEqual(source['locator']['line_start'],2)
        self.assertEqual(result['process_version'],digest({k:v for k,v in process.items() if k not in ('id','version')}))
        _,saved,_=self.api.dispatch('GET',f"/v1/diagnoses/{result['id']}",'a')
        self.assertEqual(saved,result)

    def test_rule_operator_outside_is_not_business_approval(self):
        process=self.process(criterion=dict(field='pressure_mpa',operator='lte',threshold=.65,unit='MPa'))
        refs,_=self.inputs()
        result=self.diagnose(process,refs)
        self.assertEqual(result['decision'],'outside_rule')
        self.assertEqual(result['business_approval'],'not_granted')

    def test_unit_sources_unknown_conflicting_and_mismatched_hold(self):
        cases=[
            (b'pressure_mpa\n0.68\n','measurement_unit_missing',None,{}),
            (b'pressure_mpa,unit\n0.68,kPa\n','measurement_unit_mismatch','kPa',{'unit':'kPa'}),
            (b'pressure_mpa,pressure_mpa_unit,unit\n0.68,MPa,kPa\n','measurement_unit_conflict',None,
             {'pressure_mpa_unit':'MPa','unit':'kPa'}),
            (b'pressure_mpa,unit\n0.68,\n','measurement_unit_missing',None,{'unit':''}),
        ]
        process=self.process()
        for seq,(raw,reason,unit,original_units) in enumerate(cases,1):
            with self.subTest(reason=reason,raw=raw):
                refs,_=self.inputs(raw)
                result=self.diagnose(process,refs,sequence=seq)
                self.assertEqual(result['decision'],'hold')
                self.assertIn(reason,result['hold_reasons'])
                self.assertEqual(result['observations'][0]['unit'],unit)
                self.assertEqual(result['observations'][0]['original_units'],original_units)

    def test_field_specific_unit_and_identical_unit_columns_are_supported(self):
        for raw in [b'pressure_mpa,pressure_mpa_unit\n0.68,MPa\n',
                    b'pressure_mpa,pressure_mpa_unit,unit\n0.68,MPa,MPa\n']:
            with self.subTest(raw=raw):
                refs,_=self.inputs(raw)
                result=self.diagnose(self.process(),refs)
                self.assertEqual(result['decision'],'meets_rule')
                self.assertEqual(result['observations'][0]['unit_source'],'pressure_mpa_unit')

    def test_unparseable_and_non_finite_values_remain_raw_and_hold(self):
        process=self.process()
        for seq,raw_value in enumerate(['NaN','Inf','-Infinity','true','false','not a number',''],1):
            with self.subTest(raw_value=raw_value):
                refs,_=self.inputs(f'pressure_mpa,unit\n{raw_value},MPa\n'.encode())
                result=self.diagnose(process,refs,sequence=seq)
                self.assertEqual(result['decision'],'hold')
                self.assertIn('unparseable_measurement',result['hold_reasons'])
                self.assertEqual(result['observations'][0]['original_value'],raw_value)
                self.assertIsNone(result['observations'][0]['value'])

    def test_json_boolean_is_not_numeric_measurement(self):
        refs,_=self.inputs(b'{"pressure":true}',media='application/json')
        process=self.process(criterion=dict(field='/pressure',operator='gte',threshold=0,unit='MPa'))
        result=self.diagnose(process,refs)
        self.assertEqual(result['decision'],'hold')
        self.assertIn('unparseable_measurement',result['hold_reasons'])
        self.assertIs(result['observations'][0]['original_value'],True)
        self.assertIsNone(result['observations'][0]['value'])

    def test_conflicting_numeric_measurements_hold(self):
        refs,_=self.inputs(b'pressure_mpa,unit\n0.68,MPa\n0.69,MPa\n')
        result=self.diagnose(self.process(),refs)
        self.assertEqual(result['decision'],'hold')
        self.assertIn('conflicting_measurements',result['hold_reasons'])
        self.assertEqual(len(result['observations']),2)

    def test_csv_decimal_precision_survives_threshold_and_conflict_checks(self):
        refs,_=self.inputs(b'pressure_mpa,unit\n0.65000000000000000001,MPa\n')
        process=self.process(criterion=dict(field='pressure_mpa',operator='lte',threshold=.65,unit='MPa'))
        result=self.diagnose(process,refs)
        self.assertEqual(result['decision'],'outside_rule')
        self.assertEqual(result['observations'][0]['original_value'],'0.65000000000000000001')
        refs,_=self.inputs(b'pressure_mpa,unit\n10000000000000000,MPa\n10000000000000001,MPa\n')
        result=self.diagnose(self.process(),refs)
        self.assertEqual(result['decision'],'hold')
        self.assertIn('conflicting_measurements',result['hold_reasons'])

    def test_missing_field_is_not_hidden_by_another_valid_input(self):
        refs,_=self.inputs()
        absent,_=self.inputs(b'other,unit\n42,MPa\n')
        result=self.diagnose(self.process(),refs+absent)
        self.assertEqual(result['decision'],'hold')
        self.assertIn('measurement_missing',result['hold_reasons'])

    def test_missing_input_late_and_media_have_separate_reasons(self):
        process=self.process(required_media=['document','image'])
        refs,_=self.inputs()
        result=self.diagnose(process,refs,input_status='missing',
            event_time=(self.now-timedelta(seconds=5)).isoformat())
        self.assertEqual(result['decision'],'hold')
        self.assertEqual(result['input_status'],'missing')
        self.assertEqual(result['delay_status'],'late')
        self.assertEqual(result['order_status'],'in_order')
        self.assertEqual(result['missing_media'],['image'])
        self.assertEqual(set(result['hold_reasons']),{'input_missing','required_media_missing','input_late'})
        empty=self.diagnose(process,[],sequence=2,input_status='complete')
        self.assertIn('input_missing',empty['hold_reasons'])
        self.assertIn('measurement_missing',empty['hold_reasons'])

    def test_recognition_unavailable_despite_structured_rule_match(self):
        refs,_=self.inputs()
        result=self.diagnose(self.process(),refs,mode='recognition')
        self.assertEqual(result['decision'],'hold')
        self.assertIn('recognition_model_unavailable',result['hold_reasons'])
        self.assertEqual(result['execution_kind'],'recognition_unavailable')
        self.assertIsNone(result['model_used'])

    def test_future_and_invalid_time_reject_without_consuming_sequence(self):
        refs,_=self.inputs();process=self.process()
        self.assert_error(422,'future_event_time',lambda:self.diagnose(process,refs,
            event_time=(self.now+timedelta(microseconds=1)).isoformat()))
        for invalid in ['2026-10-13T12:00:00','not-a-date']:
            self.assert_error(422,'invalid_event_time',lambda:self.diagnose(process,refs,event_time=invalid))
        self.assertEqual(self.diagnose(process,refs)['decision'],'meets_rule')

    def test_duplicate_sequence_and_sequence_event_order_are_independent(self):
        refs,_=self.inputs();process=self.process()
        self.diagnose(process,refs,sequence=10)
        self.assert_error(409,'sequence_conflict',lambda:self.diagnose(process,refs,sequence=10))
        lower=self.diagnose(process,refs,sequence=9)
        self.assertEqual(lower['order_status'],'out_of_order')
        self.assertIn('out_of_order',lower['hold_reasons'])
        older=self.diagnose(process,refs,sequence=11,event_time=(self.now-timedelta(milliseconds=750)).isoformat())
        self.assertEqual(older['delay_status'],'on_time')
        self.assertEqual(older['order_status'],'out_of_order')
        self.assertIn('event_time_out_of_order',older['hold_reasons'])
        # A held older acquisition must not replace the stream's known time
        # baseline and let a still-backward acquisition pass on the next call.
        still_older=self.diagnose(process,refs,sequence=12,event_time=(self.now-timedelta(milliseconds=600)).isoformat())
        self.assertIn('event_time_out_of_order',still_older['hold_reasons'])
        self.assertEqual(self.diagnose(process,refs,sequence=1,stream_id='another')['decision'],'meets_rule')

    def test_delay_boundary_is_checked_before_display_rounding(self):
        refs,_=self.inputs()
        result=self.diagnose(self.process(),refs,event_time=(self.now-timedelta(microseconds=1000001)).isoformat())
        self.assertEqual(result['delay_ms'],1000)
        self.assertEqual(result['delay_status'],'late')
        self.assertIn('input_late',result['hold_reasons'])

    def test_process_and_held_evidence_are_tenant_scoped(self):
        refs,_=self.inputs();process=self.process()
        body=dict(process_id=process['id'],stream_id='synthetic',sequence=1,event_time=self.now.isoformat(),
                  input_status='missing',inputs=refs,mode='recognition')
        self.assert_error(404,'not_found',lambda:self.post('/v1/diagnoses',body,token='b'))
        foreign,_=self.inputs(token='b')
        self.assert_error(404,'not_found',lambda:self.diagnose(process,foreign,input_status='missing',mode='recognition'))

    def test_process_and_numeric_request_validation(self):
        self.assert_error(422,'duplicate_required_media',lambda:self.process(required_media=['document','document']))
        for invalid in [True,float('nan'),float('inf')]:
            with self.subTest(threshold=invalid):
                self.assert_error(422,'invalid_input_type',lambda:self.process(
                    criterion=dict(field='pressure_mpa',operator='gte',threshold=invalid,unit='MPa')))

    def verify(self,result):
        with self.store.tx() as db:
            self.api.diagnosis.verify_saved(db,'a',result)

    def test_process_digest_rejects_changed_rule_on_new_and_saved_diagnoses(self):
        refs,_=self.inputs();process=self.process()
        result=self.diagnose(process,refs)
        changed=copy.deepcopy(process)
        changed['criterion']['threshold']=.9
        self.assert_error(409,'process_integrity_failed',lambda:self.api.diagnosis.verify_process(changed))
        with self.store.tx() as db:
            db.execute("UPDATE records SET data=? WHERE tenant='a' AND kind='processes' AND id=?",
                       (canonical(changed),process['id']))
        self.assert_error(409,'process_integrity_failed',lambda:self.verify(result))
        self.assert_error(409,'process_integrity_failed',lambda:self.diagnose(process,refs,sequence=2))

    def test_saved_observations_decision_and_provenance_must_match_sources(self):
        refs,_=self.inputs();result=self.diagnose(self.process(),refs)
        self.verify(result)
        cases=[]
        for key,new in [('original_value','0.99'),('value',.99),('unit','kPa'),
                        ('unit_source','invented'),('original_units',{'unit':'kPa'})]:
            changed=copy.deepcopy(result);changed['observations'][0][key]=new
            cases.append((key,changed))
        for key,new in [('decision','outside_rule'),('process_version','0'*64),
                        ('missing_media',['image']),('delay_ms',0),('delay_status','late'),
                        ('model_used','invented-model'),('business_approval','granted'),
                        ('stream_id','invented-stream')]:
            changed=copy.deepcopy(result);changed[key]=new
            cases.append((key,changed))
        for key,changed in cases:
            with self.subTest(field=key):
                self.assert_error(409,'diagnosis_integrity_failed',lambda:self.verify(changed))

    def test_saved_hold_cannot_be_changed_into_normal_rule_result(self):
        process=self.process()
        valid,_=self.inputs()
        unknown,_=self.inputs(b'pressure_mpa\n0.68\n')
        mismatched,_=self.inputs(b'pressure_mpa,unit\n0.68,kPa\n')
        for seq,(refs,changes) in enumerate([
                ([],{}),(unknown,{}),(mismatched,{}),
                (valid,{'input_status':'missing'}),
                (valid,{'event_time':(self.now-timedelta(seconds=5)).isoformat()}),
                (valid,{'mode':'recognition'}),
            ],1):
            with self.subTest(sequence=seq):
                result=self.diagnose(process,refs,sequence=seq,stream_id=f'hold-{seq}',**changes)
                self.assertEqual(result['decision'],'hold')
                self.verify(result)
                changed=copy.deepcopy(result)
                changed['hold_reasons']=[];changed['decision']='meets_rule'
                self.assert_error(409,'diagnosis_integrity_failed',lambda:self.verify(changed))

    def test_saved_order_snapshot_survives_following_events_and_deleted_prior_history(self):
        prior_refs,prior_asset=self.inputs();refs,_=self.inputs()
        process=self.process()
        prior=self.diagnose(process,prior_refs,sequence=10)
        held=self.diagnose(process,refs,sequence=9)
        self.verify(prior);self.verify(held)
        following=self.diagnose(process,refs,sequence=11,event_time=self.now.isoformat())
        self.verify(prior);self.verify(held);self.verify(following)
        self.api.dispatch('DELETE',f"/v1/assets/{prior_asset['asset_id']}",'a',{},'delete_prior_source')
        self.verify(held);self.verify(following)
        changed=copy.deepcopy(held)
        changed['hold_reasons']=[];changed['decision']='meets_rule'
        self.assert_error(409,'diagnosis_integrity_failed',lambda:self.verify(changed))

    def test_saved_future_acquisition_time_is_rejected(self):
        refs,_=self.inputs();result=self.diagnose(self.process(),refs)
        changed=copy.deepcopy(result)
        changed['event_time']=(self.now+timedelta(seconds=1)).isoformat()
        self.assert_error(409,'diagnosis_integrity_failed',lambda:self.verify(changed))

    def test_deleted_event_history_holds_new_events_until_new_stream_lineage(self):
        prior_refs,prior_asset=self.inputs();refs,_=self.inputs()
        process=self.process()
        self.diagnose(process,prior_refs,sequence=10,
                      event_time=(self.now-timedelta(milliseconds=200)).isoformat())
        self.api.dispatch('DELETE',f"/v1/assets/{prior_asset['asset_id']}",'a',{},'delete_event_history')
        result=self.diagnose(process,refs,sequence=11,
                             event_time=(self.now-timedelta(milliseconds=500)).isoformat())
        self.assertEqual(result['decision'],'hold')
        self.assertEqual(result['delay_status'],'on_time')
        self.assertEqual(result['order_status'],'unknown')
        self.assertIn('event_history_unavailable',result['hold_reasons'])
        self.verify(result)
        following=self.diagnose(process,refs,sequence=12,event_time=self.now.isoformat())
        self.assertEqual(following['decision'],'hold')
        self.assertIn('event_history_unavailable',following['hold_reasons'])
        self.verify(result)
        restarted=self.diagnose(process,refs,sequence=1,stream_id='new-lineage')
        self.assertEqual(restarted['order_status'],'in_order')
        self.assertEqual(restarted['decision'],'meets_rule')

    def test_coherent_saved_order_tampering_is_rejected_by_snapshot_digest(self):
        refs,_=self.inputs();process=self.process()
        self.diagnose(process,refs,sequence=10)
        held=self.diagnose(process,refs,sequence=9)
        changed=copy.deepcopy(held)
        changed.update(decision='meets_rule',hold_reasons=[],order_status='in_order')
        with self.store.tx() as db:
            db.execute("UPDATE records SET data=? WHERE tenant='a' AND kind='diagnoses' AND id=?",
                       (canonical(changed),held['id']))
        self.assert_error(409,'diagnosis_integrity_failed',lambda:self.api.dispatch(
            'GET',f"/v1/diagnoses/{held['id']}",'a'))


if __name__=='__main__':
    unittest.main()
