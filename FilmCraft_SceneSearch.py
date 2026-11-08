"""FilmCraft_Scene_Search.py - find a scene in a video by describing it.

How it works
  1. INDEX  - the video is sampled (default 1 frame per second) and every frame is turned
              into a CLIP embedding. This is done once per video and cached on disk.
  2. SEARCH - type a description ("a person singing on a stage at night") or double-click
              items in the feature list. The best matching moments come back with
              timestamps and thumbnails.
  3. TAG    - "Tag the video" scores every feature in the list against the whole video and
              shows which features really stand out, and where.
  Results can be opened in a player at that time, exported (SRT / CSV), or sent to
  FilmCraft as captions with captions.import.

One-time setup, in the Anaconda (base) window that FILMCRAFT.bat opens:

    pip install torch transformers pillow av numpy

(av, numpy and Pillow normally come with whisper-ctranslate2 / Anaconda. A plain
"pip install torch" on Windows is CPU-only; for an NVIDIA GPU install the CUDA build from
https://pytorch.org/get-started/locally/ instead. The CLIP model downloads on first use.)

Run it from the FILMCRAFT.bat window if you want to send results to FilmCraft (it needs
FILMCRAFT_CONTROL_TOKEN):

    python FilmCraft_Scene_Search.py
"""
import csv
import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "FilmCraft_Scene_Search.json"
CACHE_DIR = HERE / "video_search_cache"
OUT_DIR = HERE / "video_search_results"
THUMB_W = 384
BATCH = 32
CHECK_ON, CHECK_OFF = "\u2611", "\u2610"
MODELS = ("openai/clip-vit-base-patch32", "openai/clip-vit-base-patch16", "openai/clip-vit-large-patch14")
TEMPLATES = ("a photo of {}", "a movie still of {}", "a video frame of {}", "{}")
FITS = ("Whole frame", "Center crop")
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
VIDEO_TYPES = "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mpg *.mpeg *.wmv *.ts"

# The built-in list of searchable features. Double-click one to add it to the query.
# Your own entries (the "My features" group) are saved in FilmCraft_Scene_Search.json.
FEATURES = {
    "Shot type": [
        "a close-up of a face", "a wide establishing shot", "an aerial drone shot", "a silhouette",
        "an over-the-shoulder shot", "a title card with text", "a black screen",
        "a blurry out-of-focus shot", "a low angle shot", "a split screen"],
    "Setting": [
        "indoors", "outdoors", "a city street", "a city skyline", "a forest", "a beach", "mountains",
        "a desert", "snow", "a kitchen", "a bedroom", "an office", "a stage", "a recording studio",
        "inside a car", "a classroom", "a stadium", "a church", "a bar", "a nightclub",
        "a swimming pool", "a garden", "a rooftop", "a highway", "a train station", "an airport"],
    "Time and light": [
        "daytime", "night", "sunset", "sunrise", "neon lights", "candlelight", "backlit", "fog",
        "rain", "a thunderstorm", "golden hour", "harsh sunlight", "dark and moody",
        "bright and colorful"],
    "People": [
        "one person", "two people", "a group of people", "a crowd", "a child", "a baby",
        "an elderly person", "a smiling person", "a crying person", "a person running",
        "a person walking", "people dancing", "a person singing into a microphone",
        "a person playing guitar", "a person playing drums", "a person playing piano",
        "a person talking to the camera", "a couple kissing", "people hugging", "people fighting",
        "a person sleeping", "a person driving", "a person on a phone", "a person reading"],
    "Objects": [
        "a car", "a motorcycle", "a bicycle", "an airplane", "a boat", "a train", "a bus", "a horse",
        "a dog", "a cat", "a bird", "a phone", "a laptop", "a television", "a guitar", "a microphone",
        "a camera", "flowers", "food on a table", "a glass of drink", "a book", "a mirror", "a window",
        "a door", "a staircase", "a bridge", "a clock", "a gun", "a knife"],
    "Action and events": [
        "an explosion", "fire", "smoke", "a car chase", "a car crash", "a fight", "a wedding", "a party",
        "a concert", "a sports match", "a graduation", "a funeral", "a protest", "fireworks",
        "a dance performance", "a meal", "a celebration", "a storm", "a flood"],
    "Color and style": [
        "mostly red", "mostly blue", "mostly green", "mostly yellow", "black and white",
        "vibrant colors", "muted colors", "high contrast", "a cartoon or animation", "text overlay",
        "a screen recording", "a logo"],
}
MY_FEATURES = "My features"

DEFAULTS = {
    "video": "", "model": MODELS[1], "device": "auto", "interval": "1.0", "fit": FITS[0],
    "template": TEMPLATES[0], "top_n": "15", "min_gap": "8",
    "host": "127.0.0.1", "port": os.environ.get("FILMCRAFT_CONTROL_PORT", "9876"),
    "shift": "0", "replace": False, "custom": [],
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def fmt_time(t):
    t = max(0, int(round(t)))
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _ts(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s_, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s_:02d},{ms:03d}"


def apply_template(template, text):
    return (template or "{}").replace("{}", text)


def cache_key(video, model, interval, fit):
    p = Path(video)
    st = p.stat()
    raw = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}|{model}|{interval}|{fit}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fc_call(host, port, token, method, params=None, timeout=120):
    req = {"id": 1, "token": token, "method": method}
    if params is not None:
        req["params"] = params
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.settimeout(timeout)
        fh = sock.makefile("rw", encoding="utf-8", newline="\n")
        fh.write(json.dumps(req) + "\n")
        fh.flush()
        line = fh.readline()
    if not line:
        raise ConnectionError("FilmCraft closed the connection without a reply")
    return json.loads(line)


