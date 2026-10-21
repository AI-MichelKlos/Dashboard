import json, os, urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
req=urllib.request.Request("https://api.jobindsats.dk/v3/table/snr_arbstatus?format=json",headers={"Authorization":"Bearer "+os.environ["JOBINDSATS_API_TOKEN"],"User-Agent":"Senior-dashboard/1.0"})
with urllib.request.urlopen(req,timeout=90) as r: spec=json.load(r)
(ROOT/"data/senior-dashboard-spec.json").write_text(json.dumps(spec,ensure_ascii=False),encoding="utf-8")
print("Metadata hentet")
