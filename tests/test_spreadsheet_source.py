"""Real ZIP source extraction, immutable row provenance and existing job boundaries.

Fixtures here are synthetic; no experiment queries or answer keys are used.
"""
import base64
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from navigator.contracts import APIError, SCHEMAS, canonical, digest, validate
from navigator.library import Library
from navigator.parsing import parse_source
from navigator.store import Store
from navigator import xlsx_adapter
from navigator.xlsx_ingest import MEDIA, MAX_SOURCE_BYTES, source_snapshot

NS = xlsx_adapter.NS


def workbook(rows=None, *, formula=True, extra=None, columns=True):
    if rows is None:
        rows = ('<row r="1"><c r="A1" t="inlineStr"><is><t>Synthetic header</t></is></c><c r="B1"/></row>'
                '<row r="2" hidden="1"><c r="A2" t="inlineStr"><is><t>Category</t></is></c>'
                '<c r="B2"><v>1.2300</v></c>'
                + ('<c r="C2"><f t="shared" si="0" ref="C2:C3">1+1</f><v>2</v></c><c r="D2"><f>NOW()</f></c>' if formula else '')
                + '<c r="E2" t="b"><v>0</v></c><c r="F2" t="e"><v>#DIV/0!</v></c>'
                '<c r="G2" t="str"><v>=not executable</v></c><c r="H2" t="s"><v>0</v></c>'
                '<c r="I2"><v>0.123456789123456789</v></c></row><row r="3"/>')
    parts = {
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        'xl/workbook.xml':f'<workbook xmlns="{NS}" xmlns:r="{xlsx_adapter.RELNS}"><sheets><sheet name="Fixture" sheetId="1" state="hidden" r:id="s1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="s1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>',
        'xl/styles.xml':f'<styleSheet xmlns="{NS}"><fonts><font/></fonts><fills><fill/></fills><borders><border/></borders><cellXfs><xf/></cellXfs></styleSheet>',
        'xl/sharedStrings.xml':f'<sst xmlns="{NS}"><si><r><t>Shared </t></r><r><t>text</t></r></si></sst>',
        'xl/worksheets/sheet1.xml':f'<worksheet xmlns="{NS}"><dimension ref="A1:I3"/>'
             + ('<cols><col min="2" max="2" hidden="1"/><col min="10" max="16384" width="12"/></cols>' if columns else '')
             + f'<sheetData>{rows}</sheetData><mergeCells><mergeCell ref="A1:C1"/></mergeCells></worksheet>',
    }
    parts.update(extra or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_STORED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return out.getvalue()


def claim(raw, version=1):
    return dict(raw=raw,sha=digest(raw),asset='a'*32,version=version,meta=dict(media_type=MEDIA))


class TabularParserTests(unittest.TestCase):
    def error(self, code, action):
        with self.assertRaises(APIError) as caught:
            action()
        self.assertEqual(caught.exception.code,code)

    def test_raw_values_formulas_missing_caches_merges_and_visibility(self):
        raw = workbook()
        with patch('subprocess.Popen',side_effect=AssertionError('source parser must not execute a process')):
            result = parse_source(claim(raw))
        validate({k:v for k,v in result.items() if k!='state'},SCHEMAS['Extraction'])
        self.assertEqual(len(result['segments']),3)
        first, row, empty = result['segments']
        self.assertEqual(row['locator'],dict(kind='xlsx_row',sheet='Fixture',row=2))
        cells = {c['address']:c for c in row['fields']['cells']}
        self.assertEqual(cells['B2']['value'],1.23)
        self.assertEqual(cells['B2']['raw_value'],'1.2300')
        self.assertIn('B2=1.2300',row['quote'])
        self.assertTrue(cells['B2']['row_hidden']);self.assertTrue(cells['B2']['column_hidden'])
        self.assertEqual(row['fields']['sheet_state'],'hidden')
        self.assertEqual(cells['C2']['formula'],'1+1')
        self.assertEqual(cells['C2']['formula_attributes'],dict(t='shared',si='0',ref='C2:C3'))
        self.assertIsNone(cells['C2']['value']);self.assertEqual(cells['C2']['cached_value'],2)
        self.assertEqual(cells['C2']['cache_state'],'present')
        self.assertEqual(cells['D2']['cache_state'],'missing');self.assertIsNone(cells['D2']['cached_value'])
        self.assertFalse(cells['E2']['value']);self.assertEqual(cells['F2']['kind'],'error')
        self.assertEqual(cells['G2']['kind'],'text');self.assertIsNone(cells['G2']['formula'])
        self.assertEqual(cells['H2']['value'],'Shared text')
        self.assertEqual(cells['I2']['value'],'0.123456789123456789')
        a,b,c = first['fields']['cells']
        self.assertEqual(a['merged_anchor'],'A1');self.assertEqual(c['merged_range'],'A1:C1')
        self.assertTrue(b['stored']);self.assertFalse(c['stored'])
        self.assertEqual(c['kind'],'blank');self.assertEqual(empty['fields']['cells'],[])
        self.assertEqual(row['fields']['headers'][0]['role'],'candidate')
        self.assertEqual(row['fields']['headers'][0]['merged_range'],'A1:C1')
        self.assertIsNone(next(h for h in empty['fields']['headers'] if h['address']=='A2')['merged_range'])
        self.assertEqual(result['processor_detail']['snapshot_sha256'],digest(source_snapshot(raw)))
        self.assertEqual(result['processor_detail']['source_sha256'],digest(raw))
        self.assertFalse(result['processor_detail']['formula_execution'])

    def test_hash_source_version_and_snapshot_binding(self):
        raw = workbook()
        self.error('source_integrity_failed',lambda:parse_source(dict(claim(raw),sha='0'*64)))
        first, second = parse_source(claim(raw)),parse_source(claim(raw,2))
        self.assertEqual(first['analysis_version'],second['analysis_version'])
        self.assertNotEqual(first['segments'][0]['id'],second['segments'][0]['id'])
        modified = workbook(extra={'docProps/core.xml':'<properties>changed metadata</properties>'})
        updated = parse_source(claim(modified))
        self.assertNotEqual(first['analysis_version'],updated['analysis_version'])
        self.assertNotEqual(first['processor_detail']['snapshot_sha256'],updated['processor_detail']['snapshot_sha256'])

    def test_empty_text_and_explicit_empty_cache_differ_from_missing_cache(self):
        raw=workbook('<row r="2"><c r="A2" t="str"><v/></c><c r="B2"><f>1+1</f><v/></c><c r="C2"><f>1+1</f></c></row>')
        cells=parse_source(claim(raw))['segments'][0]['fields']['cells']
        self.assertEqual(cells[0]['kind'],'text');self.assertEqual(cells[0]['value'],'')
        self.assertEqual(cells[0]['raw_value'],'')
        self.assertEqual(cells[1]['cache_state'],'present');self.assertEqual(cells[1]['raw_value'],'')
        self.assertEqual(cells[2]['cache_state'],'missing');self.assertIsNone(cells[2]['raw_value'])

    def test_shared_and_inline_display_text_excludes_phonetic_guides(self):
        cases = [
            ('<t>品目数</t><rPh sb="0" eb="3"><t>ヒンモクスウ</t></rPh><phoneticPr fontId="0"/>','品目数'),
            ('<r><rPr><b/></rPr><t xml:space="preserve">全 </t></r>'
             '<r><t>国</t></r><rPh sb="0" eb="3"><t>ゼンクニ</t></rPh>','全 国'),
            ('<t xml:space="preserve"> leading and trailing </t>'
             '<rPh sb="1" eb="4"><t>guide</t></rPh>',' leading and trailing '),
            ('<rPh sb="0" eb="0"><t>guide only</t></rPh><phoneticPr fontId="0"/>',''),
        ]
        for content,expected in cases:
            with self.subTest(expected=expected):
                rows = f'<row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2" t="inlineStr"><is>{content}</is></c></row>'
                raw = workbook(rows,extra={'xl/sharedStrings.xml':f'<sst xmlns="{NS}"><si>{content}</si></sst>'})
                original_sha=digest(raw)
                result=parse_source(claim(raw))
                self.assertEqual(result['processor_version'],'xlsx-source-rows-v3')
                self.assertEqual([c['value'] for c in result['segments'][0]['fields']['cells']],[expected,expected])
                self.assertEqual(result['segments'][0]['source_sha256'],original_sha)
                self.assertEqual(result['processor_detail']['source_sha256'],original_sha)
                self.assertNotIn('ヒンモクスウ',result['segments'][0]['quote'])
                self.assertNotIn('ゼンクニ',result['segments'][0]['quote'])
                self.assertEqual(digest(raw),original_sha)

    def test_source_byte_and_complete_row_bounds(self):
        raw = workbook(extra={'docProps/core.xml':'<properties>'+'x'*70000+'</properties>'})
        self.assertGreater(len(raw),65536)
        self.assertEqual(parse_source(claim(raw))['state'],'succeeded')
        self.error('source_byte_limit',lambda:parse_source(claim(b'x'*(MAX_SOURCE_BYTES+1))))
        rows = ''.join(f'<row r="{r}"><c r="A{r}" t="str"><v>x</v></c></row>' for r in range(1,258))
        self.error('extraction_limit',lambda:parse_source(claim(workbook(rows))))
        self.error('extraction_limit',lambda:parse_source(claim(workbook('<row r="1"><c r="A1" t="str"><v>'+'x'*8193+'</v></c></row>'))))

    def test_unsafe_zip_xml_external_macro_and_bad_structures(self):
        for prefix in ('<!DOCTYPE x [<!ENTITY x "bad">]>','<!ENTITY x "bad">','\x00'):
            self.error('xlsx_invalid',lambda:source_snapshot(workbook(extra={'bad.xml':prefix+'<x/>'})))
        self.error('xlsx_invalid',lambda:source_snapshot(workbook(extra={'../unsafe.xml':'<x/>'})))
        self.error('xlsx_feature_unsupported',lambda:source_snapshot(workbook(extra={'xl/vbaProject.bin':b'bad'})))
        self.error('xlsx_external_link_unsupported',lambda:source_snapshot(workbook(extra={'bad.rels':'<Relationships><Relationship TargetMode="External" Target="https://example.test"/></Relationships>'})))
        self.error('xlsx_cell_invalid',lambda:source_snapshot(workbook('<row r="1"><c r="A2"><v>1</v></c></row>')))
        self.error('xlsx_invalid',lambda:source_snapshot(workbook('<row r="1"><c r="A1"><v>1_0</v></c></row>')))
        self.error('xlsx_invalid',lambda:source_snapshot(workbook('<row r="1"><c r="A1" t="s"><v>-1</v></c></row>')))
        self.error('xlsx_xml_limit',lambda:source_snapshot(workbook(extra={'bad.xml':'<x>'*34+'</x>'*34})))
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('bomb.xml','<x>'+'x'*20000+'</x>')
        self.error('xlsx_expansion_limit',lambda:source_snapshot(out.getvalue()))

    def test_template_adapter_policy_remains_strict(self):
        self.error('xlsx_formula_unsupported',lambda:xlsx_adapter._snapshot(workbook(columns=False)))
        snapshot = xlsx_adapter._snapshot(workbook('<row r="2"><c r="B2"><v>1.2300</v></c></row>',formula=False,columns=False))
        self.assertEqual(snapshot['Fixture']['cells']['B2']['value'],'1.23')


    def test_later_header_candidates_and_limit_are_retained(self):
        raw=workbook(''.join(f'<row r="{r}"><c r="A{r}" t="str"><v>Header {r}</v></c></row>' for r in range(1,11)))
        last=parse_source(claim(raw))['segments'][-1]
        self.assertEqual(last['fields']['headers'][-1]['row'],9)
        self.assertEqual(last['fields']['headers'][-1]['text'],'Header 9')
        self.assertIsNone(last['fields']['headers'][-1]['merged_range'])
        rows=''.join(f'<row r="{r}"><c r="A{r}" t="str"><v>a</v></c><c r="B{r}" t="str"><v>b</v></c></row>' for r in range(1,130))
        self.error('xlsx_header_candidate_limit',lambda:parse_source(claim(workbook(rows))))


class TabularLibraryTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        self.allowed=True
        self.library=Library(self.store,lambda *args:self.allowed)
        self.ctx=dict(tenant='tabular-test',actor='owner',revision=1)

    def tearDown(self):
        self.store.close()

    def ingest(self, raw):
        body=dict(name='synthetic.xlsx',media_type=MEDIA,content_base64=base64.b64encode(raw).decode())
        return self.store.mutate(self.ctx,'tabular-ingest-0001','ingest',body,
            lambda db:self.library.ingest(db,self.ctx,body))[1]

    def test_async_search_resolve_and_delete_use_existing_boundary(self):
        raw=workbook(); ingested=self.ingest(raw)
        self.assertEqual(ingested['job']['state'],'queued')
        self.assertTrue(self.library.run_once())
        with self.store.tx() as db:
            job=self.store.job(db,self.ctx['tenant'],ingested['job']['id'])
            self.assertEqual(job['state'],'succeeded',job)
            original,sha=self.library.original(db,self.ctx['tenant'],ingested['asset_id'],1)
            self.assertEqual(original,raw);self.assertEqual(sha,digest(raw))
            result=self.library.search(db,self.ctx['tenant'],dict(query='Category',filters=dict(asset_ids=[ingested['asset_id']])))
            self.assertTrue(result['results'])
            ref=result['results'][0]['evidence']
            source=self.library.resolve(db,self.ctx['tenant'],ref)
            self.assertEqual(source['locator']['kind'],'xlsx_row')
        with self.store.tx() as db:
            self.library.delete(db,self.ctx['tenant'],ingested['asset_id'])
        with self.store.tx() as db:
            with self.assertRaises(APIError):
                self.library.resolve(db,self.ctx['tenant'],ref)

    def test_revoked_worker_cannot_publish(self):
        value=self.ingest(workbook());self.allowed=False
        self.assertFalse(self.library.run_once())
        with self.store.tx() as db:
            job=self.store.job(db,self.ctx['tenant'],value['job']['id'])
            self.assertNotEqual(job['state'],'succeeded')
            self.assertIsNone(job['result'])

    def test_revoked_during_parser_cannot_publish(self):
        value=self.ingest(workbook())
        def parse_then_revoke(source):
            result=parse_source(source)
            self.allowed=False
            return result
        with patch('navigator.library.parse_source',side_effect=parse_then_revoke):
            self.assertTrue(self.library.run_once())
        with self.store.tx() as db:
            job=self.store.job(db,self.ctx['tenant'],value['job']['id'])
            self.assertNotEqual(job['state'],'succeeded')
            self.assertFalse(job['result'])
            self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0],0)

    def test_nested_cell_tamper_fails_evidence_integrity(self):
        value=self.ingest(workbook());self.library.run_once()
        with self.store.tx() as db:
            job=self.store.job(db,self.ctx['tenant'],value['job']['id'])
            segment=job['result']['segments'][1]
            ref=segment['evidence']
            self.library.resolve(db,self.ctx['tenant'],ref)
            segment['fields']['cells'][1]['raw_value']='invented'
            db.execute('UPDATE segments SET data=? WHERE id=?',(canonical(segment),segment['id']))
        with self.store.tx() as db:
            with self.assertRaises(APIError) as caught:
                self.library.resolve(db,self.ctx['tenant'],ref)
            self.assertEqual(caught.exception.code,'evidence_integrity_failed')

    def test_api_ingest_job_cancel_retry_search_original_delete(self):
        from navigator.api import API, Auth
        token='tabular-synthetic-owner-token'
        auth=Auth({token:dict(**self.ctx,scopes=['read','write','delete','diagnose','export'])})
        api=API(self.store,auth)
        raw=workbook()
        status,value,_=api.dispatch('POST','/v1/assets',token,
            dict(name='synthetic.xlsx',media_type=MEDIA,content_base64=base64.b64encode(raw).decode()),'tabular-api-ingest-0001')
        self.assertEqual(status,202)
        job_id=value['job']['id'];asset=value['asset_id']
        self.assertEqual(value['job']['processor_version'],'xlsx-source-rows-v3')
        _,cancelled,_=api.dispatch('POST',f'/v1/jobs/{job_id}/cancel',token,
            dict(expected_revision=value['job']['revision']),'tabular-api-cancel-0001')
        self.assertEqual(cancelled['state'],'cancelled')
        _,retried,_=api.dispatch('POST',f'/v1/jobs/{job_id}/retry',token,
            dict(expected_revision=cancelled['revision']),'tabular-api-retry-0001')
        self.assertEqual(retried['state'],'queued')
        self.assertTrue(api.library.run_once())
        _,job,_=api.dispatch('GET',f'/v1/jobs/{job_id}',token)
        self.assertEqual(job['state'],'succeeded')
        _,search,_=api.dispatch('POST','/v1/search',token,
            dict(query='Category',filters=dict(asset_ids=[asset],media_types=[MEDIA])),'tabular-api-search-0001')
        self.assertEqual(search['results'][0]['locator']['kind'],'xlsx_row')
        _,original,_=api.dispatch('GET',f'/v1/assets/{asset}/versions/1/original',token)
        self.assertEqual(original,raw)
        _,deleted,_=api.dispatch('DELETE',f'/v1/assets/{asset}',token,{},key='tabular-api-delete-0001')
        self.assertTrue(deleted['originals_and_derivatives_removed'])
        with self.assertRaises(APIError):
            api.dispatch('GET',f'/v1/assets/{asset}/versions/1/original',token)


if __name__=='__main__':
    unittest.main()
