#!/usr/bin/env python3
"""把 models.json 列出的 Hugging Face 開源模型鏡像到 models 分支。

- 超過 90 MiB 的檔案切成 .part000、.part001…（GitHub 單檔上限 100 MB）
- manifest.json 記錄每個檔案的大小、sha256、分片數，App 依此從 GitHub 下載並組回
- 已同步且 sha256 相同的檔案會跳過；每完成一個模型（或大檔）就 commit + push
用法：python3 sync_models.py <models.json> <models 分支工作目錄>
"""
import hashlib, json, os, re, subprocess, sys, time, urllib.request, urllib.parse

HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
PART = 90 * 1024 * 1024
PUSH_EVERY = 1500 * 1024 * 1024  # 每推送約 1.5 GB 就 push 一次（GitHub 單次 push 上限 2 GB）
SKIP_EXT = (".bin", ".safetensors", ".h5", ".msgpack", ".ot", ".pt", ".ckpt", ".gguf", ".tflite", ".mlmodel", ".pth")
SKIP_NAME = (".gitattributes",)

cfg_path, out = sys.argv[1], sys.argv[2]
cfg = json.load(open(cfg_path, encoding="utf-8"))
man_path = os.path.join(out, "manifest.json")
man = json.load(open(man_path, encoding="utf-8")) if os.path.exists(man_path) else {"files": {}, "models": {}}
pending = 0


def git(*a):
    subprocess.run(["git", "-C", out, *a], check=True)


def push(msg):
    global pending
    man["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(man, open(man_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    git("add", "-A")
    if subprocess.run(["git", "-C", out, "diff", "--cached", "--quiet"]).returncode == 0:
        return
    git("commit", "-q", "-m", msg)
    for i in range(4):
        if subprocess.run(["git", "-C", out, "push", "-q", "origin", "HEAD:models"]).returncode == 0:
            break
        time.sleep(20 * (i + 1))
    else:
        raise SystemExit("push failed")
    pending = 0
    print("pushed:", msg, flush=True)


def api(model):
    u = f"{HF}/api/models/{model}?blobs=true"
    for i in range(5):
        try:
            return json.load(urllib.request.urlopen(u, timeout=60))
        except Exception as e:
            print("api retry", model, e, flush=True)
            time.sleep(10 * (i + 1))
    raise SystemExit("cannot read " + model)


def want(name, onnx):
    if name in SKIP_NAME or name.lower().endswith(SKIP_EXT):
        return False
    m = re.match(r"^onnx/(.+?)\.onnx(_data(_\d+)?)?$", name)
    if m:
        return m.group(1) in onnx
    if name.startswith("onnx/"):
        return False
    return True


def download(url, dest, size):
    tmp = dest + ".tmp"
    for i in range(6):
        r = subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "5", "-C", "-", "-o", tmp, url])
        if r.returncode == 0 and (size is None or os.path.getsize(tmp) == size):
            os.replace(tmp, dest)
            return
        print("download retry", url, flush=True)
        time.sleep(10 * (i + 1))
    raise SystemExit("download failed: " + url)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


for m in cfg["models"]:
    mid, onnx = m["id"], set(m.get("onnx", []))
    info = api(mid)
    files = [s for s in info.get("siblings", []) if want(s["rfilename"], onnx)]
    got = {re.sub(r"\.onnx(_data.*)?$", "", f["rfilename"][5:]) for f in files if f["rfilename"].startswith("onnx/")}
    missing = sorted(onnx - got)
    if missing:
        print(f"⚠ {mid} 沒有這些 ONNX 檔：{missing}", flush=True)
    total = 0
    for s in files:
        name = s["rfilename"]
        size = s.get("size") or (s.get("lfs") or {}).get("size")
        sha = (s.get("lfs") or {}).get("sha256") or s.get("blobId")
        key = f"{mid}/{name}"
        old = man["files"].get(key)
        if old and old.get("src") == sha:
            total += old["size"]
            continue
        dest = os.path.join(out, key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # 清掉舊的分片
        d = os.path.dirname(dest)
        for f in os.listdir(d):
            if f.startswith(os.path.basename(dest) + ".part"):
                os.remove(os.path.join(d, f))
        url = f"{HF}/{mid}/resolve/main/{urllib.parse.quote(name)}"
        print(f"⬇ {key} ({(size or 0) / 1048576:.1f} MB)", flush=True)
        download(url, dest, size)
        real = os.path.getsize(dest)
        ent = {"size": real, "sha256": sha256(dest), "src": sha}
        if real > PART:
            n = 0
            with open(dest, "rb") as f:
                while True:
                    b = f.read(PART)
                    if not b:
                        break
                    open(f"{dest}.part{n:03d}", "wb").write(b)
                    n += 1
            os.remove(dest)
            ent["parts"] = n
        man["files"][key] = ent
        total += real
        pending += real
        if pending >= PUSH_EVERY:
            push(f"models: {mid} ({name})")
    man["models"][mid] = {"use": m.get("use", ""), "onnx": sorted(got & onnx), "size": total}
    push(f"models: {mid}")

push("models: manifest")
print("完成", flush=True)
