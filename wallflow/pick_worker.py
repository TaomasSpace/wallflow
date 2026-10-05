"""The 3D-layer editor's worker - runs INSIDE the depth venv (numpy, torch, SAM 2.1).

wallflow's own interpreter never imports any of this. `wallflow depth edit` starts
`<venv>/bin/python pick_worker.py serve '{...}'` and talks JSON lines over stdin:

    -> {"id": 3, "cmd": "pick", "args": {"x": 0.41, "y": 0.62}}
    <- status: loading SAM 2.1 …          (any number of these)
    <- done: {"id": 3, "cmd": "pick", "view": "...", "level": 1, "levels": 3, ...}
    <- error: what went wrong              (instead of done)

You point, SAM 2.1 segments what you pointed at. The mask (working resolution,
long side 1536) and its undo history live here; after every change a small indexed
PNG at the editor's display size is written (0 = behind, 1 = in front, 2 = its
outline) and the editor only recolours it. Coordinates arrive as fractions 0..1.

One-shot mode `render '{"jobs": [...]}'` turns saved masks into cutouts without
SAM (used by `wallflow depth edit import`).
"""
import json
import os
import sys
import time
import traceback

import numpy as np
from PIL import Image, ImageDraw

WORK_LONG = 1536        # SAM works at 1024 internally; masks are kept at this size
EXPORT_LONG = 3840      # cutouts: plenty for 1440p/4K screens, fast to filter and load


def say(*a):
    print(*a, flush=True)


def status(msg: str) -> None:
    say(f"status: {msg}")


# --- image helpers -----------------------------------------------------------------

def load_image(src: str) -> Image.Image:
    img = Image.open(src)
    img.load()
    return img.convert("RGB")


def work_size(w: int, h: int, long_side: int = WORK_LONG) -> tuple[int, int]:
    s = min(1.0, long_side / max(w, h))
    return max(1, round(w * s)), max(1, round(h * s))


def box(a: np.ndarray, r: int) -> np.ndarray:
    a = np.pad(a, ((r, r), (r, r)), mode="edge")
    c = np.cumsum(np.cumsum(a, 0, dtype=np.float64), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    d = 2 * r + 1
    return ((c[d:, d:] - c[:-d, d:] - c[d:, :-d] + c[:-d, :-d]) / (d * d)).astype(np.float32)


def guided(p: np.ndarray, guide: np.ndarray, r: int, eps: float) -> np.ndarray:
    """He et al. guided filter, grey guide: a hard mask gets the image's own soft edges."""
    mI, mp = box(guide, r), box(p, r)
    cov = box(guide * p, r) - mI * mp
    var = box(guide * guide, r) - mI * mI
    a = cov / (var + eps)
    b = mp - a * mI
    return np.clip(box(a, r) * guide + box(b, r), 0, 1).astype(np.float32)


def clean(m: np.ndarray, frac: float = 0.0002) -> np.ndarray:
    """Drop specks and fill pinholes smaller than `frac` of the image."""
    from scipy import ndimage
    t = frac * m.size
    lab, n = ndimage.label(m)
    if n > 1:
        sizes = np.bincount(lab.ravel())
        keep = sizes >= t
        keep[0] = False
        keep[int(np.argmax(sizes[1:])) + 1] = True
        m = keep[lab]
    holes, n = ndimage.label(~m)
    if n:
        sizes = np.bincount(holes.ravel())
        small = sizes < t
        small[0] = False
        m = m | small[holes]
    return m


def polygon(pts, shape) -> np.ndarray:
    H, W = shape
    img = Image.new("L", (W, H), 0)
    if len(pts) >= 3:
        ImageDraw.Draw(img).polygon([(float(x), float(y)) for x, y in pts], fill=1)
    return np.asarray(img, bool)


def resize_mask(m: np.ndarray, w: int, h: int) -> np.ndarray:
    if m.shape == (h, w):
        return m
    return np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)) > 127


