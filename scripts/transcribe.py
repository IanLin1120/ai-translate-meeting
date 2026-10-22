#!/usr/bin/env python3
"""GitHub 雲端會議轉文字（在 GitHub Actions 上執行）

流程：讀取草稿 Release 裡 App 上傳的錄音 → 下載後立刻從 GitHub 刪除錄音
      → 分辨講者（diarize.py）→ 每段發言用 SenseVoice（或 Whisper）轉文字
      → 結果 result.json 上傳回同一個草稿 Release，App 取回後會刪除整個 Release。
隱私：這個儲存庫是公開的，Actions 紀錄任何人都看得到，所以這裡「絕不」印出逐字稿內容。
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
REL = os.environ.get("RELEASE_ID", "").strip()
TEST = os.environ.get("TEST_FILE", "").strip()
MODELS = os.path.expanduser("~/.cache/aitm")
SR = 16000
API = f"https://api.github.com/repos/{REPO}"
HALL = re.compile(r"(字幕|訂閱|订阅|點贊|点赞|打賞|打赏|明鏡|明镜|Amara|謝謝觀看|谢谢观看|感謝觀看|感谢观看|請不吝|请不吝|優優獨播|优优独播|字幕志願|字幕志愿|Thank you for watching|Thanks for watching|Please subscribe|like and subscribe)", re.I)


def gh(method, url, data=None, ctype="application/json", accept="application/vnd.github+json", raw=False):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url if url.startswith("http") else API + url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", ctype)
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                b = r.read()
                return b if raw else (json.loads(b) if b else {})
        except urllib.error.HTTPError as e:
            if e.code in (404, 422) or i == 3:
                raise
        except Exception:
            if i == 3:
                raise
        time.sleep(3 * (i + 1))


def status(stage, p=None, state="running", err=None):
    """把進度寫在草稿 Release 的說明欄，App 會輪詢顯示。"""
    msg = f"{stage}" + (f" {p:.0f}%" if p is not None else "")
    print("status:", msg, flush=True)
    if not REL:
        return
    body = {"state": state, "stage": stage, "p": None if p is None else round(p, 1), "t": int(time.time())}
    if err:
        body["err"] = err
    try:
        gh("PATCH", f"/releases/{REL}", {"body": json.dumps(body, ensure_ascii=False)})
    except Exception as e:
        print("status update failed", e, flush=True)


def download_asset(a, dest):
    req = urllib.request.Request(f"{API}/releases/assets/{a['id']}")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)


def decode(path):
    """用 FFmpeg（imageio-ffmpeg 內附）轉成 16 kHz 單聲道 float32，支援各種音訊與影片格式"""
    import imageio_ffmpeg
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         check=True, stdout=subprocess.PIPE).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def to_tw(text, cc):
    return cc.convert(text) if cc and re.search(r"[一-鿿]", text) else text


def load_job():
    tmp = tempfile.mkdtemp()
    if TEST:
        job = {"parts": [{"name": os.path.basename(TEST), "t0": 0}], "lang": "auto", "model": os.environ.get("TEST_MODEL") or "firered",
               "speakers": 0, "tw": 1, "llm": os.environ.get("TEST_LLM") or "", "fix": 1, "summ": 1}
        files = {os.path.basename(TEST): TEST}
    else:
        rel = gh("GET", f"/releases/{REL}")
        assets = {a["name"]: a for a in rel["assets"]}
        if "job.json" not in assets:
            raise RuntimeError("找不到 job.json")
        jp = os.path.join(tmp, "job.json")
        download_asset(assets["job.json"], jp)
        job = json.load(open(jp, encoding="utf-8"))
        files = {}
        if job.get("mode") == "text":  # 只做 AI 整理（逐字稿已經在 job.json 裡）
            try:
                gh("DELETE", f"/releases/assets/{assets['job.json']['id']}")
            except Exception:
                pass
            return job, None
        status("下載錄音", 0)
        names = [p["name"] for p in job["parts"]]
        for i, n in enumerate(names):
            if n not in assets:
                raise RuntimeError(f"缺少錄音檔 {n}")
            dest = os.path.join(tmp, n)
            download_asset(assets[n], dest)
            files[n] = dest
            status("下載錄音", (i + 1) / len(names) * 100)
        # 錄音已經在伺服器上 → 立刻從 GitHub 刪除
        for n in names + ["job.json"]:
            try:
                gh("DELETE", f"/releases/assets/{assets[n]['id']}")
            except Exception as e:
                print("delete asset failed", e, flush=True)
        print("錄音已從 GitHub 刪除", flush=True)
    return job, files


def asr(job, files, t_start):
    # ---- 合併各段錄音（依開始時間排好） ----
    status("解碼音訊")
    parts = []
    for p in job["parts"]:
        a = decode(files[p["name"]])
        parts.append((float(p.get("t0", 0)), a))
        if not TEST:
            os.remove(files[p["name"]])
    total = max([int(t0 * SR) + len(a) for t0, a in parts] + [0])
    audio = np.zeros(total, dtype=np.float32)
    for t0, a in parts:
        s = int(t0 * SR)
        audio[s:s + len(a)] = a[: total - s]
    dur = total / SR
    print(f"音訊長度 {dur / 60:.1f} 分鐘", flush=True)
    if dur < 0.5:
        return {"dur": dur, "segs": [], "speakers": 0}

    # ---- 1. 講者辨識：先切段、算聲紋、分群（diarize.py） ----
    import diarize as DZ
    n_spk = int(job.get("speakers") or 0)
    sens = float(job.get("sens") or 0.65)
    status("分辨講者", 0)
    last = [0.0]

    def prog(p):
        if time.time() - last[0] > 20:
            last[0] = time.time()
            status("分辨講者", p)
    pieces, embs = DZ.segments_and_embeddings(audio, MODELS, prog, threads=os.cpu_count() or 4)
    labs = DZ.label(pieces, embs, n_spk, sens)
    print(f"段落 {len(pieces)}，講者 {len(set(labs))} 位", flush=True)

    # ---- 2. 相鄰同一人的段落合併成「一段發言」（每段只會有一個人） ----
    turns = []
    for (s, e), k, x in zip(pieces, labs, embs):
        if turns and turns[-1]["spk"] == k and s - turns[-1]["e"] < 0.8 and e - turns[-1]["s"] < 25:
            turns[-1]["e"] = e
            if x is not None:
                turns[-1]["E"].append(x)
        else:
            turns.append({"s": s, "e": e, "spk": k, "E": [x] if x is not None else []})

    # ---- 3. 每段發言各自轉文字 ----
    model = job.get("model") or "firered"
    lang = job.get("lang") or "auto"
    if model == "firered" and lang in ("ja", "ko", "yue"):
        model = "sensevoice"  # FireRedASR 只支援中文、英文
    if model in ("firered", "sensevoice") and lang not in ("auto", "zh", "en", "ja", "ko", "yue"):
        model = "large-v3-turbo"  # 其他語言改用 Whisper
    def clip(u):
        """取出這段發言的音訊；太短（或超出錄音範圍）的段落會讓 FireRedASR 當掉（Invalid input shape），
        小於 0.1 秒直接略過，0.1～0.5 秒補靜音到 0.5 秒"""
        a = audio[int(max(0, u["s"] - 0.15) * SR):int(min(dur, max(u["e"], u["s"]) + 0.15) * SR)]
        if len(a) < SR // 10 or not np.isfinite(a).all():
            return None
        if len(a) < SR // 2:
            a = np.concatenate([a, np.zeros(SR // 2 - len(a), dtype=np.float32)])
        return a
    bad = [0]
    status("語音轉文字", 0)
    if model == "firered":
        import sherpa_onnx
        d = fetch_model("firered", "asr-models/sherpa-onnx-fire-red-asr-large-zh_en-2025-02-16.tar.bz2",
                        ["encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"])
        pd = fetch_model("punct", "punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12.tar.bz2", ["model.onnx"])
        rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr(encoder=f"{d}/encoder.int8.onnx", decoder=f"{d}/decoder.int8.onnx",
                                                              tokens=f"{d}/tokens.txt", num_threads=os.cpu_count() or 4)
        pc = sherpa_onnx.OfflinePunctuation(sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(ct_transformer=f"{pd}/model.onnx", num_threads=2)))
        t_asr = time.time()
        for i, u in enumerate(turns):
            a = clip(u)
            if a is None:
                u["text"] = ""
                continue
            try:
                st = rec.create_stream()
                st.accept_waveform(SR, a)
                rec.decode_stream(st)
                txt = st.result.text.strip()
            except Exception as e:  # 單一段落失敗不要讓整場會議失敗
                bad[0] += 1
                print("略過一段無法辨識的音訊：", type(e).__name__, flush=True)
                u["text"] = ""
                continue
            # FireRedASR 的英文是全大寫 → 轉成一般寫法（縮寫保留大寫）
            txt = re.sub(r"[A-Z][A-Z']+", lambda m: m.group(0) if len(m.group(0)) <= 3 else m.group(0).lower(), txt)
            u["text"] = pc.add_punctuation(txt) if txt else ""
            if time.time() - last[0] > 20:
                last[0] = time.time()
                el = time.time() - t_asr
                status("語音轉文字", min(99, (i + 1) / len(turns) * 100))
        det = lang
    elif model == "sensevoice":
        import sherpa_onnx
        d = f"{MODELS}/sensevoice"
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(model=f"{d}/model.int8.onnx", tokens=f"{d}/tokens.txt",
                                                             num_threads=os.cpu_count() or 4, use_itn=True,
                                                             language=lang if lang in ("zh", "en", "ja", "ko", "yue") else "auto")
        B = 16
        for i in range(0, len(turns), B):
            batch = []
            for u in turns[i:i + B]:
                a = clip(u)
                u["text"] = ""
                if a is not None:
                    batch.append((u, a))
            ss = []
            for u, a in batch:
                st = rec.create_stream()
                st.accept_waveform(SR, a)
                ss.append(st)
            try:
                if ss:
                    rec.decode_streams(ss)
                for (u, a), st in zip(batch, ss):
                    u["text"] = st.result.text
            except Exception as e:
                print("一批音訊辨識失敗，改成逐段辨識：", type(e).__name__, flush=True)
                for u, a in batch:
                    try:
                        st = rec.create_stream()
                        st.accept_waveform(SR, a)
                        rec.decode_stream(st)
                        u["text"] = st.result.text
                    except Exception:
                        bad[0] += 1
            if time.time() - last[0] > 20:
                last[0] = time.time()
                status("語音轉文字", min(99, (i + B) / len(turns) * 100))
        det = lang
    else:
        from faster_whisper import WhisperModel
        status("載入語音模型")
        wm = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4, download_root=f"{MODELS}/whisper")
        wl = None if lang in ("auto", "") else lang
        prompt = "以下是會議錄音，內容是繁體中文和 English 夾雜。" if wl in (None, "zh") else None
        det = wl
        for i, u in enumerate(turns):
            a = clip(u)
            u["text"] = ""
            if a is None:
                continue
            try:
                segs, info = wm.transcribe(a, language=wl, beam_size=5, vad_filter=False, condition_on_previous_text=False,
                                           without_timestamps=True, initial_prompt=prompt)
                u["text"] = "".join(s.text for s in segs if not (s.no_speech_prob > 0.8 and s.avg_logprob < -1))
                det = det or info.language
            except Exception as e:
                bad[0] += 1
                print("略過一段無法辨識的音訊：", type(e).__name__, flush=True)
            if time.time() - last[0] > 20:
                last[0] = time.time()
                status("語音轉文字", min(99, (i + 1) / len(turns) * 100))

    # ---- 4. 整理：繁體、去掉幻聽字幕、附上聲紋向量（App 可以再「重新分辨講者」） ----
    cc = None
    if job.get("tw", 1):
        import opencc
        cc = opencc.OpenCC("s2twp")
    res = []
    for u in turns:
        t = re.sub(r"\s+", " ", u.get("text") or "").strip()
        t = re.sub(r"(?<=[\u4e00-\u9fff，。！？、]) (?=[\u4e00-\u9fff])", "", t)
        t = re.sub(r"(.{2,20}?)(?:\s*\1){3,}", r"\1\1", t)
        if not t or re.fullmatch(r"[\s.。,，!！?？…\-]*", t) or (HALL.search(t) and len(t) < 40):
            continue
        item = {"s": round(u["s"], 2), "e": round(u["e"], 2), "spk": int(u["spk"]), "text": to_tw(t, cc)}
        if u["E"]:
            item["emb"] = [round(float(x), 4) for x in np.mean(np.stack(u["E"]), 0)]
        res.append(item)
    ids = list(dict.fromkeys(u["spk"] for u in res))
    remap = {k: i + 1 for i, k in enumerate(ids)}
    for u in res:
        u["spk"] = remap[u["spk"]]
    if bad[0]:
        print(f"::notice title=略過的段落::{bad[0]} 段音訊無法辨識（已略過，其他內容正常）", flush=True)
    print(f"完成：{len(res)} 句、{len(ids)} 位講者，耗時 {(time.time() - t_start) / 60:.1f} 分鐘", flush=True)
    return {"dur": round(dur, 2), "lang": det, "model": model, "speakers": len(ids), "segs": res,
            "took": round(time.time() - t_start, 1)}


def fetch_model(name, rel_path, files):
    d = f"{MODELS}/{name}"
    if all(os.path.exists(f"{d}/{f}") for f in files):
        return d
    status("下載語音模型")
    print(f"::notice title=下載模型::{name}（快取裡沒有）", flush=True)
    os.makedirs(d, exist_ok=True)
    tmpd = tempfile.mkdtemp()
    subprocess.run(f"curl -sSL https://github.com/k2-fsa/sherpa-onnx/releases/download/{rel_path} | tar xj -C {tmpd}", shell=True, check=True)
    for f in files:
        src = glob.glob(f"{tmpd}/*/{f}")[0]
        shutil.move(src, f"{d}/{f}")
    shutil.rmtree(tmpd, ignore_errors=True)
    return d


def main():
    t_start = time.time()
    job, files = load_job()
    if job.get("mode") == "text":
        r = {"dur": job.get("dur", 0), "lang": job.get("lang"), "model": "text", "segs": job.get("segs", []), "speakers": 0}
    else:
        r = asr(job, files, t_start)
    names = {int(k): v for k, v in (job.get("names") or {}).items()}
    spk_name = lambda u: names.get(int(u.get("spk", 0)), f"講者 {u.get('spk', 0)}")
    eng = job.get("llm") or ""
    if eng in ("github", "local") and r["segs"] and (job.get("fix") or job.get("summ")):
        import llm as L
        ai = None
        try:
            ai = L.LLM(eng, status)
            cc = None
            if job.get("tw", 1):
                import opencc
                cc = opencc.OpenCC("s2twp")
            if job.get("fix"):
                status("AI 修正錯字", 0)
                fixed = L.correct(ai, r["segs"], spk_name, lambda p: status("AI 修正錯字", p))
                for i, txt in fixed.items():
                    u = r["segs"][i]
                    txt = to_tw(txt, cc)
                    if txt != u["text"]:
                        u["raw"] = u["text"]
                        u["text"] = txt
                print(f"AI 修正 {len(fixed)} 句", flush=True)
            if job.get("summ"):
                status("AI 重點整理", 0)
                s = L.summarize(ai, r["segs"], spk_name, lambda p: status("AI 重點整理", p))
                r["summary"] = json.loads(to_tw(json.dumps(s, ensure_ascii=False), cc)) if cc else s
            r["llm"] = {"engine": eng, "model": ai.model, "calls": ai.calls}
            print(f"AI 整理完成：{ai.model}，{ai.calls} 次呼叫", flush=True)
        except Exception as e:
            print(f"::warning title=AI 整理失敗::{type(e).__name__}: {str(e)[:200]}", flush=True)
            r["llm_error"] = str(e)[:300]
        finally:
            if ai:
                ai.close()
    r["took"] = round(time.time() - t_start, 1)
    return r


if __name__ == "__main__":
    try:
        r = main()
        if TEST:
            print("TEST RESULT:")
            print(f"::notice title=測試結果::{r['took']} 秒・{r['speakers']} 位講者・語言 {r.get('lang')}")
            for u in r["segs"]:
                print(f"  講者{u['spk']} [{u['s']:.1f}-{u['e']:.1f}] {u['text']}")
                print(f"::notice title=講者{u['spk']} {u['s']:.1f}s::{u['text']}" + (f"  〔原：{u['raw']}〕" if u.get("raw") else ""))
            if r.get("summary"):
                s = r["summary"]
                print(f"::notice title=AI 重點整理 {r.get('llm', {}).get('model')}::{s.get('title')}｜{s.get('overview')}｜章節：" +
                      "／".join(c.get("title", "") for c in s.get("chapters", [])) + f"｜待辦：{json.dumps(s.get('actions', []), ensure_ascii=False)[:300]}")
            if r.get("llm_error"):
                print(f"::notice title=AI 整理失敗::{r['llm_error']}")
            sys.exit(0)
        data = json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode()
        rel = gh("GET", f"/releases/{REL}")
        up = rel["upload_url"].split("{")[0]
        gh("POST", f"{up}?name=result.json", data, ctype="application/json")
        status("完成", 100, state="done")
    except Exception as e:
        traceback.print_exc()
        tb = traceback.format_exc().strip().splitlines()
        print(f"::error title=轉文字失敗::{type(e).__name__}: {str(e)[:200]} @ {' | '.join(l.strip() for l in tb[-4:-1])[:400]}")
        status("失敗", state="error", err=str(e)[:300])
        sys.exit(1)
