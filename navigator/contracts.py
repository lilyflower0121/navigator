"""Request schemas are the source for validation and OpenAPI (stdlib only)."""
import base64
import hashlib
import json
import math
import re
from decimal import Decimal,InvalidOperation

MAX_BODY = 6 * 1024 * 1024
MAX_SOURCE = 64 * 1024
MAX_PDF = 4 * 1024 * 1024
MAX_IMAGE = 1024 * 1024
MAX_XLSX_SOURCE = 2 * 1024 * 1024
XLSX_MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
LIMITS = dict(xlsx_source_bytes=MAX_XLSX_SOURCE,source_bytes=MAX_SOURCE,pdf_source_bytes=MAX_PDF,pdf_pages=32,image_source_bytes=MAX_IMAGE,image_pixels=2000000,image_gallery=5,xlsx_bytes=1048576,xlsx_templates=5,xlsx_reports=10,http_bytes=MAX_BODY, assets=100,
              tenant_source_bytes=20*1024*1024, segments=256, active_jobs=32,
              jobs=256, idempotency_keys=512, derived_records=100, definitions=20)
MEDIA = ['text/plain', 'text/markdown', 'text/csv', 'application/json','application/pdf',
         'image/png', 'image/jpeg', 'video/mp4', 'audio/wav', XLSX_MEDIA]

class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()

def json_load(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError('non_finite_json')
    def finite_float(value):
        if len(value)>128:
            raise ValueError('number_literal_limit')
        number=float(value)
        if not math.isfinite(number):
            raise ValueError('non_finite_json')
        if number==0 and Decimal(value)!=0:
            raise ValueError('json_number_underflow')
        return number
    def bounded_int(value):
        if len(value.lstrip('-'))>64:
            raise ValueError('integer_literal_limit')
        return int(value)
    def decoded_strings(value,depth=0):
        if depth>20:
            raise ValueError('json_nesting_limit')
        if isinstance(value,str):
            value.encode('utf-8')
        elif isinstance(value,dict):
            for key,item in value.items():
                key.encode('utf-8');decoded_strings(item,depth+1)
        elif isinstance(value,list):
            for item in value:
                decoded_strings(item,depth+1)
    try:
        if isinstance(raw,bytes):
            raw=raw.decode('utf-8')
        value=json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant,parse_float=finite_float,parse_int=bounded_int)
        decoded_strings(value)
        return value
    except (ValueError, UnicodeError, RecursionError,InvalidOperation) as error:
        raise APIError(422, 'invalid_json') from error

def obj(properties, required=None):
    return dict(type='object', properties=properties, required=list(properties) if required is None else required, additionalProperties=False)

def string(maximum=200, minimum=1, **kw):
    return dict(type='string', minLength=minimum, maxLength=maximum, **kw)

def integer(low=0, high=1000000):
    return dict(type='integer', minimum=low, maximum=high)

def array(items, maximum=10, minimum=0):
    return dict(type='array', items=items, minItems=minimum, maxItems=maximum)

ID = string(32, 32, pattern='^[a-f0-9]{32}$')
EVIDENCE = obj(dict(asset_id=ID, source_version=integer(1,100), analysis_version=string(100), segment_id=ID))
FILTER = obj(dict(asset_ids=array(ID), media_types=array(dict(type='string', enum=MEDIA)),
                  labels=array(string(80)), versions=dict(type='string', enum=['latest','all'])), [])
QUERY = obj(dict(query=string(1000), modality=dict(type='string',enum=['text','image','video']),
                 filters=FILTER, limit=integer(1,20),ranking_mode=dict(type='string',enum=['lexical','bm25','hybrid'])), ['query'])