def load_alpha(path: str, w: int, h: int, alpha: bool) -> np.ndarray | None:
    """A cutout's alpha (alpha=True) or a grey mask, as a bool mask at (w, h)."""
    if not path or not os.path.exists(path):
        return None
    try:
        im = Image.open(path)
        a = im.getchannel("A") if alpha and "A" in im.getbands() else im.convert("L")
        return np.asarray(a.resize((w, h), Image.BILINEAR)) > 127
    except Exception:
        return None


def render_cutout(src: str, mask: np.ndarray, out: str) -> dict:
    """mask (any size) -> RGBA cutout at up to EXPORT_LONG, soft edges near the boundary."""
    img = load_image(src)
    W0, H0 = img.size
    W, H = work_size(W0, H0, EXPORT_LONG)
    if (W, H) != (W0, H0):
        img = img.resize((W, H), Image.LANCZOS)
    rgb = np.asarray(img)
    m = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((W, H), Image.BILINEAR),
                   np.float32) / 255.0
    hard = (m > 0.5).astype(np.float32)
    if hard.sum() < 0.002 * W * H:
        return {"empty": True, "coverage": 0.0}
    guide = (rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)) / 255.0
    r = max(3, round(max(W, H) / 900))
    soft = guided(hard, guide, r, 1e-3)
    from scipy import ndimage
    edge = hard.astype(bool) ^ ndimage.binary_erosion(hard.astype(bool), iterations=1, border_value=1)
    band = ndimage.binary_dilation(edge, iterations=r)
    alpha = np.where(band, soft, hard)
    rgba = np.dstack([rgb, (np.clip(alpha, 0, 1) * 255 + 0.5).astype(np.uint8)])
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".part.png"
    Image.fromarray(rgba, "RGBA").save(tmp, "PNG", compress_level=1)
    os.replace(tmp, out)
    return {"empty": False, "coverage": round(float(hard.mean()), 4)}


