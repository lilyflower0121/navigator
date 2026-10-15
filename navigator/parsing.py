"""Actual bounded extraction, adapted from Analytics backend/processing.py.

No replay fixtures and no inference. Physical CSV line ranges use reader.line_num,
so quoted newlines do not turn a logical row number into a false source locator.
"""
import csv
import io
import json
from .contracts import APIError,LIMITS,MAX_SOURCE,MAX_PDF,MAX_IMAGE,MAX_XLSX_SOURCE,SCHEMAS,digest,json_load,validate

PROCESSOR='structured-text-v1'
XLSX_MEDIA='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

def parse_source(claim):
    media=claim['meta']['media_type']
    if not claim['raw'] or len(claim['raw'])>(MAX_PDF if media=='application/pdf' else MAX_IMAGE if media in ('image/png','image/jpeg') else MAX_XLSX_SOURCE if media==XLSX_MEDIA else MAX_SOURCE):
        raise APIError(413,'source_byte_limit')
    if digest(claim['raw'])!=claim['sha']:
        raise APIError(409,'source_integrity_failed')
    if media=='application/pdf':
        from .pdf_adapter import parse_pdf
        result=parse_pdf(claim)
        if result['state']=='succeeded':
            validate({k:v for k,v in result.items() if k!='state'},SCHEMAS['Extraction'])
        return result
    if media in ('image/png','image/jpeg'):
        from .image_adapter import parse_image
        return parse_image(claim)
    if media==XLSX_MEDIA:
        from .xlsx_ingest import parse_xlsx
        return parse_xlsx(claim)
    if media not in ('text/plain','text/markdown','text/csv','application/json'):
        return dict(state='blocked',error='media_parser_unavailable',segments=[],capability=media,
                    processor_version=PROCESSOR,model_used=None)
    try:
        text=claim['raw'].decode('utf-8-sig')
    except UnicodeError as error:
        raise APIError(422,'invalid_utf8') from error
    if '\x00' in text:
        raise APIError(422,'binary_text_rejected')
    analysis=PROCESSOR+':'+digest(dict(source_sha256=claim['sha'],media_type=media,processor=PROCESSOR))
    segments=[]
    def add(quote,location,fields):
        if not quote.strip():
            return
        if len(segments)>=LIMITS['segments'] or len(quote)>8192:
            raise APIError(413,'extraction_limit')
        id=digest(dict(asset=claim['asset'],version=claim['version'],analysis=analysis,location=location))[:32]
        segments.append(dict(id=id,quote=quote,locator=location,fields=fields,
            evidence=dict(asset_id=claim['asset'],source_version=claim['version'],analysis_version=analysis,segment_id=id),
            source_sha256=claim['sha'],claim_kind='source_quote',model_used=None))
    if media=='text/csv':
        # CSV physical lines are CR/LF/CRLF, not Unicode separators such as
        # U+2028, which are ordinary characters in a CSV cell.
        lines=io.StringIO(text,newline='').readlines()
        reader=csv.reader(io.StringIO(text,newline=''),strict=True)
        try:
            header=next(reader)
            if not header or len(header)>64 or len(set(header))!=len(header) or any(not h or len(h)>100 for h in header):
                raise APIError(422,'invalid_csv_header')
            header_end=reader.line_num
            previous=header_end
            for row in reader:
                end=reader.line_num
                if len(row)!=len(header):
                    raise APIError(422,'csv_column_mismatch')
                # csv.reader tracks physical lines, including embedded newlines.
                add(''.join(lines[previous:end]).rstrip('\r\n'),dict(kind='lines',line_start=previous+1,line_end=end,header_line_end=header_end),dict(zip(header,row)))
                previous=end
        except StopIteration:
            raise APIError(422,'empty_csv')
        except csv.Error as error:
            raise APIError(422,'invalid_csv') from error
    elif media=='application/json':
        data=json_load(text)
        decoder=json.JSONDecoder()
        def skip(index):
            while index<len(text) and text[index] in ' \t\r\n':
                index+=1
            return index
        def visit(value,index=0,pointer='',depth=0):
            if depth>12:
                raise APIError(413,'json_nesting_limit')
            index=skip(index)
            if isinstance(value,dict):
                index=skip(index+1)
                for position in range(len(value)):
                    key,end=decoder.raw_decode(text,index)
                    index=skip(end)+1  # colon, already checked by json_load
                    index=visit(value[key],index,pointer+'/'+key.replace('~','~0').replace('/','~1'),depth+1)
                    index=skip(index)
                    if position+1<len(value):
                        index=skip(index+1)
                return index+1
            elif isinstance(value,list):
                index=skip(index+1)
                for i,v in enumerate(value):
                    index=skip(visit(v,index,pointer+'/'+str(i),depth+1))
                    if i+1<len(value):
                        index=skip(index+1)
                return index+1
            else:
                _,end=decoder.raw_decode(text,index)
                # Preserve the original escaped string/number token. A parsed
                # value and its source spelling are distinct pieces of evidence.
                add(text[index:end],dict(kind='json_pointer',pointer=pointer,char_start=index,char_end=end,offset_unit='unicode_codepoints'),{pointer: value})
                return end
        visit(data)
    else:
        for i,line in enumerate(text.splitlines(),1):
            add(line,dict(kind='lines',line_start=i,line_end=i),{})
    if not segments:
        raise APIError(422,'no_extractable_content')
    result=dict(state='succeeded',analysis_version=analysis,processor_version=PROCESSOR,
                model_used=None,segments=segments,interpretation='structural_extraction_only')
    # Never commit a successful extraction which the published API cannot serve.
    try:
        validate({k:v for k,v in result.items() if k!='state'},SCHEMAS['Extraction'])
    except APIError as error:
        raise APIError(413,'extraction_contract_limit') from error
    return result