XLSX_SELECTION = obj(dict(sheet=string(31),ranges=array(string(40,pattern=r'^[A-Z]{1,3}[1-9][0-9]{0,4}:[A-Z]{1,3}[1-9][0-9]{0,4}$'),8,1)))
CALCULATION_REF = obj(dict(task_id=ID,revision=integer(1,9),claim_index=integer(0,19)))
SCHEMAS = {
 'AssetInput': obj(dict(name=string(160), media_type=dict(type='string', enum=MEDIA),
                      content_base64=string(MAX_BODY), labels=array(string(80)), asset_id=ID, xlsx_selection=XLSX_SELECTION), ['name','media_type','content_base64']),
 'QueryInput': QUERY,
 'AnswerInput': obj(dict(**QUERY['properties'], mode=dict(type='string',enum=['extractive','generative'])),['query']),
 'RevisionInput': obj(dict(expected_revision=integer(1))),
 'EmptyInput': obj({}),
 'TemplateInput': obj(dict(name=string(160),content_base64=string(90000))),
 'TemplateBody': obj(dict(format=dict(type='string',const='navigator-cell-layout-v1'),
                         fields=array(obj(dict(field=string(80),cell=string(30,pattern='^[A-Z]{1,3}[1-9][0-9]{0,5}$'), required=dict(type='boolean'))),100,1))),
 'ReportInput': obj(dict(template_id=ID,assignments=array({'anyOf':[
     obj(dict(field=string(80),evidence=EVIDENCE,source_field=string(100))),
     obj(dict(field=string(80),calculation=CALCULATION_REF))]},100))),
 'ProcessInput': obj(dict(name=string(160),required_media=array(dict(type='string',enum=['document','image','video','audio']),4,1),
                         max_delay_ms=integer(0,3600000),criterion=obj(dict(field=string(100),operator=dict(type='string',enum=['gte','lte']),
                         threshold=dict(type='number',minimum=-1e9,maximum=1e9),unit=string(30,0))))),
 'DiagnosisInput': obj(dict(process_id=ID,stream_id=string(80),sequence=integer(0,2**31-1),event_time=string(40),
                           input_status=dict(type='string',enum=['complete','missing']),inputs=array(EVIDENCE),
                           mode=dict(type='string',enum=['structured_rules','recognition']))),
 'Evidence': EVIDENCE,
 'Error': obj(dict(code=string(100),request_id=string(40))),
}

def validate(value, schema, depth=0):
    if depth > 20:
        raise APIError(422,'input_nesting_limit')
    if '$ref' in schema:
        return validate(value, SCHEMAS[schema['$ref'].split('/')[-1]],depth+1)
    if 'anyOf' in schema:
        for alternative in schema['anyOf']:
            try:
                validate(value,alternative,depth+1)
                return
            except APIError:
                pass
        raise APIError(422,'invalid_input_union')
    kind = schema.get('type')
    valid = {'object':isinstance(value,dict), 'array':isinstance(value,list),
             'string':isinstance(value,str), 'integer':type(value) is int,
             'number':type(value) is int or type(value) is float and math.isfinite(value),
             'boolean':type(value) is bool, 'null':value is None}
    if isinstance(kind,list):
        if not any(valid.get(t,False) for t in kind):
            raise APIError(422,'invalid_input_type')
        kind=next(t for t in kind if valid.get(t,False))
    if kind and not valid.get(kind,False):
        raise APIError(422,'invalid_input_type')
    if 'enum' in schema and value not in schema['enum'] or 'const' in schema and value != schema['const']:
        raise APIError(422,'invalid_input_value')
    if kind == 'object':
        if any(k not in value for k in schema.get('required',[])) or schema.get('additionalProperties') is False and any(k not in schema['properties'] for k in value):
            raise APIError(422,'invalid_input_fields')
        for key,item in value.items():
            if key in schema.get('properties',{}):
                validate(item,schema['properties'][key],depth+1)
            elif isinstance(schema.get('additionalProperties'),dict):
                validate(item,schema['additionalProperties'],depth+1)
    if kind == 'array':
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',1000):
            raise APIError(413,'input_item_limit')
        for item in value:
            validate(item,schema['items'],depth+1)
    if kind == 'string':
        if not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',10000) or 'pattern' in schema and not re.search(schema['pattern'],value):
            raise APIError(422,'invalid_input_text')
        if '\x00' in value:
            raise APIError(422,'nul_in_input')
        try:
            value.encode('utf-8')
        except UnicodeError as error:
            raise APIError(422,'invalid_unicode') from error
    if kind in ('integer','number') and not schema.get('minimum',-math.inf)<=value<=schema.get('maximum',math.inf):
        raise APIError(422,'invalid_input_number')

def decode_source(encoded,media_type=None):
    try:
        raw=base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError) as error:
        raise APIError(422,'invalid_base64') from error
    if not raw or len(raw)>(MAX_PDF if media_type=='application/pdf' else MAX_IMAGE if media_type in ('image/png','image/jpeg') else MAX_XLSX_SOURCE if media_type==XLSX_MEDIA else MAX_SOURCE):
        raise APIError(413,'source_byte_limit')
    return raw

