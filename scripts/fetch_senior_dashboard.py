"""Official aggregate data for the senior dashboard. Credentials stay in Actions."""
import json, math, os, re, time, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
API = 'https://api.jobindsats.dk/v3/'
STATUSES = ['/', '/2/', '/2/5/', '/2/6/', '/2/7/', '/2/8/', '/2/9/', '/2/10/', '/9/', '/9/11/', '/9/12/', '/9/13/', '/9/14/', '/14/', '/14/15/', '/14/16/']

def get(path):
    req = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + os.environ['JOBINDSATS_API_TOKEN'], 'User-Agent': 'Senior-dashboard/1.0'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=150) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError('Jobindsats HTTP ' + str(exc.code) + ': ' + exc.read().decode()[:300]) from None
            time.sleep(45 * (attempt + 1))

def num(v):
    if v is None or str(v) in ('', '.', '..', '-'): return None
    if isinstance(v, str): v = v.replace('.', '').replace(',', '.') if ',' in v else v
    v = float(v)
    return round(v, 3) if math.isfinite(v) else None

def normal(s):
    return re.sub('[^a-z0-9]', '', s.lower().replace('æ', 'ae').replace('ø', 'oe').replace('å', 'aa'))

def main():
    spec = get('table/snr_arbstatus?format=json')
    hs = {h['hierarchy_id']: h for d in spec['dimensions'] for h in d['hierarchies']}
    values = lambda h: [v for level in hs[h]['levels'] for v in level['values']]
    geos = [g for g in values('_nykom') if g['value_name'] != 'Uoplyst område']
    geos.sort(key=lambda g: (g['value_id'] != '/', g['value_name']))
    periods = sorted(v['period_id'] for p in spec['periods'] if p['periodtype_id'] == 'M' for v in p['values'])
    ages = {v['value_name']: int(re.search(r'\d+', v['value_name'])[0]) for v in values('_alder_snr') if re.search(r'\d+', v['value_name'])}
    status_names = {v['value_name']: STATUSES.index(v['value_id']) for v in values('_snr_arbstatus')}
    out = {'meta': {'dataset': 'snr_arbstatus', 'source': 'Jobindsats.dk / STAR', 'checkedAt': datetime.now(ZoneInfo('Europe/Copenhagen')).isoformat(timespec='seconds'), 'latestPeriod': periods[-1], 'schema': 1}, 'periods': periods, 'ages': list(range(55,76)), 'statuses': [{'id': sid, 'label': next(n for n,i in status_names.items() if i == k)} for k,sid in enumerate(STATUSES)], 'areas': [], 'data': {}, 'map': {}}
    pidx = {p:i for i,p in enumerate(periods)}
    for geo in geos:
        gid = geo['value_id']
        key = 'dk' if gid == '/' else gid.strip('/')
        url = 'data/snr_arbstatus?mgroup.*=*&period.M=latest:' + str(len(periods)) + '&hierarchy._nykom=' + gid + '&hierarchy._alder_snr=level:alder_snr&hierarchy._snr_arbstatus=*&format=json'
        payload = get(url)
        columns = payload['columns']
        print(geo['value_name'], len(payload['rows']), columns, flush=True)
        rows = [dict(zip(columns, r)) for r in payload['rows']]
        pc = next(c for c in columns if c == 'Periode')
        ac = next(c for c in columns if c == 'Alder')
        sc = next(c for c in columns if c == 'Arbejdsmarkedsstatus')
        nc = next(c for c in columns if 'Antal personer' in c)
        dc = next(c for c in columns if 'beskæftigelsesgrad' in c.lower())
        # Determine the degree scale once for the full national series, never per cell.
        if key == 'dk':
            max_degree = max(num(r[dc]) or 0 for r in rows)
            degree_factor = 100 if max_degree <= 1.01 else 1
            out['meta']['degreeFactor'] = degree_factor
            (ROOT/'data/senior-dashboard-sample.json').write_text(json.dumps({'columns':columns,'rows':payload['rows'][:25]},ensure_ascii=False),encoding='utf-8')
        cube = [[[None] * 17 for a in range(21)] for p in periods]
        for r in rows:
            if r[ac] not in ages: continue
            ai = ages[r[ac]] - 55
            if not 0 <= ai < 21: continue
            si = status_names[r[sc]]
            cube[pidx[r[pc]]][ai][si] = num(r[nc])
            if si == 1:
                degree = num(r[dc])
                cube[pidx[r[pc]]][ai][16] = round(degree * degree_factor, 3) if degree is not None else None
        if any(cube[-1][a][0] is None for a in range(21)):
            raise RuntimeError('Befolkningsgrundlaget mangler: ' + geo['value_name'])
        out['areas'].append({'id': key, 'name': geo['value_name'], 'key': normal(geo['value_name'])})
        out['data'][key] = cube
        time.sleep(1)
    assert len(out['areas']) == 99, 'Forventer landet og 98 kommuner'
    # Validate the non-overlapping main categories where all counts are published.
    for key,cube in out['data'].items():
        for month in cube:
            for row in month:
                if all(row[i] is not None for i in (0,1,8,13)):
                    assert abs(row[0]-row[1]-row[8]-row[13]) < 2, (key, row)
    for key,cube in out.pop('data').items():
        (ROOT/('data/senior-dashboard-area-' + key + '.json')).write_text(json.dumps(cube,separators=(',',':'),allow_nan=False),encoding='utf-8')
        for pi,month in enumerate(cube):
            out['map'].setdefault(periods[pi],{})[key] = [[r[0],r[1],r[5],r[16]] for r in month]
    for p,snapshot in out.pop('map').items():
        (ROOT/('data/senior-dashboard-map-' + p + '.json')).write_text(json.dumps(snapshot,separators=(',',':'),allow_nan=False),encoding='utf-8')
    OUT = ROOT/'data/senior-dashboard.json'
    OUT.write_text(json.dumps(out,ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf-8')
    print('Validated', len(periods), 'months, 99 areas; bytes', OUT.stat().st_size)

if __name__ == '__main__': main()
