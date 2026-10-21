"""會議逐字稿的 AI 整理：修正錯字、產生分章節的重點整理

兩種引擎（都用 OpenAI 相容的 chat completions 介面）：
- github：GitHub Models（已於 2026-07-30 停止服務，現在自動改用 local）
- local ：在 GitHub Actions 伺服器上用 llama-cpp-python 跑開源 Qwen3（文字不會送到其他服務）
隱私：這個儲存庫是公開的，Actions 紀錄任何人都看得到，這裡不印出任何逐字稿內容。
"""
import json, os, re, subprocess, sys, time, urllib.request, urllib.error

MODELS = os.path.expanduser("~/.cache/aitm")
GH_URL = "https://models.github.ai/inference/chat/completions"
GH_MODELS = ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"]
LOCAL_GGUF = [  # 依序嘗試（Hugging Face 上的開源 GGUF）
    ("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
    ("Qwen/Qwen2.5-3B-Instruct-GGUF", "qwen2.5-3b-instruct-q4_k_m.gguf"),
]


class LLM:
    def __init__(self, engine, status=None):
        if engine == "github":
            engine = "local"  # GitHub Models 已於 2026-07-30 停止服務
        self.engine = engine
        self.status = status or (lambda *a, **k: None)
        self.model = None
        self.calls = 0
        if engine == "local":
            self._start_local()

    # ---------- 開源模型：llama.cpp server ----------
    def _start_local(self):
        d = f"{MODELS}/llm"
        os.makedirs(d, exist_ok=True)
        try:
            import llama_cpp  # noqa
        except ImportError:
            self.status("安裝開源語言模型執行環境")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "llama-cpp-python",
                            "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu"], check=True)
        from llama_cpp import Llama
        gguf = None
        for repo, fn in LOCAL_GGUF:
            p = f"{d}/{fn}"
            if not (os.path.exists(p) and os.path.getsize(p) > 1e8):
                self.status("下載開源語言模型")
                r = subprocess.run(["curl", "-fsSL", "-o", p, f"https://huggingface.co/{repo}/resolve/main/{fn}"])
                if r.returncode != 0 and os.path.exists(p):
                    os.remove(p)
            if os.path.exists(p) and os.path.getsize(p) > 1e8:
                gguf = p
                break
        if not gguf:
            raise RuntimeError("無法下載開源語言模型")
        self.model = os.path.basename(gguf).replace(".gguf", "")
        self.llama = Llama(model_path=gguf, n_ctx=16384, n_threads=os.cpu_count() or 4, verbose=False)

    def close(self):
        self.llama = None

    # ---------- 呼叫 ----------
    def chat(self, system, user, max_tokens=3000, temperature=0.2):
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": temperature, "max_tokens": max_tokens}
        if self.engine == "github":
            last = None
            for m in ([self.model] if self.model else GH_MODELS):
                body["model"] = m
                for i in range(6):
                    req = urllib.request.Request(GH_URL, data=json.dumps(body).encode(), method="POST", headers={
                        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Content-Type": "application/json",
                        "Accept": "application/json"})
                    try:
                        raw = urllib.request.urlopen(req, timeout=180).read()
                        try:
                            r = json.loads(raw)
                        except Exception:
                            last = f"回應格式錯誤：{raw[:80]!r}"
                            time.sleep(5 * (i + 1))
                            continue
                        self.model = m
                        self.calls += 1
                        return r["choices"][0]["message"]["content"]
                    except urllib.error.HTTPError as e:
                        last = f"HTTP {e.code}"
                        if e.code == 429:
                            wait = int(e.headers.get("Retry-After") or 20 * (i + 1))
                            if wait > 600:
                                raise RuntimeError("GitHub Models 今天的免費額度用完了，請明天再試，或改用開源模型")
                            time.sleep(min(wait, 120))
                            continue
                        if e.code in (400, 404, 403, 401):
                            break  # 換下一個模型
                        time.sleep(5 * (i + 1))
                    except Exception as e:
                        last = str(e)
                        time.sleep(5 * (i + 1))
            raise RuntimeError(f"GitHub Models 無法使用（{last}）")
        r = self.llama.create_chat_completion(messages=body["messages"], temperature=temperature, max_tokens=max_tokens)
        self.calls += 1
        return re.sub(r"<think>[\s\S]*?</think>", "", r["choices"][0]["message"]["content"]).strip()


def _json(txt):
    """從模型回覆中取出 JSON（容忍 ```json 區塊與前後文字）"""
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    for a, b in (("{", "}"), ("[", "]")):
        i, j = txt.find(a), txt.rfind(b)
        if i >= 0 and j > i:
            try:
                return json.loads(txt[i:j + 1])
            except Exception:
                pass
    return None


def fmt_t(s):
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


FIX_SYS = """你是會議逐字稿校對員。輸入是語音辨識（ASR）產生的逐字稿，每行格式為「編號|講者|內容」。
請根據上下文修正辨識錯誤：同音或近音錯字、斷錯的詞、專有名詞與英文術語的拼寫、明顯漏掉或多餘的字、標點。
規則：
1. 只修正錯誤，保留原本的語意、語氣與口語用詞，不要改寫、不要摘要、不要刪掉內容。
2. 使用台灣繁體中文與台灣慣用詞；英文單字保持英文（必要時修正大小寫，例如 ok → OK、iphone → iPhone）。
3. 不確定的地方維持原樣。
4. 只輸出 JSON 物件：{"編號": "修正後內容", ...}，只列出有修改的行；沒有修改就輸出 {}。"""

