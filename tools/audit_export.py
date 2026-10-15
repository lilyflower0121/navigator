"""Audit an export with caller-provided forbidden terms; never print secrets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata

def norm(value):
    value=unicodedata.normalize('NFKC',value).casefold()
    return ''.join(c for c in value if unicodedata.category(c)!='Cf')

def audit(root, terms):
    root=Path(root).resolve();terms=[norm(x) for x in terms if x]
    records=[];text_hits=[];path_hits=[];private=[];secrets=[];excluded=[]
    exact_secrets=[v for k,v in os.environ.items() if any(x in k.upper() for x in ('TOKEN','SECRET','API_KEY','PASSWORD')) and len(v)>=20]
    signatures=[re.compile(x) for x in (r'AIza[0-9A-Za-z_-]{30,}',r'gh[pousr]_[A-Za-z0-9]{20,}',
                r'github_pat_[A-Za-z0-9_]{20,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]
    private_pattern=re.compile(r'/(?:Users|home|Volumes)/[^\s\"\x27<>]+')
    for p in sorted(root.rglob('*')):
        if not p.is_file() and not p.is_symlink():continue
        rel=p.relative_to(root).as_posix()
        if '.git' in p.relative_to(root).parts:continue
        if p.is_symlink():excluded.append(dict(path=rel,reason='symlink_not_exportable'));continue
        raw=p.read_bytes();records.append(dict(path=rel,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
        path=norm(rel)
        if any(x in path for x in terms):path_hits.append(rel)
        try:s=raw.decode('utf-8')
        except UnicodeError:excluded.append(dict(path=rel,reason='unexpected_binary_file'));continue
        if any(x in norm(s) for x in terms):text_hits.append(rel)
        if private_pattern.search(s):private.append(rel)
        if any(x in s for x in exact_secrets) or any(x.search(s) for x in signatures):secrets.append(rel)
        if (p.suffix.lower() in ('.pyc','.sqlite','.db','.safetensors','.gguf','.zip','.log','.parquet') or
                p.name=='.env' or any(x in p.parts for x in ('__pycache__','node_modules','.venv'))):
            excluded.append(dict(path=rel,reason='excluded_runtime_or_data_file'))
    report=dict(schema_version='navigator-export-audit-v1',files=len(records),bytes=sum(x['bytes'] for x in records),
                forbidden_term_count=len(terms),forbidden_text_hits=text_hits,forbidden_path_hits=path_hits,
                private_absolute_path_hits=private,credential_hits=secrets,excluded_file_hits=excluded,
                source_files=records,git_history_checked_separately=True)
    report['passed']=not any((text_hits,path_hits,private,secrets,excluded))
    return report

def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--root',default='.');a.add_argument('--deny-terms-file',required=True);a.add_argument('--output');args=a.parse_args()
    report=audit(args.root,json.loads(Path(args.deny_terms_file).read_text()))
    if args.output:
        with open(args.output,'x') as out:out.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='source_files'},ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)

if __name__=='__main__':main()
