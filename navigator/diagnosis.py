"""Event-aware rule diagnosis over pinned structured evidence; no perception AI."""
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import json
import math
from .contracts import APIError,digest,SCHEMAS,validate
from .store import uid
from .library import family

def event_time(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone_required')
        return parsed.astimezone(timezone.utc)
    except (ValueError,TypeError,AttributeError,OverflowError) as error:
        raise APIError(422,'invalid_event_time') from error

def measurement(fields,criterion,evidence):
    """Keep source values/units; a process's unit is never an observed unit.

    Unit conversion is deliberately unavailable. Both conventional CSV unit
    columns are recorded so a field-specific declaration cannot hide a
    contradictory generic declaration.
    """
    raw=fields[criterion['field']]
    reasons=[]
    number=None
    try:
        if isinstance(raw,bool) or not isinstance(raw,(int,float,str)):
            raise ValueError('not_measurement')
        number=float(raw)
        if not math.isfinite(number):
            number=None
            raise ValueError('non_finite')
        # CSV decimal precision must survive numeric conflict/rule checks,
        # even when the JSON display number rounds to a nearby float.
        Decimal(str(raw))
    except (TypeError,ValueError,OverflowError,InvalidOperation):
        number=None
        reasons.append('unparseable_measurement')
    unit_fields=[criterion['field']+'_unit','unit']
    original_units={key:fields[key] for key in unit_fields if key in fields}
    unit=None;unit_source=None
    if not original_units or any(not isinstance(u,str) or not u.strip() for u in original_units.values()):
        reasons.append('measurement_unit_missing')
    elif len(set(original_units.values()))>1:
        reasons.append('measurement_unit_conflict')
    else:
        unit_source=next(iter(original_units))
        unit=original_units[unit_source]
        if unit!=criterion['unit']:
            reasons.append('measurement_unit_mismatch')
    return dict(original_value=raw,value=number,unit=unit,unit_source=unit_source,
                original_units=original_units,evidence=evidence),reasons

class Diagnosis:
    def __init__(self,store,library,clock=lambda:datetime.now(timezone.utc)):
        self.store,self.library,self.clock=store,library,clock

    def verify_process(self,process):
        """An immutable rule remains bound to the exact ProcessInput digest."""
        try:
            validate(process,SCHEMAS['Process'])
            original={k:process[k] for k in SCHEMAS['ProcessInput']['properties']}
            if (digest(original)!=process['version'] or
                    len(set(process['required_media']))!=len(process['required_media'])):
                raise ValueError('changed_process')
        except (APIError,KeyError,TypeError,ValueError,OverflowError) as error:
            raise APIError(409,'process_integrity_failed') from error

    def facts(self,db,tenant,process,inputs,input_status,event,received,execution_kind):
        """Recompute all evidence-supported facts without current stream history."""
        segments=[self.library.resolve(db,tenant,r) for r in inputs]
        media=[]
        for ref in inputs:
            source=db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',
                              (tenant,ref['asset_id'],ref['source_version'])).fetchone()
            media.append(family(json.loads(source['meta'])['media_type']))
        missing=sorted(set(process['required_media'])-set(media))
        elapsed_ms=(received-event).total_seconds()*1000
        delay_status='late' if elapsed_ms>process['max_delay_ms'] else 'on_time'
        reasons=[]
        if input_status=='missing' or not inputs:
            reasons.append('input_missing')
        if missing:
            reasons.append('required_media_missing')
        if delay_status=='late':
            reasons.append('input_late')
        if execution_kind=='recognition_unavailable':
            reasons.append('recognition_model_unavailable')
        criterion=process['criterion'];observed=[]
        for segment in segments:
            if criterion['field'] in segment['fields']:
                observation,problems=measurement(segment['fields'],criterion,segment['evidence'])
                observed.append(observation)
                reasons.extend(problems)
            else:
                reasons.append('measurement_missing')
        if not observed:
            reasons.append('measurement_missing')
        if len({Decimal(str(o['original_value'])) for o in observed if o['value'] is not None})>1:
            reasons.append('conflicting_measurements')
        return dict(missing_media=missing,delay_ms=round(elapsed_ms),delay_status=delay_status,
                    observations=observed),reasons

    def rule_decision(self,process,observed,reasons):
        if reasons:
            return 'hold'
        number=Decimal(str(observed[0]['original_value']))
        criterion=process['criterion'];threshold=Decimal(str(criterion['threshold']))
        meets=number>=threshold if criterion['operator']=='gte' else number<=threshold
        return 'meets_rule' if meets else 'outside_rule'

    def verify_saved(self,db,tenant,value):
        """Reject stored/replayed observations and decisions without source support.

        Ordering reasons describe the evaluation-time snapshot. Replaying old
        diagnoses neither incorporates later events nor restores deleted history.
        """
        try:
            validate(value,SCHEMAS['Diagnosis'])
            event=event_time(value['event_time']);received=event_time(value['received_time'])
            if event>received:
                raise ValueError('future_event')
        except (APIError,KeyError,TypeError,ValueError,OverflowError) as error:
            raise APIError(409,'diagnosis_integrity_failed') from error
        process=self.store.record(db,tenant,'processes',value['process_id'])
        self.verify_process(process)
        if process['id']!=value['process_id']:
            raise APIError(409,'process_integrity_failed')
        facts,reasons=self.facts(db,tenant,process,value['inputs'],value['input_status'],
                                event,received,value['execution_kind'])
        try:
            snapshot=db.execute('SELECT snapshot_hash FROM sequences WHERE tenant=? AND process=? AND stream=? AND sequence=?',
                                (tenant,process['id'],value['stream_id'],value['sequence'])).fetchone()
            historical={'out_of_order','event_time_out_of_order','event_history_unavailable'}&set(value['hold_reasons'])
            reasons=sorted(set(reasons)|historical)
            expected_order=('unknown' if 'event_history_unavailable' in historical else
                            'out_of_order' if historical else 'in_order')
            if (value['process_version']!=process['version'] or
                    digest({key:value[key] for key in facts})!=digest(facts) or
                    value['hold_reasons']!=reasons or
                    value['order_status']!=expected_order or
                    value['decision']!=self.rule_decision(process,facts['observations'],reasons) or
                    snapshot is None or snapshot['snapshot_hash']!=digest(value)):
                raise ValueError('changed_diagnosis')
        except (KeyError,TypeError,ValueError,OverflowError,InvalidOperation) as error:
            raise APIError(409,'diagnosis_integrity_failed') from error

    def process(self,db,ctx,body):
        if len(set(body['required_media']))!=len(body['required_media']):
            raise APIError(422,'duplicate_required_media')
        value=dict(id=uid(),version=digest(body),**body)
        self.store.save_record(db,ctx['tenant'],'processes',value)
        return 201,value,[]

    def evaluate(self,db,ctx,body):
        process=self.store.record(db,ctx['tenant'],'processes',body['process_id'])
        self.verify_process(process)
        if process['id']!=body['process_id']:
            raise APIError(409,'process_integrity_failed')
        received=self.clock();event=event_time(body['event_time'])
        if event>received:
            raise APIError(422,'future_event_time')
        previous=db.execute('SELECT max(sequence) FROM sequences WHERE tenant=? AND process=? AND stream=?',
                            (ctx['tenant'],process['id'],body['stream_id'])).fetchone()[0]
        if db.execute('SELECT 1 FROM sequences WHERE tenant=? AND process=? AND stream=? AND sequence=?',
                      (ctx['tenant'],process['id'],body['stream_id'],body['sequence'])).fetchone():
            raise APIError(409,'sequence_conflict')
        execution_kind='structured_rules' if body['mode']=='structured_rules' else 'recognition_unavailable'
        # Resolve even held evidence through the tenant/integrity boundary.
        facts,reasons=self.facts(db,ctx['tenant'],process,body['inputs'],body['input_status'],event,received,execution_kind)
        order_status='out_of_order' if previous is not None and body['sequence']<previous else 'in_order'
        if order_status=='out_of_order':
            reasons.append('out_of_order')
        # Sequence and acquisition time are independent signals. Increasing
        # sequence numbers do not make an older acquisition timely/in order.
        if previous is not None:
            rows=db.execute("SELECT data FROM records WHERE tenant=? AND kind='diagnoses'",(ctx['tenant'],)).fetchall()
            known_sequences=set()
            for row in rows:
                prior=json.loads(row['data'])
                if prior.get('process_id')==process['id'] and prior.get('stream_id')==body['stream_id']:
                    prior_event=event_time(prior['event_time'])
                    known_sequences.add(prior['sequence'])
                    if body['sequence']>previous and prior['sequence']<body['sequence'] and event<prior_event:
                        order_status='out_of_order'
                        reasons.append('event_time_out_of_order')
            ledger={row[0] for row in db.execute('SELECT sequence FROM sequences WHERE tenant=? AND process=? AND stream=?',
                                               (ctx['tenant'],process['id'],body['stream_id']))}
            # Source deletion removes dependent diagnosis payloads, including
            # their acquisition times. A surviving sequence number cannot
            # prove that the next event is temporally in order.
            if ledger-known_sequences:
                order_status='unknown'
                reasons.append('event_history_unavailable')
        decision=self.rule_decision(process,facts['observations'],reasons)
        value=dict(id=uid(),state='evaluated',decision=decision,hold_reasons=sorted(set(reasons)),
                   process_id=process['id'],process_version=process['version'],event_time=event.isoformat(),received_time=received.isoformat(),
                   stream_id=body['stream_id'],sequence=body['sequence'],input_status=body['input_status'],**facts,
                   order_status=order_status,inputs=body['inputs'],execution_kind=execution_kind,model_used=None,
                   business_approval='not_granted')
        assets=[r['asset_id'] for r in body['inputs']]
        self.store.save_record(db,ctx['tenant'],'diagnoses',value,assets)
        # A stream's sequence ledger is bounded by the diagnosis record limit.
        db.execute('INSERT INTO sequences(tenant,process,stream,sequence,snapshot_hash) VALUES(?,?,?,?,?)',
                   (ctx['tenant'],process['id'],body['stream_id'],body['sequence'],digest(value)))
        return 201,value,assets