def save_mask(mask: np.ndarray, path: str, meta: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part.png"
    Image.fromarray(mask.astype(np.uint8) * 255, "L").save(tmp, "PNG")
    os.replace(tmp, path)
    with open(os.path.splitext(path)[0] + ".json", "w") as f:
        json.dump(meta, f, indent=1)


# --- SAM 2.1 ---------------------------------------------------------------------------

class Sam:
    def __init__(self, model_id: str):
        import torch
        from sam2.build_sam import build_sam2_hf
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        self.torch = torch
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        if self.dev == "cuda":
            torch.set_float32_matmul_precision("high")
        status(f"loading {model_id.split('/')[-1]} on {self.dev.upper()} (first time: downloading it)")
        self.pred = SAM2ImagePredictor(build_sam2_hf(model_id, device=self.dev))

    def _ctx(self):
        import contextlib
        if self.dev == "cuda":
            return self.torch.autocast("cuda", dtype=self.torch.bfloat16)
        return contextlib.nullcontext()

    def set_image(self, rgb: np.ndarray) -> None:
        with self.torch.inference_mode(), self._ctx():
            self.pred.set_image(rgb)

    def predict(self, points=None, labels=None, box=None, multi=True, mask_input=None):
        kw = {"multimask_output": multi}
        if points:
            kw["point_coords"] = np.asarray(points, np.float32)
            kw["point_labels"] = np.asarray(labels, np.int32)
        if box is not None:
            kw["box"] = np.asarray(box, np.float32)
        if mask_input is not None:
            kw["mask_input"] = np.asarray(mask_input, np.float32)
        with self.torch.inference_mode(), self._ctx():
            masks, scores, _ = self.pred.predict(**kw)
        return np.asarray(masks) > 0.5, np.asarray(scores, np.float32)

    def features(self):
        """The image encoder's output for the current image, float32 on the model's device:
        (256 ch @ 64x64, 64 ch @ 128x128, 32 ch @ 256x256) - what the learned head reads."""
        f = self.pred._features
        hi = f["high_res_feats"]
        return f["image_embed"].float(), hi[1].float(), hi[0].float()


# --- the learned guess ("learn my taste") -------------------------------------------------------
# A small head on top of SAM 2.1's frozen image features + the depth map predicts which
# pixels you put in front. SAM already knows what things are; the head only learns your
# rule for what goes forward, so a few dozen masks are enough to start. Its 256x256 output
# lives in SAM's own (square, 1024px) frame, which is also what SAM's decoder takes as a
# mask prompt - so each predicted region is re-cut by SAM for crisp edges.

HEAD_VERSION = 1


def make_head():
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    def block(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.GroupNorm(8, o), nn.GELU())

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.drop = nn.Dropout2d(0.1)
            self.a = nn.Sequential(block(256 + 2, 128), block(128, 128))      # features + depth + height
            self.glob = nn.Linear(128, 128)
            self.b = block(128 + 64 + 1, 64)
            self.c = nn.Sequential(block(64 + 32 + 1, 32), nn.Conv2d(32, 1, 1))

        def forward(self, e, f1, f0, d):
            n = e.shape[0]
            d64 = F.interpolate(d, size=(64, 64), mode="area")
            d128 = F.interpolate(d, size=(128, 128), mode="area")
            ys = torch.linspace(-1, 1, 64, device=e.device).view(1, 1, 64, 1).expand(n, 1, 64, 64)
            x = self.a(torch.cat([self.drop(e), d64, ys], 1))
            x = x + self.glob(x.mean((2, 3)))[:, :, None, None]       # whole-image context
            x = F.interpolate(x, size=(128, 128), mode="bilinear", align_corners=False)
            x = self.b(torch.cat([x, f1, d128], 1))
            x = F.interpolate(x, size=(256, 256), mode="bilinear", align_corners=False)
            return self.c(torch.cat([x, f0, d], 1))

    return Head()


def depth_small(a: dict, small: np.ndarray) -> np.ndarray:
    """Depth (1 = near) at the working size: wallflow's cached map if the automatic cutout
    already made one, else our own small cache, else Depth Anything V2 (ONNX) right now."""
    H, W = small.shape[:2]

    def norm(d):
        d = np.asarray(Image.fromarray(np.asarray(d, np.float32), "F").resize((W, H), Image.BILINEAR), np.float32)
        lo, hi = np.percentile(d, 1), np.percentile(d, 99)
        return np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1)

    for p in (a.get("depth_full"), a.get("depth_cache")):
        if p and os.path.exists(p):
            try:
                return norm(np.load(p))
            except Exception:
                pass
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    path = hf_hub_download(a.get("depth_model") or "onnx-community/depth-anything-v2-small", "onnx/model.onnx")
    prov = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
    sess = ort.InferenceSession(path, providers=prov)
    name = sess.get_inputs()[0].name
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    img = Image.fromarray(small)

    def infer(nw, nh):
        x = np.asarray(img.resize((nw, nh), Image.BICUBIC)).astype(np.float32) / 255.0
        return sess.run(None, {name: ((x - mean) / std).transpose(2, 0, 1)[None]})[0][0]

    sc = 518.0 / max(W, H)
    nw, nh = max(14, round(W * sc / 14) * 14), max(14, round(H * sc / 14) * 14)
    try:
        out = infer(nw, nh)
    except Exception:
        out = infer(518, 518)
    out = np.asarray(out, np.float32)
    if out.ndim == 3:
        out = out[0]
    d = norm(out)
    if a.get("depth_cache"):
        os.makedirs(os.path.dirname(a["depth_cache"]), exist_ok=True)
        np.save(a["depth_cache"], d.astype(np.float16))
    return d


