"""Query-selected, indivisible original worksheet-row bundles.

Structural relationships are candidates, never semantic header/footnote proof.
No labels, source names, fixed row numbers or answers are supplied by evaluation.
"""
import re,unicodedata
from .contracts import canonical,digest
from .retrieval import bm25
POLICY='xlsx-original-row-bundles-v1'
MAX_PARTS=20
MAX_CHARACTERS=2000

def _key(s):return canonical(s['evidence'])
def _present(s):return [c for c in s['fields'].get('cells',[]) if c['kind']!='blank']
def _data(s):
    cs=_present(s)
    return len(cs)>=2 and any(c['kind'] in ('number','formula') for c in cs)
def _text(s):
    return unicodedata.normalize('NFKC',s['locator']['sheet']+' '+' '.join(str(c['value']) for c in _present(s) if c['value'] is not None))
def _part(s):
    return dict(evidence=s['evidence'],source_sha256=s['source_sha256'],locator=s['locator'],quote=s['quote'],start_char=0,end_char=len(s['quote']),offset_unit='unicode_codepoints',quote_context_sha256=digest(s['quote'].encode()))

def build(query,selected,material):
    rows=[s for s in selected if s['locator']['kind']=='xlsx_row']
    if not rows:return None
    if len(rows)!=len(selected):
        return dict(policy=POLICY,spans=[],blocked_reason='mixed_media_table_context_unverified',part_count=0,characters=0,context_truncated=False,coverage='mixed_source_relationships_unverified')
    groups={}
    for s in material:
        if s['locator']['kind']=='xlsx_row':groups.setdefault((s['evidence']['asset_id'],s['locator']['sheet']),[]).append(s)
    for group in groups.values():group.sort(key=lambda s:s['locator']['row'])
    seeds=[s for s in rows if _data(s)]
    if not seeds:
        # Text-only tables have no reliable inferred data/header split. Preserve
        # their whole sheet or explicitly refuse the context budget.
        seeds=rows[:4]
    normalized=unicodedata.normalize('NFKC',query)
    # A literal unit/sheet qualifier in the question is a source condition,
    # not a gold answer selector. Preserve all matching sheets if ambiguous.
    def explicit_sheet(s):
        name=unicodedata.normalize('NFKC',s['locator']['sheet'])
        return name in normalized or any(p and p in normalized for p in re.findall(r'\(([^()]*)\)',name))
    if any(explicit_sheet(s) for s in seeds):seeds=[s for s in seeds if explicit_sheet(s)]
    scores=bm25(normalized,[_text(s) for s in seeds])
    order=sorted(range(len(seeds)),key=lambda i:(-scores[i],i))[:4]
    units={}
    for i in order:
        seed=seeds[i];group_key=(seed['evidence']['asset_id'],seed['locator']['sheet']);group=groups[group_key];at=next(j for j,s in enumerate(group) if _key(s)==_key(seed))
        if _data(seed):
            lo=hi=at
            while lo and _data(group[lo-1]) and group[lo-1]['locator']['row']+1==group[lo]['locator']['row']:lo-=1
            while hi+1<len(group) and _data(group[hi+1]) and group[hi]['locator']['row']+1==group[hi+1]['locator']['row']:hi+=1
            start=lo
            while start and not _data(group[start-1]):start-=1
            end=hi+1
            while end<len(group) and not _data(group[end]):end+=1
            context=[s for s in group[start:lo]+group[hi+1:end] if _present(s)]
            # Keep immediate original data neighbors (e.g. parent/detail rows)
            # intact; their semantic relationship remains unverified.
            context+=group[max(lo,at-1):min(hi+1,at+2)]
            # Numeric period/year headings share cell types with data. Retain
            # the first two rows of each run as unverified boundary candidates.
            context+=group[lo:min(hi+1,lo+2)]
        else:
            lo,hi=0,len(group)-1;context=group
        unit_key=(*group_key,lo,hi)
        unit=units.setdefault(unit_key,dict(primary=[],rows={}))
        unit['primary'].append(seed['evidence'])
        for s in [*context,seed]:unit['rows'][_key(s)]=s
    spans=[]
    for unit in units.values():
        ordered=sorted(unit['rows'].values(),key=lambda s:s['locator']['row'])
        parts=[_part(s) for s in ordered]
        spans.append(dict(kind='xlsx_table_unit',quote='\n'.join(p['quote'] for p in parts),parts=parts,primary_refs=unit['primary'],structure_semantics_verified=False))
    count=sum(len(s['parts']) for s in spans);chars=sum(len(s['quote']) for s in spans)
    reason='table_context_budget_exceeded' if count>MAX_PARTS or chars>MAX_CHARACTERS else None
    return dict(policy=POLICY,spans=[] if reason else spans,blocked_reason=reason,selected_primary_refs=[seeds[i]['evidence'] for i in order],
        candidate_data_rows=len(seeds),part_count=count,characters=chars,limits=dict(parts=MAX_PARTS,characters=MAX_CHARACTERS),
        coverage='structural_candidate_units_only_query_and_footnote_applicability_unverified',context_truncated=False)
