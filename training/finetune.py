#!/usr/bin/env python3
"""用你自己校對過的會議錄音，微調（fine-tune）一個專屬的語音辨識模型

流程：
  1. 從你的 GitHub 草稿 Release「train-data」下載 App 上傳的訓練資料（錄音＋你校對過的逐字稿）
  2. 切成一句一句（每句 ≤ 30 秒），保留 10% 當作考題
  3. 先用原本的模型考一次（字錯率 CER），再用 LoRA 微調，訓練完再考一次
  4. 有進步才轉成 CTranslate2 int8 格式，上傳到新的草稿 Release「custom-model-日期」
     → App「設定 → ☁️ GitHub 雲端轉文字」就能選「我的模型」
建議在 Google Colab（免費 T4 GPU）執行 finetune_whisper.ipynb；也可以在有 NVIDIA GPU 的電腦執行。
隱私：草稿 Release 只有儲存庫擁有者看得到；這支程式不會印出逐字稿內容。
"""
import argparse, json, math, os, random, re, shutil, subprocess, sys, tarfile, tempfile, time, urllib.request

import numpy as np

SR = 16000


# ---------------- GitHub ----------------
class GH:
    def __init__(self, repo, token):
        self.api = f"https://api.github.com/repos/{repo}"
        self.token = token

    def req(self, method, url, data=None, ctype="application/json", accept="application/vnd.github+json", raw=False):
        if data is not None and not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data).encode()
        r = urllib.request.Request(url if url.startswith("http") else self.api + url, data=data, method=method)
        r.add_header("Authorization", f"Bearer {self.token}")
        r.add_header("Accept", accept)
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            r.add_header("Content-Type", ctype)
        with urllib.request.urlopen(r, timeout=600) as f:
            b = f.read()
            return b if raw else (json.loads(b) if b else {})

    def releases(self):
        out, page = [], 1
        while True:
            r = self.req("GET", f"/releases?per_page=100&page={page}")
            out += r
            if len(r) < 100:
                return out
            page += 1

    def download(self, asset_id, dest):
        r = urllib.request.Request(f"{self.api}/releases/assets/{asset_id}")
        r.add_header("Authorization", f"Bearer {self.token}")
        r.add_header("Accept", "application/octet-stream")
        with urllib.request.urlopen(r, timeout=1800) as f, open(dest, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)

    def upload(self, rel, path, name):
        up = rel["upload_url"].split("{")[0]
        with open(path, "rb") as f:
            data = f.read()
        return self.req("POST", f"{up}?name={name}", data, ctype="application/octet-stream")


def decode(path):
    import imageio_ffmpeg
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         check=True, stdout=subprocess.PIPE).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def norm(t):
    """算字錯率用：去掉標點與空白、英文小寫"""
    return re.sub(r"[\s，。、！？,.!?：:；;「」『』（）()…~\-\"'“”‘’]", "", str(t)).lower()


# ---------------- 1. 訓練資料 ----------------
def load_data(gh, local_dir, tmp, max_sec=30):
    metas = []  # (json path, audio path)
    if local_dir:
        for f in sorted(os.listdir(local_dir)):
            if f.endswith(".json"):
                j = json.load(open(os.path.join(local_dir, f), encoding="utf-8"))
                metas.append((j, os.path.join(local_dir, j["audio"])))
    else:
        rel = next((r for r in gh.releases() if r.get("tag_name") == "train-data"), None)
        if not rel:
            sys.exit("❌ 找不到訓練資料：請先在 App「設定 → 🎓 訓練我的語音模型」上傳訓練資料")
        assets = {a["name"]: a for a in rel["assets"]}
        for n, a in assets.items():
            if not n.endswith(".json"):
                continue
            jp = os.path.join(tmp, n)
            gh.download(a["id"], jp)
            j = json.load(open(jp, encoding="utf-8"))
            if j.get("audio") not in assets:
                continue
            ap = os.path.join(tmp, j["audio"])
            gh.download(assets[j["audio"]]["id"], ap)
            metas.append((j, ap))
    items = []
    for j, ap in metas:
        audio = decode(ap)
        for s in j.get("segs", []):
            t = str(s.get("text") or "").strip()
            a, b = float(s["s"]), float(s["e"])
            if not t or b - a < 0.6 or b - a > max_sec:
                continue
            x = audio[int(max(0, a - 0.1) * SR):int((b + 0.15) * SR)]
            if len(x) < SR // 2:
                continue
            items.append({"audio": x, "text": t, "mid": j.get("id", ap)})
    return items, len(metas)


