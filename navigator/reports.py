"""Template-bound JSON placement drafts. Never execute formulas or submit."""
import base64
import re
from .contracts import APIError,SCHEMAS,canonical,decode_source,digest,json_load,validate
from .store import uid

def formula_like_literal(value):
    """Flag spreadsheet metacharacters without rewriting or evaluating text."""
    return (isinstance(value,str) and
            (value.startswith(('\t','\r')) or value.lstrip().startswith(('=','+','-','@'))))

def literal_status(value):
    """Source blanks retain their bytes and cannot complete a required field."""
    if value is None:
        return 'unknown','source_value_null'
    if isinstance(value,str) and not value.strip():
        return 'unknown','source_value_blank'
    if formula_like_literal(value):
        return 'needs_review','formula_like_literal'
    return 'copied',None

def source_literal(fields,path):
    """Resolve a legacy field name or JSON Pointer inside verified fields only."""
    if path in fields:
        value=fields[path]
    else:
        if not path.startswith('/'):
            raise APIError(422,'source_field_not_found')
        value=fields
        for token in path[1:].split('/'):
            if re.search(r'~(?:[^01]|$)',token):
                raise APIError(422,'source_field_not_found')
            token=token.replace('~1','/').replace('~0','~')
            if isinstance(value,dict):
                if token not in value:
                    raise APIError(422,'source_field_not_found')
                value=value[token]
            elif isinstance(value,list):
                if not re.fullmatch(r'0|[1-9][0-9]*',token) or int(token)>=len(value):
                    raise APIError(422,'source_field_not_found')
                value=value[int(token)]
            else:
                raise APIError(422,'source_field_not_found')
    if value is not None and not isinstance(value,(str,int,float,bool)):
        raise APIError(422,'source_field_not_literal')
    return value

