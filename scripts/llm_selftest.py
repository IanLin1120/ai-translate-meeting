"""AI 整理引擎自我測試（只用合成的範例文字），結果寫成 Actions 註記"""
import json, os, sys, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
eng = os.environ.get("ENGINE", "github")
if eng == "github":
    for model in ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"]:
        body = {"model": model, "messages": [{"role": "user", "content": "用一句繁體中文打招呼"}], "max_tokens": 50}
        req = urllib.request.Request("https://models.github.ai/inference/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                print(f"::notice title={model} HTTP {r.status}::type={r.headers.get('content-type')} enc={r.headers.get('content-encoding')} body={raw[:300]!r}")
        except urllib.error.HTTPError as e:
            print(f"::notice title={model} HTTP {e.code}::{e.read()[:300]!r}")
import llm as L
segs = [{"s": 1, "e": 6, "spk": 1, "text": "大家好今天我們要討論新產品的上式時間跟預算"},
        {"s": 7, "e": 12, "spk": 2, "text": "我覺得上式時間可以訂在十二月初，但是budget要在確認一下"},
        {"s": 13, "e": 18, "spk": 1, "text": "好那我下週五之前把報價單給客戶，小王負責更新時成表"}]
name = lambda u: f"講者 {u['spk']}"
t = time.time()
try:
    ai = L.LLM(eng, lambda *a, **k: print("status", a, flush=True))
    fx = L.correct(ai, segs, name)
    print(f"::notice title={eng} 修正 ({ai.model}, {time.time()-t:.0f}s)::{json.dumps(fx, ensure_ascii=False)}")
    s = L.summarize(ai, segs, name)
    print(f"::notice title={eng} 重點 ({time.time()-t:.0f}s, {ai.calls} 次)::{json.dumps(s, ensure_ascii=False)[:900]}")
    ai.close()
except Exception as e:
    import traceback; traceback.print_exc()
    print(f"::error title={eng} 失敗::{type(e).__name__}: {str(e)[:300]}")