# Successful result schemas are explicit about provenance; nested record payloads
# have stable fields documented in docs/CONTRACT_JA.md and preserved verbatim.
SCHEMAS.update({
 'Source': obj(dict(source_version=integer(1,100),sha256=string(64,64),bytes=integer(1,MAX_PDF),
                    media_type=dict(type='string',enum=MEDIA),name=string(160),labels=array(string(80)),
                    authorization_revision=integer(1),job_id=ID,analysis=dict(type='object'),xlsx_selection=XLSX_SELECTION),['source_version','sha256','bytes','media_type','name','labels','authorization_revision','job_id','analysis']),
 'Asset': obj(dict(id=ID,versions=array({'$ref':'#/components/schemas/Source'},100,1))),
 'Job': obj(dict(id=ID,asset_id=ID,source_version=integer(1,100),state=dict(type='string',enum=['queued','running','succeeded','blocked','failed','cancelled']),
                revision=integer(1),epoch=integer(),attempt=integer(0,3),lease_until=dict(type='number'),processor_version=string(80),model_used=dict(type=['null','string'],maxLength=100),
                execution_kind=dict(type='string',const='real_deterministic'),error=dict(type=['string','null']),result=dict(type=['object','null']))),
 'IngestResult':obj(dict(asset_id=ID,source_version=integer(1,100),job={'$ref':'#/components/schemas/Job'})),
})