class Reports:
    def __init__(self,store,library):
        self.store,self.library=store,library
        self.evidence_assist=None

    def calculated_cell(self,db,ctx,field,reference):
        if ctx is None or self.evidence_assist is None:raise APIError(403,'calculation_context_required')
        calculation=self.evidence_assist.report_claim(db,ctx,reference)
        return dict(**field,value=calculation['claim']['value'],status='copied',evidence=None,
                    source_field=None,source_quote=None,source_sha256=None,locator=None,
                    formula_injection_risk=False,value_type='calculated_decimal',uncertainty_reason=None,
                    calculation=calculation)

    def verify_template(self,template):
        """Check the mapping against its immutable byte original on every use."""
        try:
            raw=decode_source(template['original_base64'])
            content=json_load(raw)
            validate(content,SCHEMAS['TemplateBody'])
            fields=content['fields']
            sha=digest(raw)
            if (sha!=template['version'] or sha!=template['source_sha256'] or
                    template['source_version']!=1 or template['bytes']!=len(raw) or
                    content['fields']!=template['fields'] or content['format']!=template['format'] or
                    len({f['field'] for f in fields})!=len(fields) or
                    len({f['cell'] for f in fields})!=len(fields)):
                raise APIError(409,'template_integrity_failed')
        except (APIError,KeyError,TypeError) as error:
            raise APIError(409,'template_integrity_failed') from error
        return content

    def verify_draft(self,db,tenant,report,ctx=None):
        """Revalidate stored/replayed values, not just existence of their refs."""
        template=self.store.record(db,tenant,'templates',report['template_id'])
        content=self.verify_template(template)
        try:
            if (report['template_version']!=template['version'] or
                    report['template_source_sha256']!=template['source_sha256'] or
                    report['template_source_version']!=template['source_version'] or
                    report['template_original_base64']!=template['original_base64'] or
                    report['format']!=content['format'] or len(report['cells'])!=len(content['fields']) or
                    report['state']!='draft' or report['review_required'] is not True or
                    report['rendered_office_file'] is not False or report['external_submission'] is not False):
                raise APIError(409,'report_integrity_failed')
            if sum('calculation' in c for c in report['cells'])>10:raise APIError(409,'report_integrity_failed')
            for field,cell in zip(content['fields'],report['cells']):
                if 'calculation' in cell or cell.get('value_type')=='calculated_decimal':
                    expected=self.calculated_cell(db,ctx,field,cell['calculation']['reference'])
                    if canonical(expected)!=canonical(cell):raise APIError(409,'report_calculation_integrity_failed')
                    continue
                if any(cell[k]!=v for k,v in field.items()) or cell['value_type']!='literal':
                    raise APIError(409,'report_integrity_failed')
                if cell['evidence'] is None:
                    if any(cell[k] is not None for k in ('value','source_field','source_quote','source_sha256','locator')) or cell['status']!='unknown' or cell['uncertainty_reason']!='not_provided' or cell['formula_injection_risk'] is not False:
                        raise APIError(409,'report_integrity_failed')
                    continue
                segment=self.library.resolve(db,tenant,cell['evidence'])
                try:
                    source_value=source_literal(segment['fields'],cell['source_field'])
                except APIError as error:
                    raise APIError(409,'report_integrity_failed') from error
                risk=formula_like_literal(source_value)
                status,reason=literal_status(source_value)
                if (canonical(cell['value'])!=canonical(source_value) or cell['source_quote']!=segment['quote'] or
                        cell['source_sha256']!=segment['source_sha256'] or cell['locator']!=segment['locator'] or
                        cell['status']!=status or cell['uncertainty_reason']!=reason or
                        cell['formula_injection_risk'] is not risk):
                    raise APIError(409,'report_integrity_failed')
            if (report['unresolved_fields']!=[c['field'] for c in report['cells'] if c['status']!='copied'] or
                    report['required_unresolved_fields']!=[c['field'] for c in report['cells'] if c['required'] and c['status']!='copied']):
                raise APIError(409,'report_integrity_failed')
        except (KeyError,TypeError,ValueError) as error:
            raise APIError(409,'report_integrity_failed') from error

    def template(self,db,ctx,body):
        validate(body,SCHEMAS['TemplateInput'])
        raw=decode_source(body['content_base64'])
        content=json_load(raw)
        validate(content,SCHEMAS['TemplateBody'])
        fields=content['fields']
        if len({f['field'] for f in fields})!=len(fields) or len({f['cell'] for f in fields})!=len(fields):
            raise APIError(422,'duplicate_template_mapping')
        value=dict(id=uid(),name=body['name'],version=digest(raw),source_sha256=digest(raw),
                   source_version=1,bytes=len(raw),media_type='application/vnd.navigator.template+json',
                   original_base64=base64.b64encode(raw).decode(),fields=fields,format=content['format'])
        self.store.save_record(db,ctx['tenant'],'templates',value)
        return 201,value,[]

    def render(self,db,ctx,body):
        validate(body,SCHEMAS['ReportInput'])
        template=self.store.record(db,ctx['tenant'],'templates',body['template_id'])
        content=self.verify_template(template)
        sha=template['source_sha256']
        assignments={a['field']:a for a in body['assignments']}
        if len(assignments)!=len(body['assignments']) or not set(assignments)<=set(f['field'] for f in template['fields']):
            raise APIError(422,'invalid_report_mapping')
        if sum('calculation' in a for a in assignments.values())>10:raise APIError(413,'report_calculation_limit')
        cells=[];assets=[]
        for field in content['fields']:
            cell=dict(**field,value=None,status='unknown',evidence=None,source_field=None,source_quote=None,
                      source_sha256=None,locator=None,formula_injection_risk=False,
                      value_type='literal',uncertainty_reason='not_provided')
            if field['field'] in assignments:
                assignment=assignments[field['field']]
                if 'calculation' in assignment:
                    cell=self.calculated_cell(db,ctx,field,assignment['calculation'])
                    assets.extend(pin['asset_id'] for pin in cell['calculation']['scope'])
                    cells.append(cell)
                    continue
                segment=self.library.resolve(db,ctx['tenant'],assignment['evidence'])
                val=source_literal(segment['fields'],assignment['source_field'])
                risk=formula_like_literal(val)
                status,reason=literal_status(val)
                cell.update(value=val,status=status,
                            evidence=dict(segment['evidence']),source_sha256=segment['source_sha256'],locator=segment['locator'],
                            source_field=assignment['source_field'],source_quote=segment['quote'],formula_injection_risk=risk,
                            uncertainty_reason=reason)
                assets.append(segment['evidence']['asset_id'])
            cells.append(cell)
        value=dict(id=uid(),state='draft',template_id=template['id'],template_version=template['version'],
                   template_source_sha256=sha,template_source_version=template.get('source_version',1),
                   template_original_base64=template['original_base64'],format=content['format'],cells=cells,
                   unresolved_fields=[c['field'] for c in cells if c['status']!='copied'],
                   required_unresolved_fields=[c['field'] for c in cells if c['required'] and c['status']!='copied'],
                   review_required=True,rendered_office_file=False,external_submission=False)
        self.store.save_record(db,ctx['tenant'],'reports',value,assets)
        return 201,value,assets
