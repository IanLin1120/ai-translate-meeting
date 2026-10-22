"""講者辨識（會議用）

1. pyannote segmentation 3.0（sherpa-onnx）切出說話段落與換人的時間點
2. 每段（長段再切成約 4 秒）用 3D-Speaker ERes2Net 算聲紋向量
3. 平均連結階層分群（自動估計人數，或使用指定人數）＋依群中心反覆修正
4. 太零碎的群併到最像的講者；相鄰同一人的段落合併
"""
import numpy as np

SR = 16000


def _norm(x):
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)


def _kmeans(X, k, iters=60, seed=0):
    """簡單的 k-means++（不需要 scikit-learn）"""
    rng = np.random.default_rng(seed)
    best, best_in = None, np.inf
    for rep in range(6):
        c = [X[rng.integers(len(X))]]
        for _ in range(1, k):
            d = np.min(((X[:, None, :] - np.array(c)[None]) ** 2).sum(-1), axis=1)
            c.append(X[rng.choice(len(X), p=d / d.sum())] if d.sum() > 0 else X[rng.integers(len(X))])
        C = np.array(c)
        for _ in range(iters):
            lab = np.argmin(((X[:, None, :] - C[None]) ** 2).sum(-1), axis=1)
            newC = np.array([X[lab == j].mean(0) if np.any(lab == j) else C[j] for j in range(k)])
            if np.allclose(newC, C):
                break
            C = newC
        inertia = ((X - C[lab]) ** 2).sum()
        if inertia < best_in:
            best, best_in = lab, inertia
    return best


def nme_sc(E, max_k=15, k=None):
    """NME-SC (Park et al. 2019)：用正規化最大特徵間隙自動挑 p 和講者數"""
    n = len(E)
    if n <= 2:
        return np.zeros(n, int)
    S = _norm(E) @ _norm(E).T
    S = (S + 1) / 2  # 0..1
    max_k = min(max_k, n - 1)
    best = None
    pmax = max(2, min(n - 1, int(n * 0.3)))
    for p in sorted(set(np.linspace(2, pmax, num=min(20, pmax - 1)).astype(int))):
        A = np.zeros_like(S)
        idx = np.argsort(-S, axis=1)[:, :p]
        np.put_along_axis(A, idx, 1.0, axis=1)
        A = (A + A.T) / 2
        D = np.diag(A.sum(1))
        L = D - A
        w = np.linalg.eigvalsh(L)
        gaps = np.diff(w[: max_k + 1])
        kk = int(np.argmax(gaps)) + 1
        g = gaps.max() / (w.max() + 1e-9)
        r = (p / n) / (g + 1e-9)
        if best is None or r < best[0]:
            best = (r, p, kk, L)
    _, p, kk, L = best
    if k:
        kk = min(k, n)
    if kk <= 1:
        return np.zeros(n, int)
    w, v = np.linalg.eigh(L)
    X = _norm(v[:, :kk])
    return _kmeans(X, kk)


def cluster(X, dur, k=None, thr=0.65, min_frac=0.015):
    """平均連結階層分群 → 零碎小群併入最像的群 → 依群中心反覆修正
    指定人數 k：先用自動結果；人數不夠就降低門檻細分，太多就合併最像的群
    （預設值依 AMI 會議語料評估：4 場 25–39 分鐘會議，平均 DER 12.2%）"""
    from scipy.cluster.hierarchy import linkage, fcluster
    n = len(X)
    if n < 2:
        return np.zeros(n, int)
    X = _norm(X)
    Z = linkage(X, "average", metric="cosine")
    tot = float(dur.sum())
    min_s = min(30.0, max(3.0, tot * min_frac))

    def cent(lab):
        return {j: _norm((X[lab == j] * dur[lab == j, None]).sum(0)) for j in np.unique(lab)}

    def absorb(lab, floor):
        C = cent(lab)
        while len(C) > max(1, floor):
            T = {j: dur[lab == j].sum() for j in C}
            s = min(T, key=T.get)
            if T[s] >= min_s:
                break
            t = max((j for j in C if j != s), key=lambda j: float(C[s] @ C[j]))
            lab[lab == s] = t
            C = cent(lab)
        return lab

    th = thr
    while True:
        lab = np.unique(fcluster(Z, th, "distance"), return_inverse=True)[1]
        lab = absorb(lab, k or 1)
        if not k or len(np.unique(lab)) >= k or th <= 0.3:
            break
        th -= 0.05
    for _ in range(8):
        C = cent(lab)
        while k and len(C) > k:
            ks = list(C)
            a, b = max(((a, b) for i, a in enumerate(ks) for b in ks[i + 1:]), key=lambda p: float(C[p[0]] @ C[p[1]]))
            lab[lab == b] = a
            C = cent(lab)
        K = list(C)
        new = np.array(K)[np.argmax(X @ np.stack([C[j] for j in K]).T, 1)]
        if (new == lab).all():
            break
        lab = absorb(new, k or 1)
    return np.unique(lab, return_inverse=True)[1]