HASH=string(64,64,pattern='^[a-f0-9]{64}$')
LITERAL=dict(type=['string','number','boolean','null'])
NULL_TEXT=dict(type=['string','null'],maxLength=200)
FIELDS=dict(type='object',additionalProperties=LITERAL)
XLSX_CELL=obj(dict(address=string(20),column=string(3),column_index=integer(1,256),kind=dict(type='string',enum=['number','text','blank','boolean','error','date','formula']),value=LITERAL,raw_value=dict(type=['string','null'],maxLength=8192),formula=dict(type=['string','null'],maxLength=8192),formula_attributes=dict(type='object',additionalProperties=dict(type='string')),cached_value=LITERAL,cache_state=dict(type='string',enum=['present','missing','not_formula']),stored=dict(type='boolean'),merged_range=dict(type=['string','null']),merged_anchor=dict(type=['string','null']),row_hidden=dict(type='boolean'),column_hidden=dict(type='boolean'),style_id=integer(0,65535)))
XLSX_FIELDS=obj(dict(sheet=string(31),row=integer(1,10000),sheet_state=dict(type='string',enum=['visible','hidden','veryHidden']),row_hidden=dict(type='boolean'),cells=array(XLSX_CELL,256),merges=array(string(40),256),labels=array(string(8192),256),headers=array(obj(dict(address=string(20),row=integer(1,10000),column=string(3),text=string(8192),role=dict(type='string',const='candidate'),merged_range=dict(type=['string','null'],maxLength=40)),['address','row','column','text','role']),256),columns=array(obj(dict(column=string(3),column_index=integer(1,256),hidden=dict(type='boolean'))),256)))
SCHEMAS['WorksheetCell']=XLSX_CELL
SCHEMAS['WorksheetRowFields']=XLSX_FIELDS
SCHEMAS.update({
 'Locator':obj(dict(kind=dict(type='string',enum=['lines','json_pointer','pdf_page','image_region','xlsx_row']),sheet=string(31),row=integer(1,10000),page=integer(1,32),line_start=integer(1,65536),line_end=integer(1,65536),
                    header_line_end=integer(1,65536),pointer=string(2000,0),bbox=array(integer(0,2000000),4,4),image_width=integer(1,2000000),image_height=integer(1,2000000),char_start=integer(0,65536),char_end=integer(0,65536),
                    offset_unit=dict(type='string',const='unicode_codepoints')),['kind']),
 'Segment':obj(dict(id=ID,quote=string(8192),locator={'$ref':'#/components/schemas/Locator'},fields={'anyOf':[FIELDS,XLSX_FIELDS]},
                    evidence={'$ref':'#/components/schemas/Evidence'},source_sha256=HASH,
                    claim_kind=dict(type='string',enum=['source_quote','ocr_observation']),model_used=dict(type=['null','string'],maxLength=100),
                    confidence=dict(type='number',minimum=0,maximum=100),review_required=dict(type='boolean')),
                    ['id','quote','locator','fields','evidence','source_sha256','claim_kind','model_used']),
 'Extraction':obj(dict(analysis_version=string(100),processor_version=string(80),model_used=dict(type=['null','string'],maxLength=100),
                       segments=array({'$ref':'#/components/schemas/Segment'},256),interpretation=string(100),capability=string(100),extracted_sha256=HASH,
                       pixel_sha256=HASH,image_width=integer(1,2000000),image_height=integer(1,2000000),visual_signature=obj(dict(format=dict(type='string',const='rgb-grid16-v1'),values=array(integer(0,255),256,256))),
                       processor_detail=dict(type='object'),ocr_state=dict(type='string',enum=['observations','unknown'])),[]),
 'Search':obj(dict(mode=dict(type='string',enum=['lexical','bm25','hybrid']),query=string(1000),filters=FILTER,
                  results=array({'$ref':'#/components/schemas/Match'},20),score_meaning=string(200),model_used=dict(type=['string','null'],maxLength=200))),
 'Answer':obj(dict(id=ID,mode=dict(type='string',const='extractive'),status=dict(type='string',enum=['evidence_found','review_required','insufficient_evidence']),
                  reason=string(100),query=string(1000),filters=FILTER,retrieval_mode=dict(type='string',enum=['lexical','bm25','hybrid']),score_meaning=string(200),
                  assessment_scope=dict(type='string',const='returned_excerpts'),conclusion_support=dict(type='string',const='unassessed'),
                  excerpts=array({'$ref':'#/components/schemas/Match'},20),model_used=dict(type='null'),generated_answer=dict(type='null'),notice=string(500))),
 'Process':obj(dict(id=ID,version=HASH,**SCHEMAS['ProcessInput']['properties'])),
 'Observation':obj(dict(original_value=LITERAL,value=dict(type=['number','null']),unit=NULL_TEXT,unit_source=NULL_TEXT,
                       original_units=FIELDS,evidence={'$ref':'#/components/schemas/Evidence'})),
 'Diagnosis':obj(dict(id=ID,state=dict(type='string',const='evaluated'),decision=dict(type='string',enum=['hold','meets_rule','outside_rule']),
                     hold_reasons=array(string(100),30),process_id=ID,process_version=HASH,event_time=string(40),received_time=string(40),
                     stream_id=string(80),sequence=integer(0,2**31-1),input_status=dict(type='string',enum=['complete','missing']),
                     missing_media=array(string(20),4),delay_ms=dict(type='integer'),delay_status=dict(type='string',enum=['late','on_time']),
                     order_status=dict(type='string',enum=['out_of_order','in_order','unknown']),observations=array({'$ref':'#/components/schemas/Observation'},10),
                     inputs=array({'$ref':'#/components/schemas/Evidence'}),execution_kind=dict(type='string',enum=['structured_rules','recognition_unavailable']),
                     model_used=dict(type='null'),business_approval=dict(type='string',const='not_granted'))),
 'Template':obj(dict(id=ID,name=string(160),version=HASH,source_sha256=HASH,source_version=integer(1,1),bytes=integer(1,MAX_SOURCE),
                    media_type=dict(type='string',const='application/vnd.navigator.template+json'),original_base64=string(90000),
                    fields=SCHEMAS['TemplateBody']['properties']['fields'],format=dict(type='string',const='navigator-cell-layout-v1'))),
 'ReportCell':obj(dict(field=string(80),cell=string(30),required=dict(type='boolean'),value=LITERAL,
                      status=dict(type='string',enum=['unknown','copied','needs_review']),evidence=dict(type=['object','null']),
                      source_field=NULL_TEXT,source_quote=dict(type=['string','null'],maxLength=8192),source_sha256=dict(type=['string','null']),
                      locator=dict(type=['object','null']),formula_injection_risk=dict(type='boolean'),value_type=dict(type='string',const='literal'),uncertainty_reason=NULL_TEXT)),
 'Report':obj(dict(id=ID,state=dict(type='string',const='draft'),template_id=ID,template_version=HASH,template_source_sha256=HASH,
                  template_source_version=integer(1,1),template_original_base64=string(90000),format=dict(type='string',const='navigator-cell-layout-v1'),
                  cells=array({'$ref':'#/components/schemas/ReportCell'},100,1),unresolved_fields=array(string(80),100),required_unresolved_fields=array(string(80),100),
                  review_required=dict(type='boolean',const=True),rendered_office_file=dict(type='boolean',const=False),external_submission=dict(type='boolean',const=False))),
 'Deleted':obj(dict(asset_id=ID,state=dict(type='string',const='deleted'),originals_and_derivatives_removed=dict(type='boolean',const=True))),
 'Capabilities':obj(dict(library=dict(type='object'),assist=dict(type='object'),diagnosis=dict(type='object'),report=dict(type='object'),execution=dict(type='object'),limits=dict(type='object'))),
})
SCHEMAS['Match']=obj(dict(**SCHEMAS['Segment']['properties'],retrieval_score=dict(type='number',minimum=0),
                         media_type=dict(type='string',enum=MEDIA),labels=array(string(80))),
    SCHEMAS['Segment']['required']+['retrieval_score','media_type','labels'])