def depth_tensor(d: np.ndarray, torch, dev: str, flip: bool = False):
    d256 = np.asarray(Image.fromarray(d, "F").resize((256, 256), Image.BILINEAR), np.float32)
    if flip:
        d256 = d256[:, ::-1].copy()
    return torch.from_numpy(d256)[None, None].to(dev)


_heads: dict = {}


def load_head(path: str, torch, dev: str):
    key = (path, os.stat(path).st_mtime_ns, dev)
    if key not in _heads:
        ck = torch.load(path, map_location=dev, weights_only=False)
        if ck.get("version") != HEAD_VERSION:
            raise RuntimeError("learned model is from another version - run `wallflow depth train`")
        h = make_head().to(dev)
        h.load_state_dict(ck["state"])
        h.eval()
        _heads.clear()
        _heads[key] = h
    return _heads[key]


def learned_mask(sam: "Sam", small: np.ndarray, a: dict) -> np.ndarray | None:
    """The head's guess for the image currently set in `sam`, re-cut by SAM for clean edges."""
    path = a.get("learned")
    if not path or not os.path.exists(path):
        return None
    torch = sam.torch
    H, W = small.shape[:2]
    head = load_head(path, torch, sam.dev)
    e, f1, f0 = sam.features()
    d = depth_tensor(depth_small(a, small), torch, sam.dev)
    with torch.no_grad():
        logit = head(e, f1, f0, d)[0, 0].float().cpu().numpy()             # 256x256, SAM frame
    prob = np.asarray(Image.fromarray(1 / (1 + np.exp(-logit)), "F").resize((W, H), Image.BILINEAR))
    rough = clean(prob > 0.5)
    if rough.sum() < 0.002 * rough.size:
        return rough
    from scipy import ndimage
    lab, n = ndimage.label(rough)
    out = np.zeros_like(rough)
    grow = max(2, int(0.02 * H))
    for i in range(1, n + 1):
        comp = lab == i
        if comp.sum() < 0.005 * comp.size:                                 # small bits: keep as guessed
            out |= comp
            continue
        ys, xs = np.nonzero(comp)
        bx = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        try:
            ms, _ = sam.predict(box=bx, multi=False, mask_input=np.clip(logit, -20, 20)[None])
            r = clean(ms[0]) & ndimage.binary_dilation(comp, iterations=grow)
            iou = (r & comp).sum() / max((r | comp).sum(), 1)
            out |= r if iou > 0.6 else comp                                # SAM only sharpens, never swaps
        except Exception:
            out |= comp
    return clean(out)


# --- one editing session -------------------------------------------------------------------

