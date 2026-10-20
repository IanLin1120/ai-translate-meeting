#!/usr/bin/env python3
"""GitHub 雲端會議轉文字（在 GitHub Actions 上執行）

流程：讀取草稿 Release 裡 App 上傳的錄音 → 下載後立刻從 GitHub 刪除錄音
      → faster-whisper 轉文字（中英夾雜） → sherpa-onnx 分辨講者
      → 結果 result.json 上傳回同一個草稿 Release，App 取回後會刪除整個 Release。
隱私：這個儲存庫是公開的，Actions 紀錄任何人都看得到，所以這裡「絕不」印出逐字稿內容。
"""
import json, os, re, subprocess, sys, tempfile, time, traceback, urllib.request, urllib.error
import numpy as np

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
    """轉成 16 kHz 單聲道 float32（PyAV／FFmpeg，支援各種音訊與影片格式）"""
    from faster_whisper.audio import decode_audio
    return np.asarray(decode_audio(path, sampling_rate=SR), dtype=np.float32)


def to_tw(text, cc):
    return cc.convert(text) if cc and re.search(r"[一-鿿]", text) else text


def main():
    t_start = time.time()
    tmp = tempfile.mkdtemp()
    if TEST:
        job = {"parts": [{"name": os.path.basename(TEST), "t0": 0}], "lang": "auto", "model": "large-v3-turbo", "speakers": 0, "tw": 1}
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

    # ---- 合併各段錄音（依開始時間排好） ----
    status("解碼音訊")
    parts = []
    for p in job["parts"]:
        a = decode(files[p["name"]])
        parts.append((float(p.get("t0", 0)), a))
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

    # ---- 講者辨識（sherpa-onnx：pyannote segmentation 3.0 + 3D-Speaker ERes2Net 聲紋） ----
    import sherpa_onnx
    status("分辨講者", 0)
    n_spk = int(job.get("speakers") or 0)
    thr = float(job.get("thr") or 0.5)
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=f"{MODELS}/seg/model.onnx"), num_threads=4),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"{MODELS}/emb.onnx", num_threads=4),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=n_spk if n_spk > 0 else -1, threshold=thr),
        min_duration_on=0.3, min_duration_off=0.5)
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    last = [0.0]

    def cb(done, n):
        if time.time() - last[0] > 20:
            last[0] = time.time()
            status("分辨講者", done / max(n, 1) * 100)
        return 0
    turns = [(r.start, r.end, r.speaker) for r in sd.process(audio, callback=cb).sort_by_start_time()]
    print(f"講者段落 {len(turns)}，講者 {len(set(t[2] for t in turns))} 位", flush=True)

    # ---- 語音轉文字（faster-whisper，CPU int8） ----
    from faster_whisper import WhisperModel
    model = job.get("model") or "large-v3-turbo"
    status("載入語音模型")
    wm = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4, download_root=f"{MODELS}/whisper")
    lang = job.get("lang") or "auto"
    lang = None if lang in ("auto", "") else lang
    prompt = "以下是會議錄音，內容是繁體中文和 English 夾雜。" if lang in (None, "zh") else None
    status("語音轉文字", 0)
    segs, info = wm.transcribe(audio, language=lang, task="transcribe", beam_size=5, vad_filter=True,
                               vad_parameters={"min_silence_duration_ms": 500}, word_timestamps=True,
                               condition_on_previous_text=False, initial_prompt=prompt)
    words = []
    for s in segs:
        if time.time() - last[0] > 20:
            last[0] = time.time()
            status("語音轉文字", min(99, s.end / dur * 100))
        if s.no_speech_prob > 0.8 and s.avg_logprob < -1:
            continue
        for w in (s.words or []):
            words.append((w.start, w.end, w.word))
    print(f"偵測語言 {info.language}，{len(words)} 個詞", flush=True)

    # ---- 每個詞配給重疊最多的講者，連續同一講者的詞合併成句子 ----
    def spk_at(a, b):
        best, bo = None, 0.0
        for s, e, k in turns:
            if e < a - 2:
                continue
            if s > b + 2:
                break
            o = min(b, e) - max(a, s)
            if o > bo:
                bo, best = o, k
        if best is None:  # 沒重疊 → 取最近的段落
            m = min(turns, key=lambda t: min(abs(t[0] - b), abs(t[1] - a)), default=None)
            best = m[2] if m else 0
        return best

    cc = None
    if job.get("tw", 1):
        import opencc
        cc = opencc.OpenCC("s2twp")
    out, cur = [], None
    for a, b, w in words:
        k = spk_at(a, b)
        brk = cur is None or k != cur["spk"] or a - cur["e"] > 1.2 or len(cur["text"]) > 200
        if not brk and re.search(r"[。！？!?.]$", cur["text"].strip()) and a - cur["e"] > 0.6:
            brk = True
        if brk:
            if cur:
                out.append(cur)
            cur = {"s": round(a, 2), "e": round(b, 2), "spk": int(k), "text": w}
        else:
            cur["e"] = round(b, 2)
            cur["text"] += w
    if cur:
        out.append(cur)
    res = []
    for u in out:
        t = re.sub(r"\s+", " ", u["text"]).strip()
        t = re.sub(r"(?<=[一-鿿，。！？、]) (?=[一-鿿])", "", t)
        t = re.sub(r"(.{2,20}?)(?:\s*\1){3,}", r"\1\1", t)
        if not t or re.fullmatch(r"[\s.。,，!！?？…\-]*", t) or (HALL.search(t) and len(t) < 40):
            continue
        u["text"] = to_tw(t, cc)
        res.append(u)

    # ---- 每句的聲紋向量（App 可以在手機上「重新分辨講者」） ----
    status("整理結果")
    try:
        ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"{MODELS}/emb.onnx", num_threads=4))
        for u in res:
            a = audio[int(u["s"] * SR):int(u["e"] * SR)]
            if len(a) < SR:
                continue
            st = ext.create_stream()
            st.accept_waveform(SR, a)
            st.input_finished()
            u["emb"] = [round(x, 4) for x in ext.compute(st)]
    except Exception as e:
        print("embedding skipped", e, flush=True)
    ids = sorted({u["spk"] for u in res})
    remap = {k: i + 1 for i, k in enumerate(ids)}
    for u in res:
        u["spk"] = remap[u["spk"]]
    print(f"完成：{len(res)} 句、{len(ids)} 位講者，耗時 {(time.time() - t_start) / 60:.1f} 分鐘", flush=True)
    return {"dur": round(dur, 2), "lang": info.language, "model": model, "speakers": len(ids), "segs": res,
            "took": round(time.time() - t_start, 1)}


if __name__ == "__main__":
    try:
        r = main()
        if TEST:
            print("TEST RESULT:")
            print(f"::notice title=測試結果::{r['took']} 秒・{r['speakers']} 位講者・語言 {r.get('lang')}")
            for u in r["segs"]:
                print(f"  講者{u['spk']} [{u['s']:.1f}-{u['e']:.1f}] {u['text']}")
                print(f"::notice title=講者{u['spk']} {u['s']:.1f}s::{u['text']}")
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
