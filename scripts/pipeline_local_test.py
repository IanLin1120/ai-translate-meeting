"""在同一台機器上依序跑完 prep → asr → final（不經過多台伺服器，中間檔放本機），測試用"""
import json, os, sys, time
sys.argv = ["pipeline.py", "prep"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CUSTOM_ALLOW_TEST", "1")
import pipeline as PL, transcribe as T
ST = PL.TMP + "/store"
os.makedirs(ST, exist_ok=True)
T.status = lambda *a, **k: print("status:", a[0], flush=True)
PL.assets = lambda: {n: {"id": n} for n in os.listdir(ST)}


def put(name, data, ctype=None):
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False).encode()
    open(f"{ST}/{name}", "wb").write(data)


def get(name, binary=False):
    p = f"{ST}/{name}"
    return p if binary else json.load(open(p, encoding="utf-8"))


PL.put, PL.get, PL.clean_work = put, get, (lambda keep_result=True: None)
outs = {}
PL.out = lambda k, v: outs.__setitem__(k, v)
og = T.gh
T.gh = lambda m, u, *a, **k: ({"id": 1} if (m == "POST" and u == "/releases") else og(m, u, *a, **k))
PL.prep()
for k in json.loads(outs["asr"]):
    PL.asr_shard(k)
T.TEST = ""
PL.final()
r = json.load(open(f"{ST}/result.json"))
print(f"::notice title=我的模型轉文字::{len(r['segs'])} 句、{r['speakers']} 位講者、模型 {r['model']}")
assert r["segs"], "沒有轉出文字"