class Session:
    def __init__(self, sam_factory):
        self.sam_factory = sam_factory
        self.sam = None
        self.src = None

    # -- open ---------------------------------------------------------------------------
    def open(self, a: dict) -> dict:
        t0 = time.monotonic()
        self.src = a["src"]
        img = load_image(self.src)
        W, H = work_size(*img.size)
        self.small = np.asarray(img.resize((W, H), Image.LANCZOS))
        self.shape = (H, W)
        self.view = (int(a["view_w"]), int(a["view_h"]), a["view_out"])
        os.makedirs(os.path.dirname(a["view_out"]) or ".", exist_ok=True)
        self.paths = {k: a.get(k) or "" for k in ("mask", "manual", "auto", "subject")}
        self.learn_args = {k: a.get(k) or "" for k in ("learned", "depth_full", "depth_cache", "depth_model")}
        self.history, self.last = [], None
        if self.sam is None:
            self.sam = self.sam_factory()
        status("reading the image")
        self.sam.set_image(self.small)
        self.mask, self.start = self._initial(a.get("start", "best"))
        return self._view(start=self.start, seconds=round(time.monotonic() - t0, 1))

    def _initial(self, which: str):
        H, W = self.shape
        p = self.paths
        cands = {
            "saved": lambda: load_alpha(p["mask"], W, H, alpha=False),
            "manual": lambda: load_alpha(p["manual"], W, H, alpha=True),
            "learned": self._learned,
            "auto": lambda: load_alpha(p["auto"], W, H, alpha=True),
            "subject": lambda: load_alpha(p["subject"], W, H, alpha=False),
        }
        order = {"best": ["saved", "manual", "learned", "auto", "subject"],
                 "auto": ["learned", "auto", "subject"]}.get(which, [which])
        for k in order:
            m = cands[k]() if k in cands else None
            if m is not None:
                return clean(m), k
        return np.zeros(self.shape, bool), "empty"

    def _learned(self):
        try:
            status("asking the learned model")
            return learned_mask(self.sam, self.small, self.learn_args)
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            say(f"(learned guess failed: {type(e).__name__}: {e})")
            return None

    # -- view ---------------------------------------------------------------------------
    def _view(self, **extra) -> dict:
        vw, vh, out = self.view
        m = resize_mask(self.mask, vw, vh)
        lab = m.astype(np.uint8)
        edge = np.zeros_like(m)
        edge[:, 1:] |= m[:, 1:] != m[:, :-1]
        edge[1:, :] |= m[1:, :] != m[:-1, :]
        edge &= m
        from scipy import ndimage
        edge = ndimage.binary_dilation(edge, iterations=1) & m
        lab[edge] = 2
        im = Image.fromarray(lab, "P")
        im.putpalette([0, 0, 0, 255, 255, 255, 255, 0, 0] + [0] * (256 * 3 - 9))
        tmp = out + ".part.png"
        im.save(tmp, "PNG", compress_level=1)
        os.replace(tmp, out)
        L = self.last
        return {"view": out, "front": round(float(self.mask.mean()), 4),
                "level": L["level"] if L else 0, "levels": len(L["cands"]) if L else 0,
                "undo": len(self.history), **extra}

    # -- edits --------------------------------------------------------------------------
    def _px(self, x: float, y: float) -> tuple[int, int]:
        H, W = self.shape
        return int(min(max(x, 0.0), 0.9999) * W), int(min(max(y, 0.0), 0.9999) * H)

    def _push(self) -> None:
        self.history.append(self.mask.copy())
        del self.history[:-60]

    def _start(self, add: bool, cands: list, level: int, refinable: bool, **prompt) -> None:
        self._push()
        L = dict(before=self.mask.copy(), add=add, cands=cands, level=level, **prompt)
        self.last = L if refinable or len(cands) > 1 else None
        self._apply(L)

    def _apply(self, L: dict) -> None:
        m = L["cands"][L["level"]]
        if L.get("clip") is not None:
            m = m & L["clip"]
        self.mask = (L["before"] | m) if L["add"] else (L["before"] & ~m)

    def pick(self, a: dict) -> dict:
        x, y = self._px(a["x"], a["y"])
        masks, scores = self.sam.predict([[x, y]], [1], multi=True)
        order = np.argsort([int(m.sum()) for m in masks])            # small part -> whole thing
        cands = [clean(masks[i]) for i in order]
        level = int(np.argmax(scores[order]))
        add = not bool(self.mask[y, x])
        self._start(add, cands, level, True, pts=[[x, y]], lbl=[1], box=None, clip=None)
        return self._view(added=add)

    def cycle(self, a: dict) -> dict:
        L = self.last
        if L and len(L["cands"]) > 1:
            L["level"] = int(min(max(L["level"] + int(a.get("step", 1)), 0), len(L["cands"]) - 1))
            self._apply(L)
        return self._view()

    def refine(self, a: dict) -> dict:
        L = self.last
        if L is None or "pts" not in L:
            return self._view(note="refine works on the last click or lasso")
        x, y = self._px(a["x"], a["y"])
        L["pts"].append([x, y])
        L["lbl"].append(1 if a.get("include", True) else 0)
        masks, _ = self.sam.predict(L["pts"], L["lbl"], box=L["box"], multi=False)
        L["cands"], L["level"] = [clean(masks[0])], 0
        self._push()
        self._apply(L)
        return self._view()

    def lasso(self, a: dict) -> dict:
        H, W = self.shape
        pts = [(x * W, y * H) for x, y in a["pts"]]
        poly = polygon(pts, self.shape)
        if not poly.any():
            return self._view()
        add = bool(a.get("add", True))
        if a.get("exact"):
            self._start(add, [poly], 0, False)
            self.last = None
            return self._view()
        ys, xs = np.nonzero(poly)
        bx = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        masks, _ = self.sam.predict(box=bx, multi=True)
        cands = [clean(m) & poly for m in masks]
        iou = [float((c & poly).sum()) / max(float((c | poly).sum()), 1.0) for c in cands]
        order = np.argsort([int(c.sum()) for c in cands])
        cands = [cands[i] for i in order]
        level = int(np.argmax([iou[i] for i in order]))
        self._start(add, cands, level, True, pts=[], lbl=[], box=bx, clip=poly)
        return self._view()

    def below(self, a: dict) -> dict:
        """Everything below a stroke; the boundary snaps to image edges (GrabCut in a band)."""
        import cv2
        H, W = self.shape
        pts = [(x * W, y * H) for x, y in a["pts"]]
        if len(pts) < 2:
            return self._view()
        raw = polygon(pts + [(pts[-1][0], H + 1), (pts[0][0], H + 1)], self.shape)
        b = max(8, int(0.035 * H))
        line = Image.new("L", (W, H), 0)
        ImageDraw.Draw(line).line(pts, fill=1, width=1)
        band = cv2.dilate(np.asarray(line, np.uint8), np.ones((2 * b + 1, 2 * b + 1), np.uint8)) > 0
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        y0, y1 = max(0, int(min(ys)) - 3 * b), min(H, int(max(ys)) + 3 * b)
        x0, x1 = max(0, int(min(xs))), min(W, int(max(xs)) + 1)
        m = raw.copy()
        if y1 - y0 > 4 and x1 - x0 > 4:
            r, bd = raw[y0:y1, x0:x1], band[y0:y1, x0:x1]
            tri = np.where(r, cv2.GC_FGD, cv2.GC_BGD).astype(np.uint8)
            tri[bd & r] = cv2.GC_PR_FGD
            tri[bd & ~r] = cv2.GC_PR_BGD
            if (tri == cv2.GC_FGD).any() and (tri == cv2.GC_BGD).any():
                crop = np.ascontiguousarray(self.small[y0:y1, x0:x1, ::-1])
                bgd, fgd = np.zeros((1, 65)), np.zeros((1, 65))
                cv2.grabCut(crop, tri, None, bgd, fgd, 4, cv2.GC_INIT_WITH_MASK)
                m[y0:y1, x0:x1] = (tri == cv2.GC_FGD) | (tri == cv2.GC_PR_FGD)
        self._start(bool(a.get("add", True)), [clean(m)], 0, False)
        self.last = None
        return self._view()

    def set(self, a: dict) -> dict:
        self._push()
        self.last = None
        if a.get("what") == "clear":
            self.mask, start = np.zeros(self.shape, bool), "empty"
        else:
            self.mask, start = self._initial(a.get("what", "auto"))
        return self._view(start=start)

    def undo(self, a: dict) -> dict:
        if self.history:
            self.mask = self.history.pop()
            self.last = None
        return self._view()

    def apply(self, a: dict) -> dict:
        H, W = self.shape
        if a.get("mask_out"):
            save_mask(self.mask, a["mask_out"], {"src": self.src, "size": [W, H], "time": int(time.time())})
        if self.mask.sum() < 0.002 * self.mask.size:
            return {"empty": True, "coverage": 0.0}
        return render_cutout(self.src, self.mask, a["out"])


