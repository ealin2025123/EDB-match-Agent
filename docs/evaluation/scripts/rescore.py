from decimal import Decimal, InvalidOperation
from pathlib import Path
import json

def number(value):
    try:
        v=Decimal(str(value).replace(',',''))
        return v if v.is_finite() else None
    except InvalidOperation:return None

def identity(row):return str(row.get('indicator_name','')).strip(),str(row.get('end_date',''))

def equivalent_value(row,gold):
    value,expected=number(row.get('indicator_data')),number(gold.get('indicator_data'))
    if value is None or expected is None:return False
    unit=str(row.get('indicator_unit','')).strip();target=str(gold.get('indicator_unit','')).strip()
    if unit==target:return value==expected
    # Explicit dimensional conversion only; never infer ratios from names.
    currency={'元':Decimal(1),'万元':Decimal(10000),'亿元':Decimal(100000000)}
    return unit in currency and target in currency and value*currency[unit]==expected*currency[target]

def score(rows,gold):
    """One-to-one matching with explicit unit equivalence; duplicates reduce precision."""
    remaining=list(gold);correct=0;issues=[]
    for row in rows:
        found=next((i for i,g in enumerate(remaining) if identity(row)==identity(g)
                    and equivalent_value(row,g)
                    and (not row.get('indicator_code') or str(row['indicator_code'])==str(g['indicator_code']))),None)
        if found is None:issues.append(row)
        else:correct+=1;remaining.pop(found)
    return {'correct':correct,'predicted':len(rows),'expected':len(gold),
            'precision':correct/len(rows) if rows else None,
            'coverage':correct/len(gold) if gold else None,
            'wrong_records':issues,'missing_records':remaining,
            'gold_pass':not issues and not remaining}
if __name__=='__main__':
 root=Path(__file__).resolve().parents[1]
 sources=json.loads((root/'synthetic-sources.json').read_text(encoding='utf-8'))
 gold={x['case']['id']:x['case']['gold'] for x in sources}
 for stage in ['A_generation','B_matching_review','C_preflight','D_reuse']:
  rows=json.loads((root/(stage+'.json')).read_text(encoding='utf-8'))
  for r in rows:
   if 'rows' in r:
    result=score(r['rows'],gold[r['case_id']])
    assert result==r['score'], 'Stored score differs: '+r['case_id']
  print(stage, 'verified',len(rows),'observations')
