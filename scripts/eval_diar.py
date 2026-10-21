#!/usr/bin/env python3
"""用 AMI 會議語料（真實會議、有人工標註）評估講者辨識參數，結果寫成 Actions 註記。"""
import io, json, os, subprocess, sys, tempfile, time, urllib.request
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import diarize as D

MODELS = os.path.expanduser("~/.cache/aitm")
N = int(os.environ.get("N_MEETINGS", "3"))
CFG = os.environ.get("AMI_CFG", "sdm")


def note(t, m):
    print(f"::notice title={t}::{m}", flush=True)


def der(hyp, gt, T):
    from scipy.optimize import linear_sum_assignment
    n = int(T * 100) + 1
    gk = {k: i for i, k in enumerate(sorted({k for *_, k in gt}))}
    hk = {k: i for i, k in enumerate(sorted({k for *_, k in hyp}))}
    G = np.zeros((len(gk), n), bool)
    H = np.full(n, -1)
    for s, e, k in gt:
        G[gk[k], int(s * 100):int(e * 100)] = True
    for s, e, k in hyp:
        H[int(s * 100):int(e * 100)] = hk[k]
    speech = G.any(0)
    C = np.zeros((len(gk), max(1, len(hk))))
    for h in range(len(hk)):
        m = H == h
        C[:, h] = G[:, m].sum(1)
    r, c = linear_sum_assignment(-C)
    mp = dict(zip(c, r))
    ok = 0
    for h, g in mp.items():
        ok += np.sum((H == h) & G[g])
    return 1 - ok / speech.sum(), len(hk)


def decode(b):
    import imageio_ffmpeg
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    f.write(b)
    f.close()
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", f.name, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         check=True, stdout=subprocess.PIPE).stdout
    return np.frombuffer(out, np.float32).copy()


def main():
    import pyarrow.parquet as pq
    lst = json.load(urllib.request.urlopen(f"https://huggingface.co/api/datasets/diarizers-community/ami/parquet/{CFG}/test"))
    print("parquet files", len(lst), flush=True)
    results = {}
    done = 0
    for url in lst:
        if done >= N:
            break
        tmp = "/tmp/ami.parquet"
        subprocess.run(["curl", "-sSL", "-o", tmp, url], check=True)
        t = pq.read_table(tmp)
        for row in t.to_pylist():
            if done >= N:
                break
            a = decode(row["audio"]["bytes"])
            T = len(a) / 16000
            gt = list(zip(row["timestamps_start"], row["timestamps_end"], row["speakers"]))
            nspk = len(set(row["speakers"]))
            t0 = time.time()
            pieces, embs = D.segments_and_embeddings(a, MODELS)
            te = time.time() - t0
            line = []
            for name, k in [("自動", 0), ("指定人數", nspk)]:
                labs = D.label(pieces, embs, k)
                d, kk = der([(s, e, l) for (s, e), l in zip(pieces, labs)], gt, T)
                results.setdefault(name, []).append((d, kk, nspk))
                line.append(f"{name}:{d*100:.1f}%/{kk}人")
            note(f"會議{done+1} {T/60:.0f}分 實際{nspk}人 ({te:.0f}s)", " ".join(line))
            done += 1
    for k, v in results.items():
        note(f"平均 {k}", f"DER {np.mean([x[0] for x in v])*100:.1f}%  人數 {[x[1] for x in v]} 實際 {[x[2] for x in v]}")


if __name__ == "__main__":
    main()