# --- training -----------------------------------------------------------------------------------

def _samples(sam: "Sam", jobs: list) -> tuple[list, list]:
    """Encode every (image, mask) once, plus its mirror image. Features stay on the CPU in
    half precision (~8 MB a sample); batches go to the GPU."""
    torch = sam.torch
    data, groups = [], []
    for i, j in enumerate(jobs):
        status(f"reading wallpaper {i + 1}/{len(jobs)}")
        try:
            img = load_image(j["src"])
            W, H = work_size(*img.size)
            small = np.asarray(img.resize((W, H), Image.LANCZOS))
            d = depth_small(j, small)
            m = np.asarray(Image.open(j["mask"]).convert("L").resize((256, 256), Image.BILINEAR),
                           np.float32) / 255.0
        except Exception as ex:
            say(f"(skipped {os.path.basename(j['src'])}: {type(ex).__name__}: {ex})")
            continue
        for flip in (False, True):
            sam.set_image(np.ascontiguousarray(small[:, ::-1]) if flip else small)
            e, f1, f0 = (t.half().cpu() for t in sam.features())
            dt = depth_tensor(d, torch, "cpu", flip).half()
            mt = torch.from_numpy(np.ascontiguousarray(m[:, ::-1] if flip else m))[None, None].half()
            data.append((e, f1, f0, dt, mt))
            groups.append(i)
    return data, groups


