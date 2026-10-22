#!/usr/bin/env python3
"""GitHub 雲端會議轉文字：多台伺服器同時處理（平行化版本）

  prep  （1 台）：讀取工作、下載錄音後立刻從 GitHub 刪除、分辨講者、把工作切成幾份
  asr   （N 台）：每台把一部分發言轉文字
  llm   （M 台）：每台用開源 Qwen3 修正一部分錯字，並整理那一部分的重點
  final （1 台）：合併結果、彙整成分章節的重點整理、上傳 result.json、刪除所有中間檔

中間檔（w-*）都放在同一個「草稿 Release」（只有儲存庫擁有者看得到），處理完或失敗時一律刪除。
隱私：這個儲存庫是公開的，Actions 紀錄任何人都看得到，所以這裡「絕不」印出逐字稿內容。
"""
import json, math, os, re, subprocess, sys, tempfile, time, traceback

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcribe as T  # noqa: E402

SR = T.SR
MODELS = T.MODELS
PHASE = sys.argv[1] if len(sys.argv) > 1 else "prep"
SHARD = int(sys.argv[2]) if len(sys.argv) > 2 else 0
TMP = tempfile.mkdtemp()
OUT = os.environ.get("GITHUB_OUTPUT")


def out(k, v):
    if OUT:
        with open(OUT, "a") as f:
            f.write(f"{k}={v}\n")


# ---------------- 中間檔（草稿 Release 的附件） ----------------
def assets():
    return {a["name"]: a for a in T.gh("GET", f"/releases/{T.REL}/assets?per_page=100")}


def put(name, data, ctype="application/json"):
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    a = assets().get(name)
    if a:
        T.gh("DELETE", f"/releases/assets/{a['id']}")
    rel = T.gh("GET", f"/releases/{T.REL}")
    up = rel["upload_url"].split("{")[0]
    for i in range(4):
        try:
            return T.gh("POST", f"{up}?name={name}", data, ctype=ctype)
        except Exception:
            if i == 3:
                raise
            time.sleep(5 * (i + 1))


def get(name, binary=False):
    a = assets().get(name)
    if not a:
        raise RuntimeError(f"找不到中間檔 {name}")
    p = os.path.join(TMP, name)
    T.download_asset(a, p)
    if binary:
        return p
    return json.load(open(p, encoding="utf-8"))


def clean_work(keep_result=True):
    for n, a in assets().items():
        if keep_result and n == "result.json":
            continue
        try:
            T.gh("DELETE", f"/releases/assets/{a['id']}")
        except Exception:
            pass


def split_by_weight(weights, k):
    """把連續的項目切成 k 份，每份的總量差不多；回傳 [(i0, i1), ...]"""
    n = len(weights)
    k = max(1, min(k, n))
    tot = sum(weights) or 1
    cuts, acc, j = [0], 0.0, 1
    for i, w in enumerate(weights):
        acc += w
        if j < k and acc >= tot * j / k and i + 1 < n:
            cuts.append(i + 1)
            j += 1
    cuts.append(n)
    return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1) if cuts[i + 1] > cuts[i]]


def lexists(p):
    return os.path.exists(p) and os.path.getsize(p) > 0