def segments_and_embeddings(audio, models, progress=None, threads=4, max_piece=6.0):
    import sherpa_onnx
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=f"{models}/seg/model.onnx"), num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"{models}/emb.onnx", num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.3),
        min_duration_on=0.2, min_duration_off=0.3)
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    cb = (lambda d, n: (progress(d / max(n, 1) * 60), 0)[1]) if progress else None
    res = sd.process(audio, callback=cb).sort_by_start_time()
    raw = [(r.start, r.end) for r in res]
    raw_spk = [int(r.speaker) for r in res]
    # pyannote 在錄音最後一個視窗（補零的部分）可能回報超出錄音長度的段落 → 截掉，太短的不要
    total = len(audio) / 16000
    keep = [i for i, (s, e) in enumerate(raw) if s < total - 0.05 and min(e, total) - s >= 0.05]
    raw_spk = [raw_spk[i] for i in keep]
    raw = [(raw[i][0], min(raw[i][1], total)) for i in keep]
    segments_and_embeddings.raw_spk = raw_spk
    segments_and_embeddings.raw = raw  # 給平行化版本用（每段原始發言各自轉文字）
    pieces = []
    for s, e in raw:
        d = e - s
        if d > max_piece:
            n = int(np.ceil(d / 4))
            st = d / n
            pieces += [(s + i * st, s + (i + 1) * st) for i in range(n)]
        else:
            pieces.append((s, e))
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"{models}/emb.onnx", num_threads=threads))
    embs = [None] * len(pieces)
    for i, (s, e) in enumerate(pieces):
        if e - s < 0.8:
            continue
        st = ext.create_stream()
        st.accept_waveform(SR, audio[int(s * SR):int(e * SR)])
        st.input_finished()
        embs[i] = np.array(ext.compute(st), dtype=np.float32)
        if progress and i % 50 == 0:
            progress(60 + 35 * i / max(1, len(pieces)))
    return pieces, embs


def label(pieces, embs, num_speakers=0, thr=0.65, min_frac=0.015):
    good = [i for i, x in enumerate(embs) if x is not None]
    if not good:
        return [0] * len(pieces)
    E = np.stack([embs[i] for i in good])
    dur = np.array([pieces[i][1] - pieces[i][0] for i in good])
    lab = cluster(E, dur, k=num_speakers or None, thr=thr, min_frac=min_frac)
    labels = [None] * len(pieces)
    for j, i in enumerate(good):
        labels[i] = int(lab[j])
    mids = np.array([(pieces[j][0] + pieces[j][1]) / 2 for j in good])
    for i in range(len(pieces)):
        if labels[i] is None:
            m = (pieces[i][0] + pieces[i][1]) / 2
            labels[i] = labels[good[int(np.argmin(np.abs(mids - m)))]]
    order = {}
    for l in labels:
        order.setdefault(l, len(order))
    return [order[l] for l in labels]


def diarize(audio, models, num_speakers=0, thr=0.65, progress=None, threads=4):
    pieces, embs = segments_and_embeddings(audio, models, progress, threads)
    if not pieces:
        return []
    labs = label(pieces, embs, num_speakers, thr)
    return [(s, e, l, x) for (s, e), l, x in zip(pieces, labs, embs)]