SCHEMAS['Search']['properties']['retrieval_model_version']={'$ref':'#/components/schemas/RetrievalModelVersion'}
SCHEMAS['Answer']['properties']['retrieval_model_version']={'$ref':'#/components/schemas/RetrievalModelVersion'}
SCHEMAS['Job']['properties']['result']={'anyOf':[{'$ref':'#/components/schemas/Extraction'},{'type':'null'}]}
SCHEMAS['Job']['properties']['cancel_effect']=obj(dict(result_publication=dict(type='string',const='revoked'),processing_stop=dict(type='string',enum=['not_started','not_confirmed'])))
SCHEMAS['Source']['properties']['analysis']=obj(dict(state=SCHEMAS['Job']['properties']['state'],
                                                  result=SCHEMAS['Job']['properties']['result'],error=NULL_TEXT))
SCHEMAS['ReportCell']['properties']['evidence']={'anyOf':[{'$ref':'#/components/schemas/Evidence'},{'type':'null'}]}
SCHEMAS['ReportCell']['properties']['locator']={'anyOf':[{'$ref':'#/components/schemas/Locator'},{'type':'null'}]}
SCHEMAS['ReportCell']['properties']['value_type']={'type':'string','enum':['literal','calculated_decimal']}
SCHEMAS['ReportCell']['properties']['calculation']=obj(dict(reference=CALCULATION_REF,response_sha256=HASH,
    policy_version=string(100),task={'type':'object'},scope=array({'type':'object'},8),claim={'type':'object'}))

MODULE_RESPONSE_SCHEMAS={}

def register_module(schemas,operations,responses):
    SCHEMAS.update(schemas)
    for operation in operations:
        if not any(existing[:2]==operation[:2] for existing in OPERATIONS):
            OPERATIONS.append(operation)
    MODULE_RESPONSE_SCHEMAS.update(responses)

def response_schema(method,path):
    focused=MODULE_RESPONSE_SCHEMAS.get((method.lower(),path))
    if focused:
        return focused
    if path=='/v1/assets':
        return 'IngestResult'
    if path=='/v1/assets/{id}':
        return 'Deleted' if method.lower()=='delete' else 'Asset'
    if path.startswith('/v1/jobs/'):
        return 'Job'
    return {'capabilities':'Capabilities','search':'Search','answers':'Answer','processes':'Process',
            'diagnoses':'Diagnosis','templates':'Template','reports':'Report','grounded-answers':'GroundedAnswer','image-search':'ImageSearch','table-extractions':'TableExtraction','xlsx-templates':'XLSXTemplate','xlsx-reports':'XLSXReport'}.get(path.split('/')[2])