# ---------------- 1. 準備 ----------------
def prep():
    t0 = time.time()
    rep_n = 1
    if T.TEST and "*" in T.TEST:  # 測試用:把同一段錄音重複 N 次，模擬長會議
        T.TEST, rep_n = T.TEST.split("*")[0], int(T.TEST.split("*")[1])
    if T.TEST:
        rel = T.gh("POST", "/releases", {"tag_name": f"test-{os.environ.get('GITHUB_RUN_ID', int(t0))}", "name": "test", "draft": True, "body": "{}"})
        T.REL = str(rel["id"])
    out("rel", T.REL)
    job, files = T.load_job()
    job["t_start"] = t0
    plan = {"job": job, "t_start": t0}
    # 模型先準備好（之後由這台存進快取，其他伺服器直接還原）
    model = job.get("model") or "firered"
    lang = job.get("lang") or "auto"
    if model == "firered" and lang in ("ja", "ko", "yue"):
        model = "sensevoice"
    if model in ("firered", "sensevoice") and lang not in ("auto", "zh", "en", "ja", "ko", "yue"):
        model = "large-v3-turbo"
    plan["model"] = model
    want_llm = (job.get("llm") or "") in ("github", "local") and (job.get("fix") or job.get("summ"))
    if job.get("mode") == "text":
        plan["segs"] = job.get("segs", [])
        plan["dur"] = job.get("dur", 0)
        asr_sh = []
    else:
        T.status("解碼音訊")
        parts = []
        for p in job["parts"]:
            a = T.decode(files[p["name"]])
            parts.append((float(p.get("t0", 0)), a))
            if not T.TEST:
                os.remove(files[p["name"]])
        total = max([int(t * SR) + len(a) for t, a in parts] + [0])
        audio = np.zeros(total, dtype=np.float32)
        for t, a in parts:
            s = int(t * SR)
            audio[s:s + len(a)] = a[: total - s]
        if rep_n > 1:
            audio = np.tile(audio, rep_n)
            total = len(audio)
        dur = total / SR
        plan["dur"] = dur
        print(f"音訊長度 {dur / 60:.1f} 分鐘", flush=True)
        # 依錄音長度切成幾段（切在最安靜的地方），每台伺服器各自分辨講者片段＋轉文字，最後再一起分群
        n = max(1, min(10, math.ceil(dur / float(os.environ.get("SHARD_SEC") or 360)))) if dur >= 0.5 else 0
        fr = int(SR * 0.1)
        rms = np.sqrt(np.convolve(np.square(audio[: len(audio) // fr * fr].reshape(-1, fr)).mean(1), np.ones(5) / 5, "same")) if len(audio) >= fr * 5 else np.zeros(1)
        cuts = [0.0]
        for j in range(1, n):
            c = dur * j / n
            a0, a1 = int(max(0, c - 15) * 10), int(min(dur, c + 15) * 10)
            cuts.append((a0 + int(np.argmin(rms[a0:a1]))) / 10 if a1 > a0 else c)
        cuts.append(dur)
        asr_sh = [[round(cuts[j], 2), round(cuts[j + 1], 2)] for j in range(n) if cuts[j + 1] - cuts[j] > 0.3]
        plan["turns"] = None
        print(f"音訊切成 {len(asr_sh)} 段平行處理", flush=True)
        if asr_sh:
            T.status("上傳給轉文字伺服器")
            fl = os.path.join(TMP, "audio.flac")
            import imageio_ffmpeg
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                            "-c:a", "flac", "-sample_fmt", "s16", fl], input=audio.tobytes(), check=True)
            put("w-audio.flac", open(fl, "rb").read(), "audio/flac")
            del audio
    plan["asr"] = asr_sh
    # AI 整理的伺服器數：大約每 5 分鐘會議一台（最多 10 台）
    m = max(1, min(10, math.ceil(plan["dur"] / 300))) if want_llm else 0
    plan["llm"] = m
    # 確認模型都在快取裡
    if asr_sh:
        if model == "firered":
            T.fetch_model("firered", "asr-models/sherpa-onnx-fire-red-asr-large-zh_en-2025-02-16.tar.bz2", ["encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"])
            T.fetch_model("punct", "punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12.tar.bz2", ["model.onnx"])
    if m:
        import llm as L
        L.fetch_gguf(T.status)
    put("w-plan.json", plan)
    out("asr", json.dumps(list(range(len(asr_sh)))))
    out("llm", json.dumps(list(range(m))))
    n_srv = len(asr_sh)
    T.status(f"分辨講者與轉文字（{n_srv} 台伺服器同時處理）" if n_srv else "AI 整理", 0)
    print(f"::notice title=分工::轉文字 {len(asr_sh)} 台、AI 整理 {m} 台；準備花了 {time.time() - t0:.0f} 秒", flush=True)


# ---------------- 2. 轉文字（每台一部分） ----------------
def asr_shard(k):
    t0 = time.time()
    plan = get("w-plan.json")
    ta, tb = plan["asr"][k]
    full = T.decode(get("w-audio.flac", binary=True))
    audio = full[int(ta * SR):int(tb * SR)].copy()
    del full
    dur = len(audio) / SR
    model, lang = plan["model"], plan["job"].get("lang") or "auto"
    rep = k == 0
    last = [0.0]
    n_srv = len(plan["asr"])
    st_name = f"分辨講者與轉文字（{n_srv} 台伺服器同時處理）"

    def say(p):
        if rep and time.time() - last[0] > 20:
            last[0] = time.time()
            T.status(st_name, min(99, p))
    # 1. 講者片段＋聲紋
    import diarize as DZ
    pieces, embs = DZ.segments_and_embeddings(audio, MODELS, lambda p: say(p * 0.25), threads=os.cpu_count() or 4)
    raw = getattr(DZ.segments_and_embeddings, "raw", [])
    rsp = getattr(DZ.segments_and_embeddings, "raw_spk", [0] * len(raw))
    # 同一個人連續說的話合成一段（最長 25 秒）再轉文字：上下文比較完整，標點與用字比較準
    grp = []
    for (s, e), sp in zip(raw, rsp):
        if grp and grp[-1][2] == sp and s - grp[-1][1] < 0.8 and e - grp[-1][0] <= 25:
            grp[-1][1] = max(grp[-1][1], e)
        else:
            grp.append([s, e, sp])
    units = []
    for s, e, _ in grp:
        n = max(1, math.ceil((e - s) / 25))
        w = (e - s) / n
        units += [(s + i * w, s + (i + 1) * w) for i in range(n)]
    pc = []
    for (s, e), x in zip(pieces, embs):
        m = (s + e) / 2
        ui = next((i for i, (a, b) in enumerate(units) if a - 0.05 <= m <= b + 0.05), None)
        pc.append({"s": round(s + ta, 3), "e": round(e + ta, 3), "u": ui, "emb": None if x is None else [round(float(v), 4) for v in x]})

    def clip(u):
        a = audio[int(max(0, u[0] - 0.15) * SR):int(min(dur, max(u[1], u[0]) + 0.15) * SR)]
        if len(a) < SR // 10 or not np.isfinite(a).all():
            return None
        if len(a) < SR // 2:
            a = np.concatenate([a, np.zeros(SR // 2 - len(a), dtype=np.float32)])
        return a
    texts, bad = [""] * len(units), 0
    prog = lambda i: say(25 + 75 * (i + 1) / max(1, len(units)))
    if model == "firered":
        import sherpa_onnx
        d = f"{MODELS}/firered"
        pd = f"{MODELS}/punct"
        rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr(encoder=f"{d}/encoder.int8.onnx", decoder=f"{d}/decoder.int8.onnx",
                                                              tokens=f"{d}/tokens.txt", num_threads=os.cpu_count() or 4)
        pcm = sherpa_onnx.OfflinePunctuation(sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(ct_transformer=f"{pd}/model.onnx", num_threads=1)))
        for i, u in enumerate(units):
            a = clip(u)
            if a is not None:
                try:
                    st = rec.create_stream()
                    st.accept_waveform(SR, a)
                    rec.decode_stream(st)
                    txt = re.sub(r"[A-Z][A-Z']+", lambda m: m.group(0) if len(m.group(0)) <= 3 else m.group(0).lower(), st.result.text.strip())
                    texts[i] = pcm.add_punctuation(txt) if txt else ""
                except Exception:
                    bad += 1
            prog(i)
    elif model == "sensevoice":
        import sherpa_onnx
        d = f"{MODELS}/sensevoice"
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(model=f"{d}/model.int8.onnx", tokens=f"{d}/tokens.txt", num_threads=os.cpu_count() or 4,
                                                             use_itn=True, language=lang if lang in ("zh", "en", "ja", "ko", "yue") else "auto")
        for i, u in enumerate(units):
            a = clip(u)
            if a is not None:
                try:
                    st = rec.create_stream()
                    st.accept_waveform(SR, a)
                    rec.decode_stream(st)
                    texts[i] = st.result.text
                except Exception:
                    bad += 1
            prog(i)
    else:
        from faster_whisper import WhisperModel
        wm = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4, download_root=f"{MODELS}/whisper")
        wl = None if lang in ("auto", "") else lang
        prompt = "以下是會議錄音，內容是繁體中文和 English 夾雜。" if wl in (None, "zh") else None
        for i, u in enumerate(units):
            a = clip(u)
            if a is not None:
                try:
                    segs, info = wm.transcribe(a, language=wl, beam_size=5, vad_filter=False, condition_on_previous_text=False,
                                               without_timestamps=True, initial_prompt=prompt)
                    texts[i] = "".join(s.text for s in segs if not (s.no_speech_prob > 0.8 and s.avg_logprob < -1))
                except Exception:
                    bad += 1
            prog(i)
    put(f"w-asr-{k}.json", {"units": [[round(a + ta, 3), round(b + ta, 3)] for a, b in units], "texts": texts, "pieces": pc, "bad": bad})
    print(f"::notice title=轉文字 {k + 1}/{n_srv}::{(tb - ta) / 60:.1f} 分鐘錄音、{len(units)} 段發言，花了 {time.time() - t0:.0f} 秒" + (f"，略過 {bad} 段" if bad else ""), flush=True)


def gather_turns(plan):
    """合併各台結果 → 全部聲紋一起分群（每台算出來都一樣）→ 相鄰同一人的發言合併"""
    import diarize as DZ
    units, pieces = [], []
    bad = 0
    for k in range(len(plan["asr"])):
        r = get(f"w-asr-{k}.json")
        bad += r.get("bad", 0)
        base = len(units)
        for (s, e), t in zip(r["units"], r["texts"]):
            units.append({"s": s, "e": e, "text": t, "P": []})
        for p in r["pieces"]:
            p = dict(p)
            p["u"] = None if p["u"] is None else base + p["u"]
            pieces.append(p)
    if not pieces:
        return [], bad
    embs = [None if p["emb"] is None else np.array(p["emb"], dtype=np.float32) for p in pieces]
    labs = DZ.label([(p["s"], p["e"]) for p in pieces], embs, int(plan["job"].get("speakers") or 0), float(plan["job"].get("sens") or 0.65))
    for p, l in zip(pieces, labs):
        if p["u"] is not None:
            units[p["u"]]["P"].append((p["e"] - p["s"], int(l), p["emb"]))
    turns = []
    for u in units:
        if not u["P"]:
            continue
        w = {}
        for d, l, _ in u["P"]:
            w[l] = w.get(l, 0) + d
        spk = max(w, key=w.get)
        E = [x for _, l, x in u["P"] if x is not None and l == spk]
        t = (u["text"] or "").strip()
        if turns and turns[-1]["spk"] == spk and u["s"] - turns[-1]["e"] < 0.8 and u["e"] - turns[-1]["s"] < 40:
            a = turns[-1]
            sep = " " if a["text"][-1:].isascii() and a["text"][-1:].isalnum() and t[:1].isascii() and t[:1].isalnum() else ""
            a["text"] = (a["text"] + sep + t) if a["text"] else t
            a["e"] = u["e"]
            a["E"] += E
        else:
            turns.append({"s": u["s"], "e": u["e"], "spk": spk, "text": t, "E": E})
    for u in turns:
        u["emb"] = [round(float(v), 4) for v in np.mean(np.array(u["E"], dtype=np.float32), 0)] if u["E"] else None
        del u["E"]
    return turns, bad


def build_segs(plan):
    """合併各台的轉文字結果 → 整理（繁體、去幻聽字幕、講者重新編號）"""
    if plan.get("segs") is not None and not plan.get("asr"):
        return [dict(u) for u in plan["segs"]], 0
    if "_turns" not in plan:
        plan["_turns"] = gather_turns(plan)
    turns, bad = [dict(u) for u in plan["_turns"][0]], plan["_turns"][1]
    cc = None
    if plan["job"].get("tw", 1):
        import opencc
        cc = opencc.OpenCC("s2twp")
    res = []
    for u in turns:
        t = re.sub(r"\s+", " ", u.get("text") or "").strip()
        t = re.sub(r"(?<=[一-鿿，。！？、]) (?=[一-鿿])", "", t)
        t = re.sub(r"(.{2,20}?)(?:\s*\1){3,}", r"\1\1", t)
        if not t or re.fullmatch(r"[\s.。,，!！?？…\-]*", t) or (T.HALL.search(t) and len(t) < 40):
            continue
        item = {"s": round(u["s"], 2), "e": round(u["e"], 2), "spk": int(u["spk"]), "text": T.to_tw(t, cc)}
        if u.get("emb"):
            item["emb"] = u["emb"]
        res.append(item)
    ids = list(dict.fromkeys(u["spk"] for u in res))  # 依第一次發言的順序編號（和 App 的「講者 N」一致）
    remap = {k: i + 1 for i, k in enumerate(ids)}
    for u in res:
        u["spk"] = remap[u["spk"]]
    return res, bad


def spk_namer(plan):
    names = {int(k): v for k, v in (plan["job"].get("names") or {}).items()}
    return lambda u: names.get(int(u.get("spk", 0)), f"講者 {u.get('spk', 0)}")


# ---------------- 3. AI 修正錯字＋分段重點（每台一部分） ----------------
def llm_shard(k):
    t0 = time.time()
    plan = get("w-plan.json")
    segs, _ = build_segs(plan)
    m = plan["llm"]
    blocks = split_by_weight([len(u["text"]) + 12 for u in segs], m)
    outp = {"fixed": {}, "partial": [], "err": None}
    if k < len(blocks):
        i0, i1 = blocks[k]
        part = segs[i0:i1]
        import llm as L
        rep = k == 0
        last = [0.0]
        job = plan["job"]
        steps = (1 if job.get("fix") else 0) + (1 if job.get("summ") else 0)

        def prog(stage_i, p):
            if rep and time.time() - last[0] > 20:
                last[0] = time.time()
                T.status(f"AI 修正錯字・整理重點（{m} 台伺服器同時處理）", min(99, (stage_i + p / 100) / max(1, steps) * 100))
        ai = None
        try:
            ai = L.LLM("local", T.status if rep else None)
            si = 0
            if job.get("fix"):
                fx = L.correct(ai, part, spk_namer(plan), lambda p: prog(si, p), chunk_chars=1800)
                outp["fixed"] = {str(i0 + i): v for i, v in fx.items()}
                si += 1
            if job.get("summ"):
                # 用修正後的文字做重點整理
                fixed_part = [dict(u, text=outp["fixed"].get(str(i0 + i), u["text"])) for i, u in enumerate(part)]
                outp["partial"] = L.summarize_parts(ai, fixed_part, spk_namer(plan), lambda p: prog(si, p))
            outp["model"] = ai.model
            outp["calls"] = ai.calls
        except Exception as e:
            print(f"::warning title=AI 整理 {k + 1}/{m} 失敗::{type(e).__name__}: {str(e)[:200]}", flush=True)
            outp["err"] = str(e)[:300]
        finally:
            if ai:
                ai.close()
    put(f"w-llm-{k}.json", outp)
    print(f"::notice title=AI 整理 {k + 1}/{m}::花了 {time.time() - t0:.0f} 秒", flush=True)


# ---------------- 4. 合併 ----------------
def final():
    ok = os.environ.get("NEEDS_OK", "1") == "1"
    if not ok:
        raise RuntimeError("其中一台伺服器處理失敗，請再試一次")
    plan = get("w-plan.json")
    t_start = plan.get("t_start", time.time())
    segs, bad = build_segs(plan)
    job = plan["job"]
    r = {"dur": round(plan.get("dur", 0), 2), "lang": job.get("lang"), "model": "text" if job.get("mode") == "text" else plan["model"],
         "segs": segs, "speakers": len({u["spk"] for u in segs})}
    if plan.get("llm"):
        cc = None
        if job.get("tw", 1):
            import opencc
            cc = opencc.OpenCC("s2twp")
        parts, errs, model, calls = [], [], None, 0
        for k in range(plan["llm"]):
            try:
                o = get(f"w-llm-{k}.json")
            except Exception as e:
                errs.append(str(e))
                continue
            for i, txt in o.get("fixed", {}).items():
                u = segs[int(i)]
                txt = T.to_tw(txt, cc)
                if txt != u["text"]:
                    u["raw"] = u["text"]
                    u["text"] = txt
            parts += o.get("partial") or []
            if o.get("err"):
                errs.append(o["err"])
            model = model or o.get("model")
            calls += o.get("calls", 0)
        if job.get("summ") and parts:
            import llm as L
            T.status("AI 彙整成分章節的重點")
            ai = L.LLM("local", T.status)
            try:
                s = L.summarize_merge(ai, parts)
                calls += ai.calls
            finally:
                ai.close()
            r["summary"] = json.loads(T.to_tw(json.dumps(s, ensure_ascii=False), cc)) if cc else s
        r["llm"] = {"engine": "local", "model": model, "calls": calls, "servers": plan["llm"]}
        if errs and not r.get("summary") and job.get("summ"):
            r["llm_error"] = errs[0]
    r["took"] = round(time.time() - t_start, 1)
    if bad:
        print(f"::notice title=略過的段落::{bad} 段音訊無法辨識（已略過，其他內容正常）", flush=True)
    print(f"::notice title=完成::{len(segs)} 句、{r['speakers']} 位講者，全部花了 {r['took'] / 60:.1f} 分鐘", flush=True)
    if T.TEST:
        if r.get("summary"):
            s = r["summary"]
            print(f"::notice title=AI 重點整理::{s.get('title')}｜章節：" + "／".join(c.get("title", "") for c in s.get("chapters", [])) + f"｜待辦 {len(s.get('actions', []))} 項")
        elif r.get("llm_error"):
            print(f"::notice title=AI 整理失敗::{r['llm_error']}")
        for u in segs[:8]:
            print(f"::notice title=講者{u['spk']} {u['s']:.1f}s::{u['text']}" + (f"  〔原：{u['raw']}〕" if u.get("raw") else ""))
        clean_work(keep_result=False)
        T.gh("DELETE", f"/releases/{T.REL}")
        return
    put("result.json", r)
    clean_work(keep_result=True)
    T.status("完成", 100, state="done")


if __name__ == "__main__":
    if os.environ.get("WORK_REL"):
        T.REL = os.environ["WORK_REL"]
    try:
        {"prep": prep, "asr": lambda: asr_shard(SHARD), "llm": lambda: llm_shard(SHARD), "final": final}[PHASE]()
    except Exception as e:
        traceback.print_exc()
        tb = traceback.format_exc().strip().splitlines()
        print(f"::error title={PHASE} 失敗::{type(e).__name__}: {str(e)[:200]} @ {' | '.join(l.strip() for l in tb[-4:-1])[:400]}")
        if PHASE in ("prep", "final") and T.REL:
            T.status("失敗", state="error", err=str(e)[:300])
            try:
                clean_work(keep_result=False)
                if T.TEST:
                    T.gh("DELETE", f"/releases/{T.REL}")
            except Exception:
                pass
        sys.exit(1)
