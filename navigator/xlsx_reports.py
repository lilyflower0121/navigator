"""One reviewed existing layout, populated from a verified normative claim."""
import base64
import json
import re
from .contracts import APIError,canonical,digest
from .store import uid

MEDIA='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
PROFILE='completion-inspection-reference-v1'
TEMPLATE_SHA='b18c29b5e47ea499d2bddc6533ae1d0f26588c5f0731ce22a6c6a5f26358fb4f'
SHEET='完了検査成績書'
MAP={'item':'B7','method':'C7','criterion':'D7','citation':'H7'}
UNKNOWN=[f'{col}{row}' for row in range(7,17) for col in 'EFG']+['B3','C3','F3','G3','B26','D26','F26','H26']
ROW_HEIGHTS=[dict(sheet=SHEET,row=7,height=180)]
NOTICE='原文の点検内容例を既存汎用帳票へ配置した draft。実測・合否・署名・正式提出は未確定。'

class XLSXReports:
    def __init__(self,store,library,grounded,reports=None):
        self.store,self.library,self.grounded=store,library,grounded
        from .calculated_xlsx import CalculatedXLSX
        self.calculated=CalculatedXLSX(store,library,reports)

    def original(self,encoded):
        try: raw=base64.b64decode(encoded,validate=True)
        except (ValueError,TypeError): raise APIError(422,'invalid_base64') from None
        if not raw or len(raw)>1024*1024: raise APIError(413,'xlsx_byte_limit')
        return raw

    def verify_template(self,value):
        if value.get('profile')=='calculation-report-xlsx-v1':
            ctx=getattr(self.store.request_local,'context',None)
            if ctx is None:raise APIError(403,'calculation_context_required')
            return self.calculated.verify_template(self.store.db,ctx['tenant'],value)
        raw=self.original(value['original_base64'])
        expected=dict(id=value['id'],name=value['name'],profile=PROFILE,source_sha256=digest(raw),
            version=digest(dict(source_sha256=digest(raw),profile=PROFILE,sheet=SHEET,mappings=MAP)),
            bytes=len(raw),media_type=MEDIA,original_base64=value['original_base64'],sheet=SHEET,mappings=MAP,unknown_cells=UNKNOWN)
        if digest(raw)!=TEMPLATE_SHA or canonical(value)!=canonical(expected):
            raise APIError(409,'xlsx_template_integrity_failed')
        from .xlsx_adapter import validate_xlsx
        validate_xlsx(raw)
        return raw

    def template(self,db,ctx,body):
        if 'json_template_id' in body:return self.calculated.template(db,ctx,body)
        raw=self.original(body['content_base64'])
        if digest(raw)!=TEMPLATE_SHA:
            raise APIError(422,'xlsx_template_profile_not_supported')
        if db.execute("SELECT count(*) FROM records WHERE tenant=? AND kind='xlsx_templates'",(ctx['tenant'],)).fetchone()[0]>=5:
            raise APIError(413,'xlsx_template_capacity_limit')
        value=dict(id=uid(),name=body['name'],profile=PROFILE,source_sha256=digest(raw),
            version=digest(dict(source_sha256=digest(raw),profile=PROFILE,sheet=SHEET,mappings=MAP)),bytes=len(raw),
            media_type=MEDIA,original_base64=body['content_base64'],sheet=SHEET,mappings=MAP,unknown_cells=UNKNOWN)
        self.verify_template(value)
        self.store.save_record(db,ctx['tenant'],'xlsx_templates',value)
        return 201,value,[]

    def _cells(self,db,tenant,answer):
        self.grounded.verify_saved(db,tenant,answer)
        if answer['status']!='supported' or answer['requested_field']!='midrail_height' or len(answer['claims'])!=1:
            raise APIError(422,'xlsx_supported_inspection_criterion_required')
        claim=answer['claims'][0]
        if claim['value_role']!='inspection_example_criterion':
            raise APIError(422,'xlsx_supported_inspection_criterion_required')
        lower,upper=claim['values']
        criterion=lower['value']+lower['unit']+'以上'+upper['value']+upper['unit']+'以下'
        method='中さんの高さは'+criterion+'か'
        source=claim['support_evidence'];ref=source['evidence']
        # Verify full pinned source; printed page is only included if native
        # page text itself contains that standalone footer label.
        segment=self.library.resolve(db,tenant,ref)
        if source['locator']['kind']!='pdf_page':
            raise APIError(422,'xlsx_pdf_criterion_required')
        row=db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',(tenant,ref['asset_id'],ref['source_version'])).fetchone()
        name=json.loads(row['meta'])['name'];page=source['locator']['page']
        footer=re.search(r'(?:^|\n)\s*([0-9]{1,4})\s*$',segment['quote'])
        page_label=f'原本{page}ページ'+(f'（印刷{footer.group(1)}）' if footer else '')
        citation=f"{name} / {page_label} / SHA256 {source['source_sha256']} / 点検内容例・実測未入力"
        values={'item':'中さんの高さ','method':method,'criterion':criterion,'citation':citation}
        cells=[dict(sheet=SHEET,cell=cell,value=values[field]) for field,cell in MAP.items()]
        return cells,source,[ref['asset_id']]

    def render(self,db,ctx,body):
        if 'report_id' in body:return self.calculated.render(db,ctx,body)
        template=self.store.record(db,ctx['tenant'],'xlsx_templates',body['template_id'])
        raw=self.verify_template(template)
        answer=self.store.record(db,ctx['tenant'],'grounded_answers',body['grounded_answer_id'])
        cells,source,assets=self._cells(db,ctx['tenant'],answer)
        if db.execute("SELECT count(*) FROM records WHERE tenant=? AND kind='xlsx_reports'",(ctx['tenant'],)).fetchone()[0]>=10:
            raise APIError(413,'xlsx_report_capacity_limit')
        from .xlsx_adapter import render_xlsx
        output,manifest=render_xlsx(raw,cells,row_heights=ROW_HEIGHTS)
        value=dict(id=uid(),state='draft',profile=PROFILE,template_id=template['id'],template_source_sha256=template['source_sha256'],
            template_version=template['version'],grounded_answer_id=answer['id'],source=source,cells=cells,
            unknown_cells=UNKNOWN,unresolved_fields=['actual_measurements','site_judgment','inspection_datetime','signatures','approval'],
            output_sha256=digest(output),bytes=len(output),media_type=MEDIA,file_base64=base64.b64encode(output).decode(),
            review_required=True,external_submission=False,notice=NOTICE,preservation=manifest)
        self.store.save_record(db,ctx['tenant'],'xlsx_reports',value,assets)
        return 201,value,assets

    def verify_saved(self,db,tenant,value):
        if value.get('profile')=='calculation-report-xlsx-v1':return self.calculated.verify_saved(db,tenant,value)
        if value.get('profile')!=PROFILE:raise APIError(409,'xlsx_report_integrity_failed')
        template=self.store.record(db,tenant,'xlsx_templates',value['template_id']);raw=self.verify_template(template)
        answer=self.store.record(db,tenant,'grounded_answers',value['grounded_answer_id'])
        cells,source,assets=self._cells(db,tenant,answer)
        if (canonical(value['cells'])!=canonical(cells) or canonical(value['source'])!=canonical(source) or
            value['template_source_sha256']!=template['source_sha256'] or value['template_version']!=template['version'] or
            value['unknown_cells']!=UNKNOWN or value['unresolved_fields']!=['actual_measurements','site_judgment','inspection_datetime','signatures','approval'] or
            value['notice']!=NOTICE or value['profile']!=PROFILE or value['state']!='draft' or not value['review_required'] or value['external_submission']):
            raise APIError(409,'xlsx_report_integrity_failed')
        output=self.original(value['file_base64'])
        if digest(output)!=value['output_sha256'] or len(output)!=value['bytes']:
            raise APIError(409,'xlsx_report_integrity_failed')
        from .xlsx_adapter import validate_output
        manifest=validate_output(raw,output,cells,row_heights=ROW_HEIGHTS)
        # Rendering implementation metadata may include author-tool context;
        # the native preservation check is re-run for every read/replay.
        manifest['preservation_method']='original_package_with_artifact_authored_value_nodes'
        if canonical(value['preservation'])!=canonical(manifest):
            raise APIError(409,'xlsx_report_integrity_failed')
        return output
