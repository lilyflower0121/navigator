"""Verified JSON calculation placements into a bounded native XLSX template."""
import base64
from decimal import Decimal,InvalidOperation
from .contracts import APIError,SCHEMAS,canonical,digest,validate
from .store import uid
from . import xlsx_adapter as adapter

PROFILE='calculation-report-xlsx-v1'
MEDIA='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

class CalculatedXLSX:
    def __init__(self,store,library,reports):self.store,self.library,self.reports=store,library,reports

    def context(self,tenant):
        ctx=getattr(self.store.request_local,'context',None)
        if ctx is None or ctx['tenant']!=tenant:raise APIError(403,'calculation_context_required')
        return ctx

    def raw(self,value):
        try:raw=base64.b64decode(value,validate=True)
        except (ValueError,TypeError):raise APIError(422,'invalid_base64') from None
        if not 0<len(raw)<=adapter.MAX_XLSX_BYTES:raise APIError(413,'xlsx_byte_limit')
        return raw

    def template_value(self,db,tenant,id,name,encoded,json_id,sheet,fields):
        raw=self.raw(encoded);original=self.store.record(db,tenant,'templates',json_id)
        self.reports.verify_template(original)
        if len(fields)!=len(set(fields)):raise APIError(422,'xlsx_duplicate_mapping')
        known={f['field']:f['cell'] for f in original['fields']}
        if not fields or not set(fields)<=known.keys():raise APIError(422,'invalid_report_mapping')
        mappings=[dict(field=f,cell=known[f]) for f in fields]
        snapshot=adapter._snapshot(raw,allow_formulas=True)
        cells=[dict(sheet=sheet,cell=m['cell'],value=0) for m in mappings]
        adapter._mappings(cells,snapshot);adapter.validate_preserved_formulas(raw,snapshot,cells)
        basis=dict(source_sha256=digest(raw),json_template_id=json_id,json_template_version=original['version'],sheet=sheet,mappings=mappings,profile=PROFILE)
        return dict(id=id,name=name,**basis,version=digest(basis),bytes=len(raw),media_type=MEDIA,original_base64=encoded)

    def template(self,db,ctx,body):
        value=self.template_value(db,ctx['tenant'],uid(),body['name'],body['content_base64'],body['json_template_id'],body['sheet'],body['fields'])
        if db.execute("SELECT count(*) FROM records WHERE tenant=? AND kind='xlsx_templates'",(ctx['tenant'],)).fetchone()[0]>=5:raise APIError(413,'xlsx_template_capacity_limit')
        self.store.save_record(db,ctx['tenant'],'xlsx_templates',value);return 201,value,[]

    def verify_template(self,db,tenant,value):
        try:
            expected=self.template_value(db,tenant,value['id'],value['name'],value['original_base64'],value['json_template_id'],value['sheet'],[m['field'] for m in value['mappings']])
            if canonical(value)!=canonical(expected):raise APIError(409,'xlsx_template_integrity_failed')
        except (KeyError,TypeError,ValueError) as error:raise APIError(409,'xlsx_template_integrity_failed') from error
        return self.raw(value['original_base64'])

    def material(self,db,tenant,template,report_id):
        report=self.store.record(db,tenant,'reports',report_id)
        self.reports.verify_draft(db,tenant,report,self.context(tenant));validate(report,SCHEMAS['Report'])
        if report['template_id']!=template['json_template_id'] or report['template_version']!=template['json_template_version']:raise APIError(409,'xlsx_json_template_mismatch')
        by_field={c['field']:c for c in report['cells']};cells=[];provenance=[]
        for mapping in template['mappings']:
            cell=by_field[mapping['field']]
            if cell['cell']!=mapping['cell'] or cell['value_type']!='calculated_decimal' or cell['status']!='copied':raise APIError(422,'xlsx_verified_calculation_required')
            try:
                number=Decimal(cell['value'])
                if not number.is_finite() or abs(number)>999999999999999 or len(number.as_tuple().digits)>15:raise ValueError()
                native=int(number) if number==number.to_integral_value() else float(number)
                if Decimal(str(native))!=number:raise ValueError()
            except (InvalidOperation,ValueError,OverflowError):raise APIError(422,'xlsx_numeric_precision_unsupported') from None
            cells.append(dict(sheet=template['sheet'],cell=mapping['cell'],value=native))
            provenance.append(dict(field=mapping['field'],cell=mapping['cell'],calculation=cell['calculation']))
        assets=[r['asset'] for r in db.execute("SELECT asset FROM dependencies WHERE tenant=? AND kind='reports' AND id=?",(tenant,report_id))]
        return report,cells,provenance,assets

    def render(self,db,ctx,body):
        tenant=ctx['tenant'];template=self.store.record(db,tenant,'xlsx_templates',body['template_id']);raw=self.verify_template(db,tenant,template)
        report,cells,provenance,assets=self.material(db,tenant,template,body['report_id'])
        if db.execute("SELECT count(*) FROM records WHERE tenant=? AND kind='xlsx_reports'",(tenant,)).fetchone()[0]>=10:raise APIError(413,'xlsx_report_capacity_limit')
        output,preservation=adapter.render_xlsx(raw,cells,allow_formulas=True)
        value=dict(id=uid(),state='draft',profile=PROFILE,template_id=template['id'],template_source_sha256=template['source_sha256'],template_version=template['version'],
            report_id=report['id'],report_sha256=digest(report),cells=cells,provenance=provenance,unresolved_fields=report['unresolved_fields'],
            output_sha256=digest(output),bytes=len(output),media_type=MEDIA,file_base64=base64.b64encode(output).decode(),review_required=True,external_submission=False,preservation=preservation)
        self.store.save_record(db,tenant,'xlsx_reports',value,assets);return 201,value,assets

    def verify_saved(self,db,tenant,value):
        template=self.store.record(db,tenant,'xlsx_templates',value['template_id']);raw=self.verify_template(db,tenant,template)
        report,cells,provenance,_=self.material(db,tenant,template,value['report_id'])
        output=self.raw(value['file_base64'])
        preservation=adapter.validate_output(raw,output,cells,allow_formulas=True)
        preservation['preservation_method']='original_package_with_artifact_authored_value_nodes'
        expected=dict(id=value['id'],state='draft',profile=PROFILE,template_id=template['id'],template_source_sha256=template['source_sha256'],template_version=template['version'],
            report_id=report['id'],report_sha256=digest(report),cells=cells,provenance=provenance,unresolved_fields=report['unresolved_fields'],
            output_sha256=digest(output),bytes=len(output),media_type=MEDIA,file_base64=value['file_base64'],review_required=True,external_submission=False,preservation=preservation)
        if canonical(value)!=canonical(expected):raise APIError(409,'xlsx_report_integrity_failed')
        return output