SUM_PART_SYS = """你是會議記錄整理助理。輸入是一段會議逐字稿，每行格式為「[時間] 講者：內容」。
請整理這一段的重點，只輸出 JSON：
{"topics":[{"title":"主題（10 字內）","start":"開始時間","end":"結束時間","points":["重點（完整一句話，含誰說了什麼、數字、日期）"]}],
 "decisions":["已決定的事項"],"actions":[{"owner":"負責人（不知道就寫 未指定）","task":"待辦事項","due":"期限（沒有就空字串）"}]}
使用台灣繁體中文；講者用逐字稿上的名稱；不要編造逐字稿沒有的內容。"""

SUM_FINAL_SYS = """你是會議記錄整理助理。輸入是同一場會議依時間順序的分段整理（JSON）。
請合併成一份完整的會議重點整理，相同主題要合併，依時間排序成章節，只輸出 JSON：
{"title":"會議標題（15 字內）","overview":"整場會議摘要（3～5 句）",
 "chapters":[{"title":"章節標題","start":"開始時間","end":"結束時間","summary":"本章摘要（1～2 句）","points":["重點"]}],
 "decisions":["決議事項"],"actions":[{"owner":"負責人","task":"待辦事項","due":"期限"}],"keywords":["關鍵字"]}
章節數依內容而定（通常 3～8 章）；使用台灣繁體中文；不要編造內容。"""


def correct(llm, segs, spk_name, progress=None, chunk_chars=1100):
    """逐批校對；回傳 {索引: 修正後文字}"""
    out = {}
    batches, cur, n = [], [], 0
    for i, u in enumerate(segs):
        line = f"{i}|{spk_name(u)}|{u['text']}"
        if cur and n + len(line) > chunk_chars:
            batches.append(cur)
            cur, n = [], 0
        cur.append((i, line))
        n += len(line)
    if cur:
        batches.append(cur)
    for bi, b in enumerate(batches):
        # 前後各帶兩行做上下文（不要求修正）
        ids = [i for i, _ in b]
        ctx_before = [f"(上文) {spk_name(segs[j])}：{segs[j]['text']}" for j in range(max(0, ids[0] - 2), ids[0])]
        user = ("\n".join(ctx_before) + "\n" if ctx_before else "") + "\n".join(l for _, l in b)
        try:
            r = _json(llm.chat(FIX_SYS, user, max_tokens=min(4000, int(len(user) * 1.6) + 200), temperature=0.1)) or {}
            for k, v in r.items():
                try:
                    k = int(k)
                except Exception:
                    continue
                if k in ids and isinstance(v, str) and v.strip():
                    o = segs[k]["text"]
                    # 防呆：改動過大（可能是模型改寫或摘要）就不採用
                    if 0.6 <= len(v) / max(1, len(o)) <= 1.6:
                        out[k] = v.strip()
        except RuntimeError:
            raise
        except Exception as e:
            print("correct batch failed", type(e).__name__, flush=True)
        if progress:
            progress((bi + 1) / len(batches) * 100)
    return out


def summarize(llm, segs, spk_name, progress=None, chunk_chars=3200):
    lines = [f"[{fmt_t(u['s'])}] {spk_name(u)}：{u['text']}" for u in segs]
    parts, cur, n = [], [], 0
    for l in lines:
        if cur and n + len(l) > chunk_chars:
            parts.append(cur)
            cur, n = [], 0
        cur.append(l)
        n += len(l)
    if cur:
        parts.append(cur)
    partial = []
    for i, p in enumerate(parts):
        r = _json(llm.chat(SUM_PART_SYS, "\n".join(p), max_tokens=2500)) or {}
        partial.append(r)
        if progress:
            progress((i + 1) / (len(parts) + 1) * 100)
    # 分段整理太多時，先兩兩合併，避免超過單次輸入上限
    while len(json.dumps(partial, ensure_ascii=False)) > 9000 and len(partial) > 1:
        merged = []
        for i in range(0, len(partial), 2):
            grp = partial[i:i + 2]
            if len(grp) == 1:
                merged.append(grp[0])
                continue
            r = _json(llm.chat(SUM_PART_SYS.replace("一段會議逐字稿，每行格式為「[時間] 講者：內容」", "同一場會議相鄰兩段的整理（JSON）"),
                               json.dumps(grp, ensure_ascii=False), max_tokens=2500)) or {}
            merged.append(r)
        partial = merged
    final = _json(llm.chat(SUM_FINAL_SYS, json.dumps(partial, ensure_ascii=False), max_tokens=3500)) or {}
    if progress:
        progress(100)
    if not final.get("chapters"):
        # 合併失敗 → 直接用分段結果
        ch = []
        for p in partial:
            for t in p.get("topics", []):
                ch.append({"title": t.get("title", ""), "start": t.get("start", ""), "end": t.get("end", ""), "summary": "", "points": t.get("points", [])})
        final = {"title": "", "overview": "", "chapters": ch,
                 "decisions": sum([p.get("decisions", []) for p in partial], []),
                 "actions": sum([p.get("actions", []) for p in partial], []), "keywords": []}
    return final