OPERATIONS = [
 ('get','/v1/capabilities',None,200,'read'),
 ('post','/v1/assets','AssetInput',202,'write'),
 ('get','/v1/assets/{id}',None,200,'read'),
 ('delete','/v1/assets/{id}','EmptyInput',200,'delete'),
 ('get','/v1/assets/{id}/versions/{version}/original',None,200,'read'),
 ('get','/v1/jobs/{id}',None,200,'read'),
 ('post','/v1/jobs/{id}/cancel','RevisionInput',200,'write'),
 ('post','/v1/jobs/{id}/retry','RevisionInput',200,'write'),
 ('post','/v1/search','QueryInput',200,'read'),
 ('post','/v1/answers','AnswerInput',201,'read'),
 ('get','/v1/answers/{id}',None,200,'read'),
 ('post','/v1/grounded-answers','GroundedInput',201,'read'),
 ('get','/v1/grounded-answers/{id}',None,200,'read'),
 ('post','/v1/image-search','ImageQuery',200,'read'),
 ('post','/v1/table-extractions','TableInput',201,'read'),
 ('get','/v1/table-extractions/{id}',None,200,'read'),
 ('post','/v1/xlsx-templates','XLSXTemplateInput',201,'export'),
 ('get','/v1/xlsx-templates/{id}',None,200,'read'),
 ('post','/v1/xlsx-reports','XLSXReportInput',201,'export'),
 ('get','/v1/xlsx-reports/{id}',None,200,'read'),
 ('get','/v1/xlsx-reports/{id}/file',None,200,'read'),
 ('post','/v1/processes','ProcessInput',201,'diagnose'),
 ('get','/v1/processes/{id}',None,200,'read'),
 ('post','/v1/diagnoses','DiagnosisInput',201,'diagnose'),
 ('get','/v1/diagnoses/{id}',None,200,'read'),
 ('post','/v1/templates','TemplateInput',201,'export'),
 ('get','/v1/templates/{id}',None,200,'read'),
 ('post','/v1/reports','ReportInput',201,'export'),
 ('get','/v1/reports/{id}',None,200,'read'),
]

def required_scopes(route,base):
    scopes={base}
    if route in ('/v1/answers','/v1/grounded-answers','/v1/table-extractions'):
        scopes.add('write')
    elif route.endswith(('/release-answers','/read-bindings')):
        scopes.update(('read','write'))
    elif route in ('/v1/diagnoses','/v1/reports','/v1/xlsx-reports'):
        scopes.add('read')
    return scopes

