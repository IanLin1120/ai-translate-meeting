"""AI 整理引擎自我測試（只用合成的範例文字），結果寫成 Actions 註記"""
import json, os, sys, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
eng = os.environ.get("ENGINE", "github")
if eng == "github":
    import subprocess
    tok = os.environ["GITHUB_TOKEN"]
    body = json.dumps({"model": "openai/gpt-4.1-mini", "messages": [{"role": "user", "content": "用一句繁體中文打招呼"}], "max_tokens": 50})
    variants = [
        ("doc headers", "https://models.github.ai/inference/chat/completions", ["-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]),
        ("plain json", "https://models.github.ai/inference/chat/completions", ["-H", "Accept: application/json"]),
        ("azure endpoint", "https://models.inference.ai.azure.com/chat/completions", ["-H", "Accept: application/json"]),
    ]
    for name, url, h in variants:
        b = body if "azure" not in url else body.replace("openai/gpt-4.1-mini", "gpt-4.1-mini")
        r = subprocess.run(["curl", "-sS", "-m", "60", "-w", "\\nHTTP %{http_code} %{content_type}", "-X", "POST", url, "-H", f"Authorization: Bearer {tok}",
                            "-H", "Content-Type: application/json", *h, "-d", b], capture_output=True, text=True)
        print(f"::notice title=curl {name}::{(r.stdout or r.stderr)[-350:]}")
    print("proxy env:", {k: v for k, v in os.environ.items() if 'proxy' in k.lower()}, flush=True)
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