def _fit(data: list, idx: list, steps: int, dev: str, torch, label: str):
    import torch.nn.functional as F
    head = make_head().to(dev)
    opt = torch.optim.AdamW(head.parameters(), lr=1e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=1e-3, total_steps=steps, pct_start=0.1)
    rng = np.random.default_rng(0)
    head.train()
    bs = min(4, len(idx))
    for step in range(steps):
        pick = rng.choice(idx, size=bs, replace=len(idx) < bs)
        batch = [torch.cat([data[k][c] for k in pick]).to(dev).float() for c in range(5)]
        logit = head(*batch[:4])
        y = batch[4]
        p = torch.sigmoid(logit)
        dice = 1 - (2 * (p * y).sum((1, 2, 3)) + 1) / (p.sum((1, 2, 3)) + y.sum((1, 2, 3)) + 1)
        loss = F.binary_cross_entropy_with_logits(logit, y) + dice.mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        if step % 50 == 0 or step == steps - 1:
            status(f"{label}: step {step + 1}/{steps}  loss {loss.item():.3f}")
    head.eval()
    return head


def _iou(head, data: list, idx: list, dev: str, torch) -> float:
    vals = []
    with torch.no_grad():
        for k in idx:
            e, f1, f0, d, m = (t.to(dev).float() for t in data[k])
            pred = head(e, f1, f0, d) > 0
            y = m > 0.5
            vals.append(float((pred & y).sum()) / max(float((pred | y).sum()), 1.0))
    return float(np.mean(vals)) if vals else 0.0


def train(a: dict) -> dict:
    """Fit the head on every saved mask. With 10+ images a fifth of them is held out first,
    to report how well it guesses wallpapers it has never seen; then it trains on all."""
    t0 = time.monotonic()
    sam = Sam(a.get("model") or "facebook/sam2.1-hiera-large")
    torch = sam.torch
    torch.manual_seed(0)
    data, groups = _samples(sam, a["jobs"])
    n_img = len(set(groups))
    if n_img < 3:
        raise RuntimeError(f"only {n_img} usable mask(s) - save a few more in the editor first")
    steps = int(os.environ.get("WALLFLOW_TRAIN_STEPS") or min(3000, max(800, 30 * len(data))))
    val_iou = None
    if n_img >= 10:
        held = {g for g in set(groups) if g % 5 == 4}
        tr = [k for k, g in enumerate(groups) if g not in held]
        va = [k for k, g in enumerate(groups) if g in held and k % 2 == 0]     # unmirrored only
        head = _fit(data, tr, steps, sam.dev, torch, "test run")
        val_iou = round(_iou(head, data, va, sam.dev, torch), 3)
        status(f"on {len(held)} unseen wallpaper(s) it matches your masks {val_iou * 100:.0f}% (IoU)")
    head = _fit(data, list(range(len(data))), steps, sam.dev, torch, "training")
    os.makedirs(os.path.dirname(a["out"]), exist_ok=True)
    tmp = a["out"] + ".part"
    torch.save({"version": HEAD_VERSION, "state": head.state_dict(), "model": a.get("model"),
                "images": n_img}, tmp)
    os.replace(tmp, a["out"])
    return {"images": n_img, "val_iou": val_iou, "steps": steps, "device": sam.dev.upper(),
            "seconds": round(time.monotonic() - t0, 1)}