# ---------------- 2. 考試（字錯率） ----------------
def evaluate(model, proc, items, device, bs=8, lang="zh"):
    import torch, jiwer
    model.eval()
    hyps, refs = [], []
    for i in range(0, len(items), bs):
        batch = items[i:i + bs]
        feats = proc.feature_extractor([x["audio"] for x in batch], sampling_rate=SR, return_tensors="pt").input_features.to(device, dtype=next(model.parameters()).dtype)
        with torch.no_grad():
            ids = model.generate(input_features=feats, language=lang, task="transcribe", max_new_tokens=220)
        hyps += proc.batch_decode(ids, skip_special_tokens=True)
        refs += [x["text"] for x in batch]
    pairs = [(norm(r), norm(h)) for r, h in zip(refs, hyps) if norm(r)]
    cer = jiwer.cer([r for r, _ in pairs], [h for _, h in pairs]) if pairs else 1.0
    return float(cer)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="雲端儲存庫（留白：自動用權杖擁有者的 ai-translate-meeting，沒有 Fork 就用原作者的）")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--base", default="openai/whisper-large-v3-turbo", help="基礎模型（Hugging Face 名稱）")
    ap.add_argument("--epochs", type=float, default=3)
    ap.add_argument("--max_steps", type=int, default=0, help="測試用：最多訓練幾步")
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--bs", type=int, default=8)
    ap.add_argument("--lora_r", type=int, default=32)
    ap.add_argument("--local", default="", help="測試用：本機訓練資料資料夾（不從 GitHub 下載）")
    ap.add_argument("--no_upload", action="store_true")
    ap.add_argument("--force", action="store_true", help="就算沒有進步也上傳")
    ap.add_argument("--eval_max", type=int, default=300)
    a = ap.parse_args()
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    from peft import LoraConfig, get_peft_model
    t0 = time.time()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    print(f"🖥  運算裝置：{device}" + ("（沒有 GPU，只適合測試，正式訓練請用 Colab 的 GPU）" if device == "cpu" else f"（{torch.cuda.get_device_name(0)}）"), flush=True)
    if not a.repo and a.token:  # 自動判斷：用自己 Fork 的儲存庫
        try:
            me = GH("x/y", a.token).req("GET", "https://api.github.com/user")["login"]
            GH(f"{me}/ai-translate-meeting", a.token).req("GET", "")
            a.repo = f"{me}/ai-translate-meeting"
        except Exception:
            a.repo = "IanLin1120/ai-translate-meeting"
        print(f"📦 儲存庫：{a.repo}", flush=True)
    a.repo = a.repo or "IanLin1120/ai-translate-meeting"
    gh = GH(a.repo, a.token) if a.token else None
    if not a.local and not gh:
        sys.exit("❌ 請提供 GitHub 權杖（--token）")
    tmp = tempfile.mkdtemp()
    print("⬇ 下載訓練資料…", flush=True)
    items, n_meet = load_data(gh, a.local, tmp)
    hours = sum(len(x["audio"]) for x in items) / SR / 3600
    print(f"📚 {n_meet} 場會議、{len(items)} 句、{hours * 60:.0f} 分鐘語音", flush=True)
    if len(items) < 20:
        sys.exit("❌ 訓練資料太少（至少要 20 句；建議 1 小時以上校對過的錄音）")
    random.seed(7)
    random.shuffle(items)
    n_eval = max(10, min(a.eval_max, len(items) // 10))
    ev, tr = items[:n_eval], items[n_eval:]
    print(f"✂️  訓練 {len(tr)} 句、考試 {len(ev)} 句", flush=True)

    proc = WhisperProcessor.from_pretrained(a.base, language="zh", task="transcribe")
    model = WhisperForConditionalGeneration.from_pretrained(a.base, torch_dtype=dtype).to(device)
    model.generation_config.forced_decoder_ids = None
    print("📝 原本的模型考試中…", flush=True)
    base_cer = evaluate(model, proc, ev, device, a.bs)
    print(f"   原本字錯率 CER = {base_cer * 100:.2f}%", flush=True)

    # ---- LoRA 微調 ----
    model = get_peft_model(model, LoraConfig(r=a.lora_r, lora_alpha=a.lora_r * 2, lora_dropout=0.05,
                                             target_modules=["q_proj", "k_proj", "v_proj", "out_proj", "fc1", "fc2"]))
    for n, p in model.named_parameters():
        if p.requires_grad:
            p.data = p.data.float()
    model.print_trainable_parameters()
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=a.lr, weight_decay=0.01)
    steps_per_epoch = math.ceil(len(tr) / a.bs)
    total = a.max_steps or int(steps_per_epoch * a.epochs)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / max(1, total * 0.06)) * max(0.0, 1 - s / max(1, total)))
    scaler = torch.cuda.amp.GradScaler(enabled=device == "cuda")
    tok = proc.tokenizer
    prefix = tok("", add_special_tokens=True).input_ids[:-1]  # <|startoftranscript|><|zh|><|transcribe|><|notimestamps|>
    eot = tok.eos_token_id
    print(f"🏋️  開始訓練：{total} 步（約 {total / steps_per_epoch:.1f} 輪）", flush=True)
    model.train()
    step, t_last = 0, time.time()
    while step < total:
        random.shuffle(tr)
        for i in range(0, len(tr), a.bs):
            if step >= total:
                break
            batch = tr[i:i + a.bs]
            feats = proc.feature_extractor([x["audio"] for x in batch], sampling_rate=SR, return_tensors="pt").input_features.to(device, dtype=dtype)
            labs = [prefix + tok(x["text"], add_special_tokens=False).input_ids[:440] + [eot] for x in batch]
            L = max(len(l) for l in labs)
            dec_in = torch.tensor([l[:-1] + [eot] * (L - len(l)) for l in labs], device=device)
            target = torch.tensor([l[1:] + [-100] * (L - len(l)) for l in labs], device=device)
            target[:, : len(prefix) - 1] = -100  # 語言與任務標記不算分
            with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=device == "cuda"):
                out = model(input_features=feats, decoder_input_ids=dec_in)
                loss = torch.nn.functional.cross_entropy(out.logits.float().reshape(-1, out.logits.size(-1)), target.reshape(-1), ignore_index=-100)
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            scaler.step(opt)
            scaler.update()
            opt.zero_grad(set_to_none=True)
            sched.step()
            step += 1
            if step % 20 == 0 or step == total or time.time() - t_last > 60:
                t_last = time.time()
                el = time.time() - t0
                print(f"   第 {step}/{total} 步　loss {loss.item():.3f}　已花 {el / 60:.0f} 分鐘", flush=True)
    model = model.merge_and_unload()
    model.to(dtype)
    print("📝 訓練後的模型考試中…", flush=True)
    new_cer = evaluate(model, proc, ev, device, a.bs)
    print(f"   訓練後字錯率 CER = {new_cer * 100:.2f}%（原本 {base_cer * 100:.2f}%）", flush=True)
    metrics = {"base": a.base, "base_cer": round(base_cer, 4), "new_cer": round(new_cer, 4), "meetings": n_meet, "segments": len(items),
               "minutes": round(hours * 60, 1), "steps": total, "date": time.strftime("%Y-%m-%d %H:%M"), "minutes_used": round((time.time() - t0) / 60, 1), "test": bool(a.local)}
    better = new_cer < base_cer
    if not better and not a.force:
        print("⚠️  訓練後沒有比較準，不上傳（可以多校對一些會議再訓練，或加 --force 強制上傳）", flush=True)
        json.dump(metrics, open("metrics.json", "w"), ensure_ascii=False)
        return metrics
    # ---- 轉成 CTranslate2（雲端用 faster-whisper 執行） ----
    print("📦 轉換格式…", flush=True)
    hf_dir, ct_dir = os.path.join(tmp, "hf"), os.path.join(tmp, "ct2")
    model.save_pretrained(hf_dir)
    proc.save_pretrained(hf_dir)
    from transformers import WhisperTokenizerFast  # faster-whisper 需要 tokenizer.json
    WhisperTokenizerFast.from_pretrained(a.base, language="zh", task="transcribe").save_pretrained(hf_dir)
    import ctranslate2.converters
    ctranslate2.converters.TransformersConverter(hf_dir, copy_files=["tokenizer.json", "preprocessor_config.json"]).convert(ct_dir, quantization="int8_float16" if device == "cuda" else "int8", force=True)
    json.dump(metrics, open(os.path.join(ct_dir, "metrics.json"), "w"), ensure_ascii=False)
    tar = os.path.join(tmp, "model.tar")
    with tarfile.open(tar, "w") as t:
        t.add(ct_dir, arcname="model")
    size = os.path.getsize(tar) / 1048576
    print(f"   模型 {size:.0f} MB", flush=True)
    if a.no_upload or not gh:
        shutil.copy(tar, "model.tar")
        json.dump(metrics, open("metrics.json", "w"), ensure_ascii=False)
        print("✅ 完成（沒有上傳）", flush=True)
        return metrics
    print("⬆ 上傳到 GitHub…", flush=True)
    tag = "custom-model-" + time.strftime("%Y%m%d-%H%M")
    rel = gh.req("POST", "/releases", {"tag_name": tag, "name": tag, "draft": True, "body": json.dumps(metrics, ensure_ascii=False)})
    parts = []
    with open(tar, "rb") as f:
        k = 0
        while True:
            b = f.read(1900 * 1048576)  # 單一附件上限 2 GB
            if not b:
                break
            p = os.path.join(tmp, f"model.tar.part{k:03d}")
            open(p, "wb").write(b)
            gh.upload(rel, p, os.path.basename(p))
            os.remove(p)
            parts.append(os.path.basename(p))
            k += 1
    mp = os.path.join(tmp, "metrics.json")
    json.dump({**metrics, "parts": parts}, open(mp, "w"), ensure_ascii=False)
    gh.upload(rel, mp, "metrics.json")
    print(f"✅ 完成！字錯率 {base_cer * 100:.2f}% → {new_cer * 100:.2f}%，到 App「設定 → ☁️ GitHub 雲端轉文字」選「我的模型」就會使用", flush=True)
    return metrics


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        tb = traceback.format_exc().strip().splitlines()
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=訓練失敗::{type(e).__name__}: {str(e)[:300]} @ {' | '.join(l.strip() for l in tb[-5:-1])[:600]}")
        print(f"\n❌ 訓練失敗：{type(e).__name__}: {e}")
        sys.exit(1)