def open_video_at(video, t):
    """Open the video in VLC / mpv at time t (seconds). Returns the player name or None."""
    for exe in (shutil.which("vlc"), r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"):
        if exe and Path(exe).exists():
            subprocess.Popen([exe, f"--start-time={t:.2f}", str(video)])
            return "VLC"
    mpv = shutil.which("mpv")
    if mpv:
        subprocess.Popen([mpv, f"--start={t:.2f}", str(video)])
        return "mpv"
    if hasattr(os, "startfile"):
        os.startfile(str(video))  # opens the default player, but cannot seek
    return None


# --------------------------------------------------------------------------
# CLIP
# --------------------------------------------------------------------------
class ClipEmbedder:
    """Turns images and text into unit-length CLIP vectors (Hugging Face CLIP models)."""

    def __init__(self, name, device="auto", fit="Whole frame", log=print):
        import torch
        from transformers import AutoTokenizer, CLIPModel
        self.torch = torch
        has_cuda = torch.cuda.is_available()
        if device == "cuda" and not has_cuda:
            log("This PyTorch has no CUDA support, so the CPU is used (slower). "
                "Install a CUDA build of PyTorch for the GPU.")
        self.dev = "cuda" if (device in ("auto", "cuda") and has_cuda) else "cpu"
        log(f"Loading {name} on {self.dev} (the first use downloads the model) ...")
        self.model = CLIPModel.from_pretrained(name).eval().to(self.dev)
        if self.dev == "cuda":
            self.model.half()
        self.tok = AutoTokenizer.from_pretrained(name)
        self.size = int(getattr(self.model.config.vision_config, "image_size", 224))
        self.fit = fit
        self.mean = torch.tensor(CLIP_MEAN).view(1, 3, 1, 1)
        self.std = torch.tensor(CLIP_STD).view(1, 3, 1, 1)

    def _tensor(self, out, kind):
        """Pooled, projected (batch, dim) features from a tensor or a newer-transformers output object."""
        if hasattr(out, "dim"):
            f = out
        else:
            f = getattr(out, "image_embeds" if kind == "image" else "text_embeds", None)
            if f is None:
                f = getattr(out, "pooler_output", None)
            if f is None:
                f = out[0]
        want = int(self.model.config.projection_dim)
        if f.dim() == 3:  # per-token states -> take the first (image CLS) / EOS token (text)
            f = f[:, 0] if kind == "image" else f[:, -1]
        if f.shape[-1] != want:  # not projected yet
            proj = self.model.visual_projection if kind == "image" else self.model.text_projection
            f = proj(f)
        return f

    def _prep(self, im):
        from PIL import Image
        im = im.convert("RGB")
        if self.fit == "Center crop":
            w, h = im.size
            s = min(w, h)
            x0, y0 = (w - s) // 2, (h - s) // 2
            im = im.crop((x0, y0, x0 + s, y0 + s))
        return im.resize((self.size, self.size), Image.BICUBIC)

    def embed_images(self, images):
        import numpy as np
        torch = self.torch
        arr = np.stack([np.asarray(self._prep(im), dtype=np.float32) / 255.0 for im in images])
        x = (torch.from_numpy(arr).permute(0, 3, 1, 2) - self.mean) / self.std
        x = x.to(self.dev)
        if self.dev == "cuda":
            x = x.half()
        with torch.no_grad():
            f = self._tensor(self.model.get_image_features(pixel_values=x), "image").float()
        return (f / f.norm(dim=-1, keepdim=True)).cpu().numpy()

    def embed_texts(self, texts):
        torch = self.torch
        enc = self.tok(list(texts), padding=True, truncation=True, max_length=77, return_tensors="pt")
        enc = {k: v.to(self.dev) for k, v in enc.items() if k in ("input_ids", "attention_mask")}
        with torch.no_grad():
            f = self._tensor(self.model.get_text_features(**enc), "text").float()
        return (f / f.norm(dim=-1, keepdim=True)).cpu().numpy()


def make_embedder(name, device, fit, log):
    return ClipEmbedder(name, device, fit, log)


# --------------------------------------------------------------------------
# Indexing, searching, tagging (no GUI in here)
# --------------------------------------------------------------------------
def build_index(video, embedder, interval, thumb_dir, on_progress, stop):
    """Sample the video every `interval` seconds -> (times, embeddings). None if stopped."""
    import av
    import numpy as np
    thumb_dir.mkdir(parents=True, exist_ok=True)
    times, chunks, batch = [], [], []

    def flush():
        if batch:
            chunks.append(np.asarray(embedder.embed_images(batch), dtype=np.float32))
            batch.clear()

    with av.open(str(video)) as c:
        streams = [s for s in c.streams if s.type == "video"]
        if not streams:
            raise RuntimeError("There is no video stream in this file.")
        s = streams[0]
        s.thread_type = "AUTO"
        fps = float(s.average_rate or 25)
        duration = float(c.duration) / 1_000_000 if c.duration else 0.0
        expected = max(1, int(duration / interval) + 1) if duration else 0
        next_t, frame_no = 0.0, 0
        for frame in c.decode(s):
            if stop.is_set():
                return None
            t = frame.time if frame.time is not None else frame_no / fps
            frame_no += 1
            if t + 1e-6 < next_t:
                continue
            while next_t <= t + 1e-6:
                next_t += interval
            img = frame.to_image().convert("RGB")
            th = img.copy()
            th.thumbnail((THUMB_W, THUMB_W))
            th.save(thumb_dir / f"{len(times):06d}.jpg", quality=80)
            times.append(float(t))
            batch.append(img)
            if len(batch) >= BATCH:
                flush()
            on_progress(len(times), expected, t)
        flush()
    if not times:
        raise RuntimeError("No frames could be read from this file.")
    return np.asarray(times, dtype=np.float64), np.concatenate(chunks, axis=0)


def pick_moments(sims, times, top_n, min_gap, step):
    """Best matches, at least min_gap seconds apart, each with the stretch that is as good."""
    import numpy as np
    out, taken = [], []
    for i in np.argsort(-sims):
        if len(out) >= top_n:
            break
        i = int(i)
        ti = float(times[i])
        if any(abs(ti - times[j]) < min_gap for j in taken):
            continue
        taken.append(i)
        thr = sims[i] - 0.02
        lo = hi = i
        while lo > 0 and sims[lo - 1] >= thr and ti - times[lo - 1] <= min_gap:
            lo -= 1
        while hi < len(sims) - 1 and sims[hi + 1] >= thr and times[hi + 1] - ti <= min_gap:
            hi += 1
        out.append({"idx": i, "t": ti, "start": float(times[lo]), "end": float(times[hi] + step),
                    "score": float(sims[i])})
    return out


def tag_features(frames, times, texts, names, step):
    """Which features stand out? Strength = how far the best frame is above that feature's
    average over the whole video (in standard deviations), so features that are present
    all the time, or never, score low."""
    import numpy as np
    S = frames.astype(np.float32) @ texts.astype(np.float32).T  # frames x features
    peak_idx, peak = S.argmax(0), S.max(0)
    z = (peak - S.mean(0)) / (S.std(0) + 1e-6)
    out = []
    for k, name in enumerate(names):
        i = int(peak_idx[k])
        out.append({"idx": i, "t": float(times[i]), "start": float(times[i]),
                    "end": float(times[i] + step), "score": float(z[k]), "cos": float(peak[k]),
                    "label": name})
    out.sort(key=lambda r: -r["score"])
    return out


def results_to_srt(results, mode):
    """One caption per hit (so FilmCraft shows where the hits are)."""
    lines = []
    for n, r in enumerate(sorted(results, key=lambda r: r["start"]), 1):
        end = max(r["end"], r["start"] + 1.0)
        text = f"{n}. {r['label']}" if mode == "search" else r["label"]
        lines.append(f"{n}\n{_ts(r['start'])} --> {_ts(end)}\n{text}\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FilmCraft - Scene Search")
        self.geometry("1180x940")
        self.cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except Exception:
                pass

        self.log_q = queue.Queue()
        self.busy = False
        self.stop = threading.Event()
        self.embedder, self.embedder_key = None, None
        self.video = None
        self.times = None
        self.embs = None
        self.thumb_dir = None
        self.results, self.mode = [], "search"
        self._row_imgs, self._prev_img = [], None
        self.custom = list(self.cfg["custom"])

        c = self.cfg
        self.v_video = tk.StringVar(value=c["video"])
        self.v_model = tk.StringVar(value=c["model"])
        self.v_device = tk.StringVar(value=c["device"])
        self.v_interval = tk.StringVar(value=str(c["interval"]))
        self.v_fit = tk.StringVar(value=c["fit"])
        self.v_query = tk.StringVar()
        self.v_template = tk.StringVar(value=c["template"])
        self.v_topn = tk.StringVar(value=str(c["top_n"]))
        self.v_gap = tk.StringVar(value=str(c["min_gap"]))
        self.v_filter = tk.StringVar()
        self.v_host = tk.StringVar(value=c["host"])
        self.v_port = tk.StringVar(value=str(c["port"]))
        self.v_token = tk.StringVar(value=os.environ.get("FILMCRAFT_CONTROL_TOKEN", ""))
        self.v_shift = tk.StringVar(value=str(c["shift"]))
        self.v_replace = tk.BooleanVar(value=bool(c["replace"]))
        self.v_status = tk.StringVar(value="Choose a video and click 'Index video'.")
        self.v_prog = tk.DoubleVar(value=0.0)
        self.v_info = tk.StringVar(value="")

        self._build()
        self._fill_features()
        self.v_filter.trace_add("write", lambda *_: self._fill_features())
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._check_packages()

    # ---- layout ----------------------------------------------------------
    def _build(self):
        pad = {"padx": 5, "pady": 3}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # 1. video + index
        top = ttk.LabelFrame(root, text="1. Video and index", padding=6)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Video").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.v_video).grid(row=0, column=1, columnspan=7, sticky="ew", **pad)
        ttk.Button(top, text="Browse...", command=self._pick_video).grid(row=0, column=8, **pad)
        opts = ttk.Frame(top)
        opts.grid(row=1, column=0, columnspan=9, sticky="w")
        ttk.Label(opts, text="Model").pack(side="left", padx=(5, 2))
        ttk.Combobox(opts, textvariable=self.v_model, values=MODELS, width=28).pack(side="left")
        ttk.Label(opts, text="Device").pack(side="left", padx=(10, 2))
        ttk.Combobox(opts, textvariable=self.v_device, values=("auto", "cuda", "cpu"), width=6,
                     state="readonly").pack(side="left")
        ttk.Label(opts, text="One frame every (s)").pack(side="left", padx=(10, 2))
        ttk.Entry(opts, textvariable=self.v_interval, width=5).pack(side="left")
        ttk.Label(opts, text="Frame fit").pack(side="left", padx=(10, 2))
        ttk.Combobox(opts, textvariable=self.v_fit, values=FITS, width=12, state="readonly").pack(side="left")
        self.btn_index = ttk.Button(opts, text="Index video", command=self.on_index)
        self.btn_index.pack(side="left", padx=(14, 4))
        self.btn_stop = ttk.Button(opts, text="Stop", command=self.stop.set, state="disabled")
        self.btn_stop.pack(side="left")
        prog = ttk.Frame(top)
        prog.grid(row=2, column=0, columnspan=9, sticky="ew")
        prog.columnconfigure(1, weight=1)
        ttk.Label(prog, textvariable=self.v_status, width=46, anchor="w").grid(row=0, column=0, padx=5)
        ttk.Progressbar(prog, variable=self.v_prog, maximum=100).grid(row=0, column=1, sticky="ew", padx=5)

        # middle: features | search + results
        pw = ttk.PanedWindow(root, orient="horizontal")
        pw.grid(row=1, column=0, sticky="nsew", pady=6)

        left = ttk.LabelFrame(pw, text="Searchable features (double-click to add to the query)", padding=6)
        pw.add(left, weight=1)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        ff = ttk.Frame(left)
        ff.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(ff, text="Filter").pack(side="left", padx=(0, 4))
        ttk.Entry(ff, textvariable=self.v_filter).pack(side="left", fill="x", expand=True)
        self.ftree = ttk.Treeview(left, show="tree", selectmode="extended")
        fsb = ttk.Scrollbar(left, command=self.ftree.yview)
        self.ftree.configure(yscrollcommand=fsb.set)
        self.ftree.grid(row=1, column=0, sticky="nsew", pady=4)
        fsb.grid(row=1, column=1, sticky="ns", pady=4)
        self.ftree.bind("<Double-1>", lambda e: self.on_add_feature())
        fb = ttk.Frame(left)
        fb.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(fb, text="Add to query", command=self.on_add_feature).pack(side="left", padx=2)
        self.btn_tag = ttk.Button(fb, text="Tag the video", command=self.on_tag)
        self.btn_tag.pack(side="left", padx=2)
        ttk.Button(fb, text="Remove from my features", command=self.on_remove_custom).pack(side="left", padx=2)

        right = ttk.LabelFrame(pw, text="2. Search", padding=6)
        pw.add(right, weight=3)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        qf = ttk.Frame(right)
        qf.grid(row=0, column=0, sticky="ew")
        qf.columnconfigure(1, weight=1)
        ttk.Label(qf, text="Describe the scene").grid(row=0, column=0, **pad)
        qe = ttk.Entry(qf, textvariable=self.v_query)
        qe.grid(row=0, column=1, sticky="ew", **pad)
        qe.bind("<Return>", lambda e: self.on_search())
        self.btn_search = ttk.Button(qf, text="Search", command=self.on_search)
        self.btn_search.grid(row=0, column=2, **pad)
        ttk.Button(qf, text="Add query to my features", command=self.on_add_custom).grid(row=0, column=3, **pad)
        sf = ttk.Frame(right)
        sf.grid(row=1, column=0, sticky="w")
        ttk.Label(sf, text="Wording").pack(side="left", padx=(5, 2))
        ttk.Combobox(sf, textvariable=self.v_template, values=TEMPLATES, width=22).pack(side="left")
        ttk.Label(sf, text="Results").pack(side="left", padx=(10, 2))
        ttk.Entry(sf, textvariable=self.v_topn, width=4).pack(side="left")
        ttk.Label(sf, text="Keep hits apart by (s)").pack(side="left", padx=(10, 2))
        ttk.Entry(sf, textvariable=self.v_gap, width=4).pack(side="left")

        style = ttk.Style(self)
        style.configure("Thumb.Treeview", rowheight=66)
        rf = ttk.Frame(right)
        rf.grid(row=2, column=0, sticky="nsew", pady=4)
        rf.rowconfigure(0, weight=1)
        rf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(rf, style="Thumb.Treeview", columns=("time", "pick", "score", "label"),
                                 show="tree headings", selectmode="browse")
        self.tree.column("#0", width=130, stretch=False)
        self.tree.column("time", width=120, stretch=False)
        self.tree.column("pick", width=50, stretch=False, anchor="center")
        self.tree.column("score", width=80, stretch=False, anchor="e")
        self.tree.column("label", width=360)
        self.tree.heading("#0", text="Frame")
        self.checked, self._sort_col, self._sort_rev = set(), "score", True
        self._heads = {"time": "Time", "pick": "Use", "score": "Score", "label": "Match"}
        self._refresh_heads()
        tsb = ttk.Scrollbar(rf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb.grid(row=0, column=1, sticky="ns")
        pk = ttk.Frame(rf)
        pk.grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Button(pk, text="Select all", command=lambda: self._set_checked("all")).pack(side="left", padx=2)
        ttk.Button(pk, text="Select none", command=lambda: self._set_checked("none")).pack(side="left", padx=2)
        ttk.Button(pk, text="Invert", command=lambda: self._set_checked("invert")).pack(side="left", padx=2)
        self.v_picked = tk.StringVar(value="")
        ttk.Label(pk, textvariable=self.v_picked).pack(side="left", padx=10)
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self.on_open())

        pv = ttk.Frame(right)
        pv.grid(row=3, column=0, sticky="ew")
        self.lbl_prev = ttk.Label(pv, text="(select a result to preview it)", anchor="center", width=48)
        self.lbl_prev.pack(side="left", padx=5)
        pb = ttk.Frame(pv)
        pb.pack(side="left", padx=10, anchor="n")
        ttk.Label(pb, textvariable=self.v_info, wraplength=360, justify="left").pack(anchor="w", pady=(0, 6))
        ttk.Button(pb, text="Open video at this time", command=self.on_open).pack(anchor="w", pady=2)
        ttk.Button(pb, text="Copy timestamp", command=self.on_copy).pack(anchor="w", pady=2)

        # 3. export / FilmCraft
        bot = ttk.LabelFrame(root, text="3. Use the results", padding=6)
        bot.grid(row=2, column=0, sticky="ew")
        ttk.Button(bot, text="Export SRT", command=self.on_export_srt).pack(side="left", padx=4)
        ttk.Button(bot, text="Export CSV", command=self.on_export_csv).pack(side="left", padx=4)
        ttk.Separator(bot, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(bot, text="FilmCraft  host").pack(side="left")
        ttk.Entry(bot, textvariable=self.v_host, width=11).pack(side="left", padx=2)
        ttk.Label(bot, text="port").pack(side="left")
        ttk.Entry(bot, textvariable=self.v_port, width=6).pack(side="left", padx=2)
        ttk.Label(bot, text="token").pack(side="left")
        ttk.Entry(bot, textvariable=self.v_token, show="*", width=14).pack(side="left", padx=2)
        ttk.Label(bot, text="shift (s)").pack(side="left", padx=(6, 0))
        ttk.Entry(bot, textvariable=self.v_shift, width=5).pack(side="left", padx=2)
        ttk.Checkbutton(bot, text="Replace C1 first", variable=self.v_replace).pack(side="left", padx=6)
        ttk.Button(bot, text="Send results to FilmCraft as captions", command=self.on_send).pack(side="left", padx=4)

        logf = ttk.Frame(root)
        logf.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        logf.columnconfigure(0, weight=1)
        self.txt = tk.Text(logf, wrap="word", height=6, state="disabled")
        sb = ttk.Scrollbar(logf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="ew")
        sb.grid(row=0, column=1, sticky="ns")

    # ---- helpers ---------------------------------------------------------
    def log(self, msg):
        self.log_q.put(str(msg))

    def status(self, msg, pct=None):
        def apply():
            self.v_status.set(msg)
            if pct is not None:
                self.v_prog.set(pct)
        self.after(0, apply)

    def _drain_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", msg + "\n")
                self.txt.see("end")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def _check_packages(self):
        missing = []
        for mod, pipname in (("PIL", "pillow"), ("av", "av"), ("numpy", "numpy"),
                             ("transformers", "transformers"), ("torch", "torch")):
            try:
                __import__(mod)
            except ImportError:
                missing.append(pipname)
        if missing:
            self.log("Missing Python packages: " + ", ".join(missing)
                     + "\nInstall with:  pip install " + " ".join(missing))

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_index, self.btn_search, self.btn_tag):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _save_cfg(self):
        data = {
            "video": self.v_video.get(), "model": self.v_model.get(), "device": self.v_device.get(),
            "interval": self.v_interval.get(), "fit": self.v_fit.get(), "template": self.v_template.get(),
            "top_n": self.v_topn.get(), "min_gap": self.v_gap.get(), "host": self.v_host.get(),
            "port": self.v_port.get(), "shift": self.v_shift.get(), "replace": self.v_replace.get(),
            "custom": self.custom,
        }  # the token is never written to disk
        try:
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        self._save_cfg()
        self.destroy()

    def _pick_video(self):
        p = filedialog.askopenfilename(title="Choose a video",
                                       filetypes=[("Video", VIDEO_TYPES), ("All files", "*.*")])
        if p:
            self.v_video.set(os.path.normpath(p))

    def _opts(self):
        """Read every setting on the main thread; worker threads use this copy."""
        return {
            "video": self.v_video.get().strip(), "model": self.v_model.get().strip() or MODELS[1],
            "device": self.v_device.get().strip() or "auto", "interval": self.v_interval.get().strip(),
            "fit": self.v_fit.get(), "query": self.v_query.get().strip(),
            "template": self.v_template.get(), "top_n": self.v_topn.get().strip(),
            "min_gap": self.v_gap.get().strip(), "host": self.v_host.get().strip(),
            "port": self.v_port.get().strip(), "token": self.v_token.get().strip(),
            "shift": self.v_shift.get().strip(), "replace": self.v_replace.get(),
        }

    def _run(self, fn, *args):
        if self.busy:
            return
        self._save_cfg()
        self.stop.clear()
        self._set_busy(True)

        def runner():
            try:
                fn(*args)
            except ImportError as exc:
                self.log(f"A needed package is missing: {exc}. In the Anaconda window run:  "
                         "pip install torch transformers pillow av numpy")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.after(0, lambda: self._set_busy(False))
        threading.Thread(target=runner, daemon=True).start()

    # ---- feature list ----------------------------------------------------
    def _all_features(self):
        groups = dict(FEATURES)
        if self.custom:
            groups[MY_FEATURES] = list(self.custom)
        return groups

    def _fill_features(self):
        flt = self.v_filter.get().strip().lower()
        self.ftree.delete(*self.ftree.get_children())
        for cat, items in self._all_features().items():
            shown = [f for f in items if not flt or flt in f.lower() or flt in cat.lower()]
            if not shown:
                continue
            parent = self.ftree.insert("", "end", iid=f"cat:{cat}", text=f"{cat} ({len(shown)})",
                                       open=bool(flt) or cat == MY_FEATURES)
            for f in shown:
                self.ftree.insert(parent, "end", iid=f"{cat}|{f}", text=f)

    def on_add_feature(self):
        picked = [i.split("|", 1)[1] for i in self.ftree.selection() if "|" in i]
        if not picked:
            return
        cur = self.v_query.get().strip()
        self.v_query.set((cur + ", " if cur else "") + ", ".join(picked))

    def on_add_custom(self):
        q = self.v_query.get().strip()
        if q and q not in self.custom:
            self.custom.append(q)
            self._fill_features()
            self.log(f"Added to My features: {q}")

    def on_remove_custom(self):
        gone = [i.split("|", 1)[1] for i in self.ftree.selection() if i.startswith(MY_FEATURES + "|")]
        if gone:
            self.custom = [f for f in self.custom if f not in gone]
            self._fill_features()

    # ---- indexing --------------------------------------------------------
    def _get_embedder(self, o):
        key = (o["model"], o["device"], o["fit"])
        if self.embedder is None or self.embedder_key != key:
            self.status("Loading the CLIP model ...")
            self.embedder = make_embedder(o["model"], o["device"], o["fit"], self.log)
            self.embedder_key = key
        return self.embedder

    def on_index(self):
        o = self._opts()
        try:
            o["interval_f"] = float(o["interval"] or 1.0)
            if o["interval_f"] <= 0:
                raise ValueError
        except ValueError:
            self.log("'One frame every (s)' must be a positive number.")
            return
        if not o["video"] or not Path(o["video"]).is_file():
            self.log("Choose an existing video file first.")
            return
        self._run(self._index_work, o)

    def _index_work(self, o):
        import numpy as np
        video = Path(o["video"])
        d = CACHE_DIR / cache_key(video, o["model"], o["interval_f"], o["fit"])
        if (d / "meta.json").exists():
            self.times = np.load(d / "times.npy")
            self.embs = np.load(d / "emb.npy").astype(np.float32)
            self.thumb_dir, self.video = d / "thumbs", video
            self.log(f"Loaded the saved index: {len(self.times)} frames. (Delete {d} to rebuild.)")
            self.status(f"Ready: {len(self.times)} frames indexed (from the saved index).", 100)
            return
        emb = self._get_embedder(o)
        started = time.time()

        def progress(n, expected, t):
            pct = (100.0 * n / expected) if expected else 0.0
            self.status(f"Indexing ... {n} frames, at {fmt_time(t)}", min(99.0, pct))

        res = build_index(video, emb, o["interval_f"], d / "thumbs", progress, self.stop)
        if res is None:
            shutil.rmtree(d, ignore_errors=True)
            self.status("Stopped. Nothing was saved.", 0)
            self.log("Indexing stopped.")
            return
        times, embs = res
        np.save(d / "times.npy", times)
        np.save(d / "emb.npy", embs.astype(np.float16))
        (d / "meta.json").write_text(json.dumps({"video": str(video), "model": o["model"],
                                                 "interval": o["interval_f"], "fit": o["fit"],
                                                 "frames": int(len(times))}), encoding="utf-8")
        self.times, self.embs, self.thumb_dir, self.video = times, embs, d / "thumbs", video
        self.log(f"Indexed {len(times)} frames in {time.time() - started:0.0f} s.")
        self.status(f"Ready: {len(times)} frames indexed.", 100)

    # ---- search / tag ----------------------------------------------------
    def _step(self):
        return float(self.times[1] - self.times[0]) if len(self.times) > 1 else 1.0

    def _need_index(self):
        if self.embs is None or self.times is None:
            self.log("Index the video first (button at the top).")
            return False
        return True

    def on_search(self):
        o = self._opts()
        if not o["query"]:
            self.log("Type a description first, or double-click a feature.")
            return
        if not self._need_index():
            return
        try:
            o["top_n_i"] = max(1, int(float(o["top_n"] or 15)))
            o["gap_f"] = max(0.0, float(o["min_gap"] or 8))
        except ValueError:
            self.log("'Results' and 'Keep hits apart' must be numbers.")
            return
        self._run(self._search_work, o)

    def _search_work(self, o):
        emb = self._get_embedder(o)
        vec = emb.embed_texts([apply_template(o["template"], o["query"])])[0]
        res = pick_moments(self.embs @ vec, self.times, o["top_n_i"], o["gap_f"], self._step())
        for r in res:
            r["label"] = o["query"]
        self.after(0, lambda: self._show_results(res, "search"))
        self.log(f"{len(res)} moments found for: {o['query']}" if res else "Nothing found.")

    def on_tag(self):
        o = self._opts()
        if not self._need_index():
            return
        self._run(self._tag_work, o)

    def _tag_work(self, o):
        emb = self._get_embedder(o)
        names = [f for items in self._all_features().values() for f in items]
        self.status(f"Scoring {len(names)} features ...")
        texts = emb.embed_texts([apply_template(o["template"], n) for n in names])
        res = tag_features(self.embs, self.times, texts, names, self._step())[:60]
        self.after(0, lambda: self._show_results(res, "tag"))
        self.status(f"Ready: {len(self.times)} frames indexed.", 100)
        self.log("Tagged. The list shows the features that stand out most in this video, with the "
                 "moment where each is strongest.")

    # ---- results ---------------------------------------------------------
    def _thumb_photo(self, idx, size):
        from PIL import Image, ImageTk
        try:
            im = Image.open(self.thumb_dir / f"{idx:06d}.jpg")
            im.thumbnail(size)
            return ImageTk.PhotoImage(im)
        except Exception:
            return None

    def _show_results(self, results, mode):
        self.results, self.mode = results, mode
        self.tree.delete(*self.tree.get_children())
        self._row_imgs = []
        heads = ("Time", "Score", "Match") if mode == "search" else ("Peak at", "Strength", "Feature")
        for col, h in zip(("time", "score", "label"), heads):
            self._heads[col] = h
        self.checked = set(str(k) for k in range(len(results)))
        self._sort_col, self._sort_rev = "score", True
        self._refresh_heads()
        for k, r in enumerate(results):
            img = self._thumb_photo(r["idx"], (120, 64))
            self._row_imgs.append(img)
            if mode == "search":
                when = f"{fmt_time(r['start'])} - {fmt_time(r['end'])}"
                score = f"{r['score'] * 100:0.1f}"
            else:
                when, score = fmt_time(r["t"]), f"{r['score']:0.1f}"
            self.tree.insert("", "end", iid=str(k), image=img or "",
                             values=(when, CHECK_ON, score, r["label"]))
        self._sort_rows()
        self._update_picked()
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])

    # ---- sorting and ticking ---------------------------------------------
    def _refresh_heads(self):
        for col, h in self._heads.items():
            arrow = (" \u25bc" if self._sort_rev else " \u25b2") if col == self._sort_col else ""
            self.tree.heading(col, text=h + arrow, command=lambda c=col: self._on_head(c))

    def _on_head(self, col):
        if col == self._sort_col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col, self._sort_rev = col, col in ("score", "pick")
        self._refresh_heads()
        self._sort_rows()

    def _sort_rows(self):
        col = self._sort_col
        key = {"time": lambda k: self.results[k]["t"],
               "score": lambda k: self.results[k]["score"],
               "label": lambda k: self.results[k]["label"].lower(),
               "pick": lambda k: (str(k) in self.checked, self.results[k]["score"])}[col]
        order = sorted(range(len(self.results)), key=key, reverse=self._sort_rev)
        for pos, k in enumerate(order):
            self.tree.move(str(k), "", pos)
        sel = self.tree.selection()
        if sel:
            self.tree.see(sel[0])

    def _set_row_check(self, iid, on):
        (self.checked.add if on else self.checked.discard)(iid)
        self.tree.set(iid, "pick", CHECK_ON if on else CHECK_OFF)

    def _set_checked(self, how):
        for iid in self.tree.get_children():
            on = {"all": True, "none": False, "invert": iid not in self.checked}[how]
            self._set_row_check(iid, on)
        self._update_picked()

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#2":  # the "pick" column
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._set_row_check(iid, iid not in self.checked)
            self._update_picked()

    def _on_space(self, _event=None):
        for iid in self.tree.selection():
            self._set_row_check(iid, iid not in self.checked)
        self._update_picked()
        return "break"

    def _update_picked(self):
        self.v_picked.set(f"{len(self.checked)} of {len(self.results)} ticked "
                          "(only ticked results are exported or sent)")

    def _chosen(self):
        """Ticked results, in the order shown."""
        out = [self.results[int(i)] for i in self.tree.get_children() if i in self.checked]
        if not out:
            self.log("Nothing is ticked. Tick at least one result first.")
        return out

    def _selected(self):
        sel = self.tree.selection()
        if sel:
            return self.results[int(sel[0])]
        return self.results[0] if self.results else None

    def _on_select(self, _event=None):
        r = self._selected()
        if not r:
            return
        img = self._thumb_photo(r["idx"], (THUMB_W, 216))
        if img:
            self._prev_img = img
            self.lbl_prev.configure(image=img, text="")
        if self.mode == "search":
            self.v_info.set(f"{r['label']}\nBest frame at {fmt_time(r['t'])}  "
                            f"(this stretch: {fmt_time(r['start'])} to {fmt_time(r['end'])})\n"
                            f"Score {r['score'] * 100:0.1f}  (typical good matches are 25 to 35)")
        else:
            self.v_info.set(f"{r['label']}\nStrongest at {fmt_time(r['t'])}\n"
                            f"Strength {r['score']:0.1f}  (above about 4 means it clearly stands out)")

    def on_open(self):
        r = self._selected()
        if not r or not self.video:
            self.log("Search first, then select a result.")
            return
        try:
            player = open_video_at(self.video, r["t"])
        except OSError as exc:
            self.log(f"Could not open the video: {exc}")
            return
        self.log(f"Opened in {player} at {fmt_time(r['t'])}." if player else
                 f"Opened in your default player. It cannot jump to a time, so go to {fmt_time(r['t'])}. "
                 "(VLC or mpv on the PATH can.)")

    def on_copy(self):
        r = self._selected()
        if r:
            self.clipboard_clear()
            self.clipboard_append(fmt_time(r["t"]))
            self.log(f"Copied {fmt_time(r['t'])}")

    # ---- exports / FilmCraft ---------------------------------------------
    def _result_stem(self):
        return (Path(self.video).stem if self.video else "video") + "_search"

    def on_export_srt(self):
        if not self.results:
            self.log("No results to export yet.")
            return
        chosen = self._chosen()
        if not chosen:
            return
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / (self._result_stem() + ".srt")
        path.write_text(results_to_srt(chosen, self.mode), encoding="utf-8")
        self.log(f"Saved {path}")

    def on_export_csv(self):
        if not self.results:
            self.log("No results to export yet.")
            return
        chosen = self._chosen()
        if not chosen:
            return
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / (self._result_stem() + ".csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["rank", "mode", "start_s", "end_s", "peak_s", "score", "label", "video"])
            for n, r in enumerate(chosen, 1):
                w.writerow([n, self.mode, f"{r['start']:.2f}", f"{r['end']:.2f}", f"{r['t']:.2f}",
                            f"{r['score']:.4f}", r["label"], self.video])
        self.log(f"Saved {path}")

    def on_send(self):
        if not self.results:
            self.log("No results to send yet.")
            return
        o = self._opts()
        try:
            o["shift_f"] = float(o["shift"] or 0)
        except ValueError:
            self.log("'shift' must be a number.")
            return
        results = self._chosen()
        if not results:
            return
        mode, stem = self.mode, self._result_stem()
        self._run(self._send_work, o, results, mode, stem)

    def _send_work(self, o, results, mode, stem):
        """Hits become captions (one per hit) so you can see them on the timeline."""
        def run(command, **params):
            reply = fc_call(o["host"], o["port"], o["token"], "engine.execute",
                            {"command": command, "params": params})
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or "FilmCraft reported an error")
            return reply.get("result")

        shifted = [dict(r, start=r["start"] + o["shift_f"], end=r["end"] + o["shift_f"])
                   for r in results if r["end"] + o["shift_f"] > 0]
        for r in shifted:
            r["start"] = max(0.0, r["start"])
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / (stem + ".srt")
        path.write_text(results_to_srt(shifted, mode), encoding="utf-8")
        try:
            if o["replace"]:
                try:
                    run("captions.deleteTrack", track="C1")
                    self.log("Removed the existing C1 caption track.")
                except RuntimeError as exc:
                    self.log(f"No existing C1 track to replace ({exc}).")
            res = run("captions.import", path=str(path.resolve()), name=path.stem)
            track = res.get("track", "?") if isinstance(res, dict) else "?"
            self.log(f"Sent {len(shifted)} results to FilmCraft as captions (track {track}).")
        except RuntimeError as exc:
            self.log(f"FilmCraft refused it: {exc}. Is a sequence open, and is the token right?")
        except OSError as exc:
            self.log(f"Could not reach FilmCraft on {o['host']}:{o['port']}: {exc}")


if __name__ == "__main__":
    App().mainloop()