def auto(a: dict) -> int:
    """Automatic cutout with the learned model (depth.mode = "learned"). Exit 3 = nothing in front."""
    sam = Sam(a.get("model") or "facebook/sam2.1-hiera-large")
    img = load_image(a["src"])
    small = np.asarray(img.resize(work_size(*img.size), Image.LANCZOS))
    sam.set_image(small)
    m = learned_mask(sam, small, a)
    if m is None:
        say("error: no learned model - run `wallflow depth train`")
        return 1
    if m.sum() < 0.005 * m.size:
        return 3
    res = render_cutout(a["src"], m, a["out"])
    if res.get("empty"):
        return 3
    say(f"learned: {res['coverage'] * 100:.0f}% of the image in front")
    return 0


# --- entry points ------------------------------------------------------------------------------

def serve(a: dict) -> None:
    model = a.get("model") or "facebook/sam2.1-hiera-large"
    s = Session(lambda: Sam(model))
    handlers = {name: getattr(s, name) for name in
                ("open", "pick", "cycle", "refine", "lasso", "below", "set", "undo", "apply")}
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            say("error: bad request")
            continue
        cmd = req.get("cmd")
        if cmd == "quit":
            return
        fn = handlers.get(cmd)
        if fn is None:
            say(f"error: unknown command {cmd!r}")
            continue
        if cmd != "open" and s.src is None:
            say("error: no image open")
            continue
        try:
            res = fn(req.get("args") or {})
            say("done: " + json.dumps({"id": req.get("id"), "cmd": cmd, **res}))
        except ModuleNotFoundError as e:
            say(f"error: missing module {e.name} in the depth venv - run `wallflow depth setup` once more "
                "(it installs the editor's packages: torch + SAM 2.1)")
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            say(f"error: {type(e).__name__}: {e}")


def render(a: dict) -> dict:
    """Saved masks -> cutouts, no SAM (import). Reports per item, keeps going on errors."""
    ok = 0
    for i, j in enumerate(a.get("jobs", []), 1):
        try:
            m = load_alpha(j["mask"], *Image.open(j["mask"]).size, alpha=False)
            res = render_cutout(j["src"], m, j["out"])
            say("item: " + json.dumps({"i": i, "ok": True, **res}))
            ok += 1
        except Exception as e:
            say("item: " + json.dumps({"i": i, "ok": False, "error": f"{type(e).__name__}: {e}"}))
    return {"ok": ok}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        if mode == "serve":
            serve(args)
            sys.exit(0)
        if mode == "render":
            say("done: " + json.dumps(render(args)))
            sys.exit(0)
        if mode == "train":
            say("done: " + json.dumps(train(args)))
            sys.exit(0)
        if mode == "auto":
            sys.exit(auto(args))
        say(f"error: unknown mode {mode!r}")
        sys.exit(2)
    except ModuleNotFoundError as e:
        say(f"error: missing module {e.name} in the depth venv - run `wallflow depth setup` once more")
        sys.exit(2)
    except Exception as e:
        traceback.print_exc(file=sys.stdout)
        say(f"error: {type(e).__name__}: {e}")
        sys.exit(1)