def openapi():
    from .grounded import GROUNDED_SCHEMAS,RETRIEVAL_MODEL_VERSION
    SCHEMAS.update(GROUNDED_SCHEMAS)
    SCHEMAS['RetrievalModelVersion']=RETRIEVAL_MODEL_VERSION
    from .table_adapter import TABLE_SCHEMAS
    SCHEMAS.update(TABLE_SCHEMAS)
    from .governance import GOVERNANCE_SCHEMAS,GOVERNANCE_OPERATIONS,GOVERNANCE_RESPONSE_SCHEMAS
    register_module(GOVERNANCE_SCHEMAS,GOVERNANCE_OPERATIONS,GOVERNANCE_RESPONSE_SCHEMAS)
    from .maintenance import MAINTENANCE_SCHEMAS,MAINTENANCE_OPERATIONS,MAINTENANCE_RESPONSE_SCHEMAS
    register_module(MAINTENANCE_SCHEMAS,MAINTENANCE_OPERATIONS,MAINTENANCE_RESPONSE_SCHEMAS)
    from .release_assist import RELEASE_ASSIST_SCHEMAS,RELEASE_ASSIST_OPERATIONS,RELEASE_ASSIST_RESPONSE_SCHEMAS
    register_module(RELEASE_ASSIST_SCHEMAS,RELEASE_ASSIST_OPERATIONS,RELEASE_ASSIST_RESPONSE_SCHEMAS)
    from .clarifications import CLARIFICATION_SCHEMAS,CLARIFICATION_OPERATIONS,CLARIFICATION_RESPONSE_SCHEMAS
    register_module(CLARIFICATION_SCHEMAS,CLARIFICATION_OPERATIONS,CLARIFICATION_RESPONSE_SCHEMAS)
    from .evidence_assist import ASSIST_SCHEMAS,ASSIST_OPERATIONS,ASSIST_RESPONSES
    register_module(ASSIST_SCHEMAS,ASSIST_OPERATIONS,ASSIST_RESPONSES)
    paths={}
    for method,path,schema,status,scope in OPERATIONS:
        op=dict(operationId=method+'_'+path.replace('/','_').replace('{','').replace('}',''),
                security=[{'localBearer':[]}], **{'x-required-scopes':sorted(required_scopes(path,scope))}, responses={str(status):dict(description='Successful local operation',content={'application/json':{'schema':{'$ref':'#/components/schemas/'+response_schema(method,path)} if response_schema(method,path) else {'type':'object'}}})})
        op['parameters']=[dict(name=name,in_='path',required=True,schema=integer(1,100) if name=='version' else ID) for name in re.findall(r'\{(\w+)\}',path)]
        for p in op['parameters']:
            p['in']=p.pop('in_')
        if schema:
            op['requestBody']=dict(required=True,content={'application/json':{'schema':{'$ref':'#/components/schemas/'+schema}}})
        if method in ('post','delete') and path not in ('/v1/search','/v1/image-search') and not path.endswith(('/event-queries','/release-queries')):
            op['parameters'].append(dict(name='Idempotency-Key',**{'in':'header'},required=True,schema=string(80,8,pattern='^[A-Za-z0-9_-]{8,80}$')))
        if path.startswith('/v1/projects/') and method=='post' and path.endswith(('/members','/source-bindings','/contexts','/governance-revisions','/revisions','/reviews','/approvals','/apply','/validate','/publish','/resume','/retry-failed-items','/cancel')):
            op['parameters'].append(dict(name='If-Match',**{'in':'header'},required=True,schema=string(66,66,pattern='^"[a-f0-9]{64}"$')))
        if path.endswith('/original') or path.endswith('/file'):
            op['responses'][str(status)]['content']={'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}
        for error in [401,403,404,408,409,410,411,412,413,415,422,428,500,501,503]:
            op['responses'][str(error)]=dict(description='Rejected; see machine-readable code',content={'application/json':{'schema':{'$ref':'#/components/schemas/Error'}}})
        paths.setdefault(path,{})[method]=op
    return dict(openapi='3.1.0',info=dict(title='NAVIGATOR Module local API',version='0.6.0',description='Local native extraction, grounded references, bounded typed evidence tasks and table arithmetic, human-defined conditional reference confirmation, project review and durable candidate-release maintenance. Optional pinned local 0.6B generation diagnostics only (quality gate failed); no accepted generative answers or live recognition; external reader disabled by default.'),
                servers=[dict(url='http://127.0.0.1:4340')],paths=paths,
                components=dict(securitySchemes=dict(localBearer=dict(type='http',scheme='bearer')),schemas=SCHEMAS),**{'x-limits':LIMITS})

# Register focused module contracts after foundational schemas are defined.
SCHEMAS['ImageQuery']=obj(dict(media_type=dict(type='string',enum=['image/png','image/jpeg']),content_base64=string(1500000),filters=FILTER,limit=integer(1,5)),['media_type','content_base64','filters'])
SCHEMAS['ImageMatch']=obj(dict(asset_id=ID,source_version=integer(1,100),analysis_version=string(100),source_sha256=HASH,pixel_sha256=HASH,image_width=integer(1,2000000),image_height=integer(1,2000000),retrieval_score=dict(type='number',minimum=0,maximum=1),media_type=dict(type='string',enum=['image/png','image/jpeg']),labels=array(string(80))))
SCHEMAS['ImageSearch']=obj(dict(mode=dict(type='string',const='pixel_grid_similarity'),query_source_sha256=HASH,query_pixel_sha256=HASH,feature_format=dict(type='string',const='rgb-grid16-v1'),filters=FILTER,results=array({'$ref':'#/components/schemas/ImageMatch'},5),score_meaning=string(200),semantic_search=dict(type='boolean',const=False),model_used=dict(type='null')))

SCHEMAS['XLSXTemplateInput']=obj(dict(name=string(160),content_base64=string(1500000)))
SCHEMAS['XLSXReportInput']=obj(dict(template_id=ID,grounded_answer_id=ID))
XLSX_CELL=obj(dict(sheet=string(80),cell=string(30,pattern='^[A-Z]{1,3}[1-9][0-9]{0,5}$'),value=string(8192)))
SCHEMAS['XLSXTemplate']=obj(dict(id=ID,name=string(160),profile=string(100),source_sha256=HASH,version=HASH,bytes=integer(1,1048576),media_type=string(100),original_base64=string(1500000),sheet=string(80),mappings=dict(type='object'),unknown_cells=array(string(30),64)))
SCHEMAS['XLSXReport']=obj(dict(id=ID,state=dict(type='string',const='draft'),profile=string(100),template_id=ID,template_source_sha256=HASH,template_version=HASH,grounded_answer_id=ID,source=dict(type='object'),cells=array(XLSX_CELL,4,4),unknown_cells=array(string(30),64),unresolved_fields=array(string(80),20),output_sha256=HASH,bytes=integer(1,1048576),media_type=string(100),file_base64=string(1500000),review_required=dict(type='boolean',const=True),external_submission=dict(type='boolean',const=False),notice=string(500),preservation=dict(type='object')))

XLSX_MEDIA='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
for key in ('XLSXTemplate','XLSXReport'):
    SCHEMAS[key]['properties']['profile']=dict(type='string',const='completion-inspection-reference-v1')
    SCHEMAS[key]['properties']['media_type']=dict(type='string',const=XLSX_MEDIA)
SCHEMAS['XLSXTemplate']['properties']['mappings']=obj({k:dict(type='string',const=v) for k,v in {'item':'B7','method':'C7','criterion':'D7','citation':'H7'}.items()})
SCHEMAS['XLSXReport']['properties']['source']={'$ref':'#/components/schemas/GroundedSupport'}
SCHEMAS['XLSXPreservation']=obj(dict(state=dict(type='string',const='succeeded'),processor_version=string(100),original_sha256=HASH,output_sha256=HASH,original_bytes=integer(1,1048576),output_bytes=integer(1,1048576),mapped_cell_values=array(XLSX_CELL,4,4),preserved_unmapped_cells=dict(type='boolean',const=True),preserved_merges=dict(type='boolean',const=True),preserved_cell_styles=dict(type='boolean',const=True),preserved_sheet_layout=dict(type='boolean',const=False),preserved_unrelated_layout=dict(type='boolean',const=True),layout_adjustments=array(obj(dict(sheet=dict(type='string',const='完了検査成績書'),row=dict(type='integer',const=7),height=dict(type='integer',const=180))),1,1),formula_count=dict(type='integer',const=0),model_used=dict(type='null'),validation=dict(type='string',const='saved_ooxml_semantics'),preservation_method=dict(type='string',const='original_package_with_artifact_authored_value_nodes')))
SCHEMAS['XLSXReport']['properties']['preservation']={'$ref':'#/components/schemas/XLSXPreservation'}

# A second bounded profile reuses the existing template/report/download routes.
SCHEMAS['LegacyXLSXTemplate']=SCHEMAS['XLSXTemplate']
SCHEMAS['LegacyXLSXReport']=SCHEMAS['XLSXReport']
NATIVE_MAPPING=obj(dict(field=string(80),cell=string(30)))
NATIVE_CELL=obj(dict(sheet=string(31),cell=string(30),value=dict(type='number')))
SCHEMAS['CalculationXLSXTemplate']=obj(dict(id=ID,name=string(160),profile=dict(type='string',const='calculation-report-xlsx-v1'),
    source_sha256=HASH,version=HASH,bytes=integer(1,1048576),media_type=dict(type='string',const=XLSX_MEDIA),original_base64=string(1500000),
    json_template_id=ID,json_template_version=HASH,sheet=string(31),mappings=array(NATIVE_MAPPING,10,1)))
SCHEMAS['CalculationXLSXReport']=obj(dict(id=ID,state=dict(type='string',const='draft'),profile=dict(type='string',const='calculation-report-xlsx-v1'),
    template_id=ID,template_source_sha256=HASH,template_version=HASH,report_id=ID,report_sha256=HASH,cells=array(NATIVE_CELL,10,1),
    provenance=array(obj(dict(field=string(80),cell=string(30),calculation=SCHEMAS['ReportCell']['properties']['calculation'])),10,1),
    unresolved_fields=array(string(80),100),output_sha256=HASH,bytes=integer(1,1048576),media_type=dict(type='string',const=XLSX_MEDIA),file_base64=string(1500000),
    review_required=dict(type='boolean',const=True),external_submission=dict(type='boolean',const=False),preservation=dict(type='object')))
SCHEMAS['XLSXTemplate']={'anyOf':[{'$ref':'#/components/schemas/LegacyXLSXTemplate'},{'$ref':'#/components/schemas/CalculationXLSXTemplate'}]}
SCHEMAS['XLSXReport']={'anyOf':[{'$ref':'#/components/schemas/LegacyXLSXReport'},{'$ref':'#/components/schemas/CalculationXLSXReport'}]}
SCHEMAS['XLSXTemplateInput']={'anyOf':[SCHEMAS['XLSXTemplateInput'],obj(dict(name=string(160),content_base64=string(1500000),json_template_id=ID,sheet=string(31),fields=array(string(80),10,1)))]}
SCHEMAS['XLSXReportInput']={'anyOf':[SCHEMAS['XLSXReportInput'],obj(dict(template_id=ID,report_id=ID))]}
