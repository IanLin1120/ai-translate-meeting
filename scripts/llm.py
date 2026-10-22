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


def fetch_gguf(status=None):
    """下載開源語言模型（GGUF）到快取資料夾；已經有就直接用"""
    d = f"{MODELS}/llm"
    os.makedirs(d, exist_ok=True)
    for repo, fn in LOCAL_GGUF:
        p = f"{d}/{fn}"
        if not (os.path.exists(p) and os.path.getsize(p) > 1e8):
            if status:
                status("下載開源語言模型")
            print(f"::notice title=下載模型::{fn}（快取裡沒有）", flush=True)
            r = subprocess.run(["curl", "-fsSL", "-o", p, f"https://huggingface.co/{repo}/resolve/main/{fn}"])
            if r.returncode != 0 and os.path.exists(p):
                os.remove(p)
        if os.path.exists(p) and os.path.getsize(p) > 1e8:
            return p
    raise RuntimeError("無法下載開源語言模型")


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
        try:
            import llama_cpp  # noqa
        except ImportError:
            self.status("安裝開源語言模型執行環境")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "llama-cpp-python",
                            "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu"], check=True)
        from llama_cpp import Llama
        gguf = fetch_gguf(self.status)
        self.model = os.path.basename(gguf).replace(".gguf", "")
        kw = dict(model_path=gguf, n_ctx=12288, n_threads=os.cpu_count() or 4, n_batch=1024, verbose=False)
        try:
            self.llama = Llama(flash_attn=True, **kw)
        except TypeError:
            self.llama = Llama(**kw)

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

SUM_PART_SYS = """你是專業的會議記錄撰寫人。輸入是一段會議逐字稿（已經人工校對過），每行格式為「[時間] 講者：內容」。
請整理這一段的重點，只輸出 JSON：
{"topics":[{"title":"主題（12 字內）","start":"開始時間","end":"結束時間","points":["重點"]}],
 "decisions":["已決定的事項"],"actions":[{"owner":"負責人（不知道就寫 未指定）","task":"待辦事項","due":"期限"}]}
重點的寫法（很重要）：
1. 精簡：每個主題 2～4 點，每點 20～45 字；口語、重複、寒暄、離題的內容不要寫。
2. 用論述的方式寫出事情本身：「現況或問題 → 原因或影響 → 建議、做法或結論」，像正式會議記錄一樣通順有邏輯。
3. 不要逐句轉述，不要用「某某說」「某某表示」「某某提到」開頭；只有在需要指出誰負責、誰提出要求時才寫人名。
4. 保留關鍵的數字、文件編號、法規、日期與專有名詞，以逐字稿的用字為準。
5. due（期限）只有在逐字稿明確說出期限時才填，照原話寫（例如「下週五」「十一月底」）；不要自己推算或編造日期，也不要填逐字稿的時間戳記；沒有提到就填空字串。
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
    partial = summarize_parts(llm, segs, spk_name, (lambda p: progress(p * 0.8)) if progress else None, chunk_chars)
    return summarize_merge(llm, partial, (lambda p: progress(80 + p * 0.2)) if progress else None)


def summarize_parts(llm, segs, spk_name, progress=None, chunk_chars=3200):
    """分段整理（可以分給好幾台伺服器同時做）"""
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
            progress((i + 1) / len(parts) * 100)
    return partial


SUM_GROUP_SYS = """你是會議記錄整理助理。輸入是一場會議依時間順序列出的主題（每行：編號｜時間｜主題｜重點摘要）。
請把相關的主題合併成章節，只輸出 JSON：
{"title":"會議標題（15 字內）","overview":"整場會議摘要（3～5 句）","keywords":["關鍵字（5～10 個）"],
 "chapters":[{"title":"章節標題（12 字內）","summary":"本章摘要（1～2 句，說明問題與結論，不要用「某某說」開頭）","topics":[主題編號,...]}]}
