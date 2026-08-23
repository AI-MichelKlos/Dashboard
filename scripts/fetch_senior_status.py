#!/usr/bin/env python3
from __future__ import annotations
import json, math, re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import build_dashboard as api

BASE=Path(__file__).resolve().parents[1]
OUT=BASE/'data'/'senior-status.json'

def norm(x):return re.sub(r'[^a-z0-9]+',' ',str(x or '').lower().replace('æ','ae').replace('ø','oe').replace('å','aa')).strip()
def walk(x):
    if isinstance(x,dict):
        yield x
        for v in x.values():yield from walk(v)
    elif isinstance(x,list):
        for v in x:yield from walk(v)
def blob(d):return norm(' '.join(f'{k} {v}' for k,v in d.items() if isinstance(v,(str,int,float))))
def records(payload):
    cols=payload.get('columns');rows=payload.get('rows')
    if not isinstance(cols,list) or not isinstance(rows,list):raise RuntimeError('Uventet Jobindsats-format')
    return [dict(zip(cols,r)) for r in rows]
def num(v):
    if v is None or isinstance(v,bool):return None
    if isinstance(v,(int,float)):n=float(v)
    else:
        t=str(v).strip().replace('\xa0','').replace(' ','')
        if not t or t in {'-','.','..'}:return None
        if ',' in t:t=t.replace('.','').replace(',','.')
        try:n=float(t)
        except:return None
    return n if math.isfinite(n) else None
def find_table(payload):
    candidates=[]
    for d in walk(payload):
        tid=d.get('table_id')
        if not tid:continue
        b=blob(d);score=0
        if 'arbejdsmarkedsstatus for seniorer' in b:score+=1000
        for word in ('arbejdsmarkedsstatus','seniorer','senior'):score+=100 if word in b else 0
        candidates.append((score,len(b),str(tid),b))
    candidates.sort(reverse=True)
    if not candidates or candidates[0][0]<300:raise RuntimeError('Kunne ikke identificere seniormålingen i Jobindsats')
    return candidates[0][2]
def hierarchies(spec):
    out={}
    for d in walk(spec):
        hid=d.get('hierarchy_id')
        if isinstance(hid,str) and len(json.dumps(d,ensure_ascii=False))>len(json.dumps(out.get(hid,{}),ensure_ascii=False)):out[hid]=d
    return list(out.values())
def find_hierarchy(spec,words,preferred=()):
    choices=[]
    for d in hierarchies(spec):
        hid=str(d.get('hierarchy_id'));b=norm(json.dumps(d,ensure_ascii=False));score=0
        if hid in preferred:score+=1000-preferred.index(hid)*20
        for word in words:
            w=norm(word);score+=200 if w in norm(hid) else 0;score+=60 if w in b else 0
        choices.append((score,len(b),d))
    choices.sort(reverse=True,key=lambda x:(x[0],x[1]))
    if not choices or choices[0][0]<=0:raise RuntimeError(f'Hierarki ikke fundet: {words}')
    return choices[0][2]
def country_value(h):
    for d in walk(h):
        if isinstance(d.get('value_id'),str) and ('hele landet' in blob(d) or 'hele danmark' in blob(d)):return d['value_id']
    return '/'
def deepest_level(h):
    levels={}
    for d in walk(h):
        lid=d.get('level_id')
        if isinstance(lid,str):levels.setdefault(lid,set()).update(str(x.get('value_id')) for x in walk(d) if isinstance(x.get('value_id'),str))
    return max(levels,key=lambda k:len(levels[k])) if levels else None
def best_col(rows,include,exclude=(),distinct=False):
    cols=list(rows[0]);choices=[]
    for c in cols:
        n=norm(c);score=sum(100 for x in include if norm(x) in n)-sum(150 for x in exclude if norm(x) in n)
        if score>0:
            d=len({str(r.get(c)) for r in rows if r.get(c) not in (None,'')});choices.append((score+(min(d,100) if distinct else 0),d,c))
    if not choices:raise RuntimeError(f'Kolonne ikke fundet: {include}. {cols}')
    choices.sort(reverse=True);return choices[0][2]
def pkey(p):
    m=re.fullmatch(r'(\d{4})M(\d{2})',str(p));return (int(m.group(1)),int(m.group(2))) if m else (0,0)
def main():
    tid=find_table(api.jobindsats_get('tables?format=json'));spec=api.jobindsats_get(f'table/{tid}?format=json')
    geo=find_hierarchy(spec,['område','geografi','kommune'],('_hele_landet','_nykom'));age=find_hierarchy(spec,['alder']);status=find_hierarchy(spec,['arbejdsmarkedsstatus','status']);level=deepest_level(age)
    path=f'data/{tid}?mgroup.*=*&period.M=latest:1&hierarchy.{geo["hierarchy_id"]}={country_value(geo)}&hierarchy.{age["hierarchy_id"]}='+(f'level:{level}' if level else '*')+f'&hierarchy.{status["hierarchy_id"]}=*&format=json'
    rows=records(api.jobindsats_get(path));pc=best_col(rows,['periode']);agec=best_col(rows,['alder'],distinct=True);sc=best_col(rows,['arbejdsmarkedsstatus','status'],distinct=True);pctc=best_col(rows,['andel'],exclude=['grad']);period=max((str(r.get(pc)) for r in rows),key=pkey);items=[]
    for r in rows:
        if str(r.get(pc))!=period:continue
        s=norm(r.get(sc));label=str(r.get(agec) or '').strip();v=num(r.get(pctc));m=re.search(r'(?<!\d)(\d{2})(?!\d)',label)
        if not m or int(m.group(1))<55 or v is None:continue
        if 'loenmodtagerbeskaeftigelse i alt' not in s and s!='loenmodtagerbeskaeftigelse':continue
        if abs(v)<=1:v*=100
        items.append({'age':int(m.group(1)),'label':label,'employmentShare':round(v,2)})
    unique={x['age']:x for x in items};items=[unique[k] for k in sorted(unique)]
    if len(items)<8:raise RuntimeError(f'Kun {len(items)} et-årige aldersgrupper fundet')
    payload={'meta':{'source':'Jobindsats.dk / STAR','dataset':tid,'latestPeriod':period,'checkedAt':datetime.now(ZoneInfo('Europe/Copenhagen')).isoformat(timespec='seconds'),'measure':'Arbejdsmarkedsstatus for seniorer','unit':'pct.'},'period':period,'items':items}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print('Seniorfeed',tid,period,len(items))
if __name__=='__main__':main()