每個主題編號都要放進剛好一個章節，章節依時間排序（通常 3～8 章）；使用台灣繁體中文；不要編造內容。"""

_DUE_BAD = re.compile(r"^\s*\d{1,2}[:：]\d{2}(\s*[-–~]\s*\d{1,2}[:：]\d{2})?\s*$|^\s*(未指定|無|沒有|n/?a|none|—|-)\s*$", re.I)


def clean_due(d):
    """期限：只留逐字稿真的提到的說法；時間戳記（14:51）或模型自己編的過去年份日期一律清掉"""
    d = str(d or "").strip()
    if not d or _DUE_BAD.match(d):
        return ""
    y = re.search(r"(20\d\d)", d)
    if y and int(y.group(1)) < time.localtime().tm_year:
        return ""
    return d


def _norm(s):
    return re.sub(r"[\s，。、,.!！?？：:；;「」『』（）()]", "", str(s or ""))


def _dedupe(items, key=lambda x: x):
    out, seen = [], []
    for x in items:
        k = _norm(key(x))
        if not k or any(k == s or (len(k) > 6 and (k in s or s in k)) for s in seen):
            continue
        seen.append(k)
        out.append(x)
    return out


def summarize_merge(llm, partial, progress=None):
    """把各段整理合併成一份分章節的重點整理（只讓模型做分組與摘要，重點內容直接沿用，速度快很多）"""
    partial = [p for p in partial if p]
    if not partial:
        return {}
    topics = []
    for p in partial:
        for t in p.get("topics", []) or []:
            if isinstance(t, dict) and (t.get("title") or t.get("points")):
                topics.append(t)
    decisions = _dedupe([d for p in partial for d in (p.get("decisions") or []) if isinstance(d, str)])
    actions = _dedupe([a for p in partial for a in (p.get("actions") or []) if isinstance(a, dict) and a.get("task")], key=lambda a: a.get("task"))
    for a in actions:
        a["due"] = clean_due(a.get("due"))
        a["owner"] = a.get("owner") or "未指定"
    lines = []
    for i, t in enumerate(topics):
        pts = "；".join(str(x) for x in (t.get("points") or [])[:2])[:90]
        lines.append(f"{i + 1}｜{t.get('start', '')}–{t.get('end', '')}｜{t.get('title', '')}｜{pts}")
    g = {}
    try:
        g = _json(llm.chat(SUM_GROUP_SYS, "\n".join(lines)[:9000], max_tokens=1200)) or {}
    except RuntimeError:
        raise
    except Exception:
        g = {}
    chapters, used = [], set()
    for c in g.get("chapters", []) or []:
        ids = []
        for x in c.get("topics", []) or []:
            try:
                x = int(x) - 1
            except Exception:
                continue
            if 0 <= x < len(topics) and x not in used:
                ids.append(x)
                used.add(x)
        if not ids:
            continue
        ts = [topics[i] for i in ids]
        pts = _dedupe([p for t in ts for p in (t.get("points") or []) if isinstance(p, str)])[:6]  # 每章最多 6 點，保持精簡
        chapters.append({"title": c.get("title") or ts[0].get("title", ""), "start": ts[0].get("start", ""), "end": ts[-1].get("end", ""),
                         "summary": c.get("summary", ""), "points": pts})
    # 模型漏掉的主題自己成一章
    for i, t in enumerate(topics):
        if i not in used:
            chapters.append({"title": t.get("title", ""), "start": t.get("start", ""), "end": t.get("end", ""), "summary": "", "points": t.get("points") or []})
    def tkey(c):
        try:
            return [int(x) for x in str(c.get("start") or "99:99:99").split(":")]
        except Exception:
            return [99]
    chapters.sort(key=lambda c: (len(tkey(c)), tkey(c)))
    if progress:
        progress(100)
    return {"title": g.get("title", ""), "overview": g.get("overview", ""), "chapters": chapters, "decisions": decisions,
            "actions": actions, "keywords": [k for k in (g.get("keywords") or []) if isinstance(k, str)][:12]}
