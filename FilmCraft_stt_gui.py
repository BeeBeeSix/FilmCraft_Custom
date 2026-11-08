"""FilmCraft Speech-to-Text bridge.

1. Transcribes an audio/video file (or every media file in a folder) to .srt with
   whisper-ctranslate2 (faster-whisper CLI), on the GPU if you choose cuda.
   Captions can be split on pauses (from word timings) instead of Whisper's own segments.
2. Sends the .srt to FilmCraft with captions.import, optionally replacing the C1 caption
   track first and shifting the captions in time.

Run it from the window opened by FILMCRAFT.bat, so that it inherits
FILMCRAFT_CONTROL_TOKEN / FILMCRAFT_CONTROL_PORT and the Anaconda (base)
environment where whisper-ctranslate2 is installed:

    python filmcraft_stt_gui.py

Standard library only (tkinter, socket, json, subprocess).
"""
import json
import os
import queue
import shlex
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, ttk
from tkinter import font as tkfont

CONFIG_FILE = Path(__file__).with_name("filmcraft_stt_gui.json")
MEDIA_EXTS = ("mp4", "mov", "mkv", "wav", "mp3", "m4a")
FONT_STYLES = ["Regular", "Medium", "SemiBold", "Bold", "Italic", "Bold Italic"]
# Starting values for the caption style panel (they mirror FilmCraft's own style controls).
STYLE_DEFAULTS = dict(font="Inter", fontStyle="SemiBold", size="54", color="#ffffff",
                      outline="2", outlineColor="#000000", background=False,
                      backgroundColor="#000000", bgOpacity="60", align="center",
                      anchor="bottom", margin="0.08", lineSpacing="1.0")
CAPTION_WORDS = ("srt", "caption", "subtitle", "transcri")

DEFAULTS = {
    "exe": "whisper-ctranslate2",
    "model": "medium",
    "device": "cuda",
    "language": "",          # empty = auto-detect
    "extra": "",             # extra whisper-ctranslate2 arguments, appended as-is
    "split": True,           # build the SRT from word timings (split on pauses)
    "pause": "0.5",          # new caption after a gap this long (seconds)
    "max_seconds": "6",      # ... or when a caption gets this long
    "max_chars": "50",       # ... or this many characters
    "vad": False,            # --vad_filter True (skip non-speech)
    "media": "",
    "outdir": "",            # empty = <media folder>\FilmCraft_Captions
    "srt": "",
    "host": "127.0.0.1",
    "port": os.environ.get("FILMCRAFT_CONTROL_PORT", "9876"),
    "import_command": "captions.import",
    # {srt} = the SRT path, {stem} = its file name without extension.
    "import_params": '{"path": "{srt}", "name": "{stem}"}',
    "replace_c1": True,      # delete caption track C1 before importing
    "shift": "0",            # move all imported captions by this many seconds (can be negative)
    "style_on": False,       # send captions.setStyle after importing, using the style panel
    "style_json": "",        # optional extra captions.setStyle fields, e.g. {"size": 48}
    "style": dict(STYLE_DEFAULTS),
}


# --------------------------------------------------------------------------
# FilmCraft control channel
# --------------------------------------------------------------------------
def fc_call(host, port, token, method, params=None, timeout=120):
    """Send one request line, return the parsed reply dict."""
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


def normalize_commands(result):
    """Turn whatever engine.commands returns into a list of dicts with an 'id'."""
    items = result
    if isinstance(result, dict):
        for key in ("commands", "items", "result"):
            if isinstance(result.get(key), list):
                items = result[key]
                break
        else:
            items = [dict(v, id=k) if isinstance(v, dict) else {"id": k}
                     for k, v in result.items()]
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append({"id": it})
        elif isinstance(it, dict):
            cid = it.get("id") or it.get("command") or it.get("name")
            if cid:
                out.append(dict(it, id=cid))
    return out


def fill(value, mapping):
    """Recursively replace {placeholders} inside strings of a JSON structure."""
    if isinstance(value, str):
        for k, v in mapping.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [fill(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, mapping) for k, v in value.items()}
    return value


def style_params(v):
    """captions.setStyle parameters from the panel values (same as filmcraft_subtitles_gui.py)."""
    p = {"font": v["font"], "fontStyle": v["fontStyle"],
         "size": float(v["size"]), "color": v["color"],
         "align": v["align"], "anchor": v["anchor"],
         "margin": max(0.0, min(0.45, float(v["margin"]))),
         "lineSpacing": float(v["lineSpacing"]),
         "outline": float(v["outline"]), "outlineColor": v["outlineColor"],
         "background": bool(v["background"])}
    if v["background"]:  # box colour is sent as #rrggbbaa
        a = int(round(max(0.0, min(100.0, float(v["bgOpacity"]))) * 2.55))
        p["backgroundColor"] = v["backgroundColor"].lstrip("#")[:6].join(("#", "%02x" % a))
    return p


def _ts(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s_, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s_:02d},{ms:03d}"


def words_to_srt(data, pause, max_seconds, max_chars):
    """Build SRT text from a whisper-ctranslate2 JSON result.

    A new caption starts when the silence between two words is >= pause seconds,
    when the caption would run longer than max_seconds, or longer than max_chars.
    Falls back to whisper's own segments if the JSON has no word timings.
    Returns (srt_text, caption_count, used_words).
    """
    words, segs = [], []
    for seg in data.get("segments", []):
        segs.append((float(seg.get("start", 0)), float(seg.get("end", 0)), str(seg.get("text", "")).strip()))
        for w in seg.get("words") or []:
            if w.get("start") is not None and w.get("end") is not None:
                words.append((float(w["start"]), float(w["end"]), str(w.get("word", ""))))
    caps = []
    if words:
        cur, c_start, c_end = "", None, None
        for ws, we, wt in words:
            token = wt.strip()
            if not token:
                continue
            if cur:
                too_long = (we - c_start) > max_seconds or len(cur) + 1 + len(token) > max_chars
                if (ws - c_end) >= pause or too_long:
                    caps.append((c_start, c_end, cur))
                    cur, c_start = "", None
            if not cur:
                cur, c_start = token, ws
            else:
                cur += " " + token
            c_end = we
        if cur:
            caps.append((c_start, c_end, cur))
    else:
        caps = [(a, b, t) for a, b, t in segs if t]
    lines = []
    for i, (a, b, t) in enumerate(caps, 1):
        if b <= a:
            b = a + 0.3
        lines.append(f"{i}\n{_ts(a)} --> {_ts(b)}\n{t}\n")
    return "\n".join(lines), len(caps), bool(words)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FilmCraft - Speech to Text")
        self.geometry("860x960")
        self.cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except Exception:
                pass
        for key in ("import_command", "import_params"):
            if not str(self.cfg.get(key, "")).strip():
                self.cfg[key] = DEFAULTS[key]
        saved_style = self.cfg.get("style")
        self.cfg["style"] = {**STYLE_DEFAULTS, **(saved_style if isinstance(saved_style, dict) else {})}

        self.log_q = queue.Queue()
        self.busy = False
        self.commands = []
        self.stop_flag = threading.Event()

        c = self.cfg
        self.v_exe = tk.StringVar(value=c["exe"])
        self.v_model = tk.StringVar(value=c["model"])
        self.v_device = tk.StringVar(value=c["device"])
        self.v_lang = tk.StringVar(value=c["language"])
        self.v_extra = tk.StringVar(value=c["extra"])
        self.v_split = tk.BooleanVar(value=bool(c["split"]))
        self.v_pause = tk.StringVar(value=str(c["pause"]))
        self.v_maxsec = tk.StringVar(value=str(c["max_seconds"]))
        self.v_maxchars = tk.StringVar(value=str(c["max_chars"]))
        self.v_vad = tk.BooleanVar(value=bool(c["vad"]))
        self.v_media = tk.StringVar(value=c["media"])
        self.v_outdir = tk.StringVar(value=c["outdir"])
        self.v_srt = tk.StringVar(value=c["srt"])
        self.v_host = tk.StringVar(value=c["host"])
        self.v_port = tk.StringVar(value=str(c["port"]))
        self.v_token = tk.StringVar(value=os.environ.get("FILMCRAFT_CONTROL_TOKEN", ""))
        self.v_cmd = tk.StringVar(value=c["import_command"])
        self.v_params = tk.StringVar(value=c["import_params"])
        self.v_replace = tk.BooleanVar(value=bool(c["replace_c1"]))
        self.v_shift = tk.StringVar(value=str(c["shift"]))
        self.v_style_on = tk.BooleanVar(value=bool(c["style_on"]))
        self.v_style_json = tk.StringVar(value=c["style_json"])
        self.v_st = {k: (tk.BooleanVar(value=bool(v)) if k == "background" else tk.StringVar(value=str(v)))
                     for k, v in c["style"].items()}

        self._build()
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.v_token.get():
            self.log("No FILMCRAFT_CONTROL_TOKEN in the environment. Start this from the "
                     "FILMCRAFT.bat window, or paste the token below.")
        if not shutil.which(self.v_exe.get() or "whisper-ctranslate2"):
            self.log("whisper-ctranslate2 is not on PATH here. Start from the FILMCRAFT.bat "
                     "window (Anaconda base), or put its full path in 'Program'.")

    # ---- layout ----------------------------------------------------------
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(root, text="1. Transcribe (whisper-ctranslate2)",
                  font=("", 11, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", **pad)
        r += 1
        ttk.Label(root, text="Media file or folder").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_media).grid(row=r, column=1, sticky="ew", **pad)
        mb = ttk.Frame(root)
        mb.grid(row=r, column=2)
        ttk.Button(mb, text="File...", command=self._pick_media).pack(side="left", padx=2)
        ttk.Button(mb, text="Folder...", command=self._pick_media_folder).pack(side="left", padx=2)
        r += 1
        ttk.Label(root, text="SRT output folder").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_outdir).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self._pick_outdir).grid(row=r, column=2, **pad)
        r += 1
        opts = ttk.Frame(root)
        opts.grid(row=r, column=0, columnspan=3, sticky="ew")
        opts.columnconfigure(7, weight=1)
        ttk.Label(opts, text="Model").grid(row=0, column=0, **pad)
        ttk.Combobox(opts, textvariable=self.v_model, width=10,
                     values=("tiny", "base", "small", "medium", "large-v2", "large-v3")
                     ).grid(row=0, column=1, **pad)
        ttk.Label(opts, text="Device").grid(row=0, column=2, **pad)
        ttk.Combobox(opts, textvariable=self.v_device, width=6,
                     values=("cuda", "cpu", "auto")).grid(row=0, column=3, **pad)
        ttk.Label(opts, text="Language").grid(row=0, column=4, **pad)
        ttk.Entry(opts, textvariable=self.v_lang, width=6).grid(row=0, column=5, **pad)
        ttk.Label(opts, text="(blank = auto)", foreground="gray").grid(row=0, column=6, **pad)
        r += 1
        ttk.Label(root, text="Extra arguments").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_extra).grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1
        sp = ttk.Frame(root)
        sp.grid(row=r, column=0, columnspan=3, sticky="ew")
        ttk.Checkbutton(sp, text="Split on pauses", variable=self.v_split).pack(side="left", padx=6)
        ttk.Label(sp, text="Pause >= (s)").pack(side="left", padx=(8, 2))
        ttk.Entry(sp, textvariable=self.v_pause, width=5).pack(side="left")
        ttk.Label(sp, text="Max secs").pack(side="left", padx=(8, 2))
        ttk.Entry(sp, textvariable=self.v_maxsec, width=5).pack(side="left")
        ttk.Label(sp, text="Max chars").pack(side="left", padx=(8, 2))
        ttk.Entry(sp, textvariable=self.v_maxchars, width=5).pack(side="left")
        ttk.Checkbutton(sp, text="Voice filter (VAD)", variable=self.v_vad).pack(side="left", padx=(14, 2))
        r += 1
        ttk.Label(root, text="Program").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_exe).grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1
        ttk.Label(root, text="SRT to send").grid(row=r, column=0, sticky="w", **pad)
        self.cb_srt = ttk.Combobox(root, textvariable=self.v_srt)
        self.cb_srt.grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self._pick_srt).grid(row=r, column=2, **pad)

        r += 1
        ttk.Separator(root).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8)
        r += 1
        ttk.Label(root, text="2. FilmCraft control", font=("", 11, "bold")
                  ).grid(row=r, column=0, columnspan=3, sticky="w", **pad)
        r += 1
        conn = ttk.Frame(root)
        conn.grid(row=r, column=0, columnspan=3, sticky="ew")
        conn.columnconfigure(5, weight=1)
        ttk.Label(conn, text="Host").grid(row=0, column=0, **pad)
        ttk.Entry(conn, textvariable=self.v_host, width=14).grid(row=0, column=1, **pad)
        ttk.Label(conn, text="Port").grid(row=0, column=2, **pad)
        ttk.Entry(conn, textvariable=self.v_port, width=7).grid(row=0, column=3, **pad)
        ttk.Label(conn, text="Token").grid(row=0, column=4, **pad)
        ttk.Entry(conn, textvariable=self.v_token, show="*").grid(row=0, column=5, sticky="ew", **pad)
        ttk.Button(conn, text="Test connection", command=self.on_test).grid(row=0, column=6, **pad)
        r += 1
        ttk.Label(root, text="Import command").grid(row=r, column=0, sticky="w", **pad)
        self.cb_cmd = ttk.Combobox(root, textvariable=self.v_cmd, values=("captions.import",))
        self.cb_cmd.grid(row=r, column=1, sticky="ew", **pad)
        self.cb_cmd.bind("<<ComboboxSelected>>", self._on_cmd_selected)
        ttk.Button(root, text="Find caption commands", command=self.on_find).grid(row=r, column=2, **pad)
        r += 1
        ttk.Label(root, text="Command params").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_params).grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1
        ttk.Label(root, text="JSON; {srt} = the SRT path, {stem} = its file name. Rename keys to match the command.",
                  foreground="gray").grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        r += 1
        imp = ttk.Frame(root)
        imp.grid(row=r, column=0, columnspan=3, sticky="ew")
        ttk.Checkbutton(imp, text="Replace caption track C1", variable=self.v_replace).pack(side="left", padx=6)
        ttk.Label(imp, text="Shift (s)").pack(side="left", padx=(12, 2))
        ttk.Entry(imp, textvariable=self.v_shift, width=7).pack(side="left")

        r += 1
        sf = ttk.LabelFrame(root, text="Caption style (captions.setStyle)", padding=6)
        sf.grid(row=r, column=0, columnspan=3, sticky="ew", padx=6, pady=4)
        top = ttk.Frame(sf)
        top.grid(row=0, column=0, columnspan=6, sticky="w")
        ttk.Checkbutton(top, text="Apply this style after importing",
                        variable=self.v_style_on).pack(side="left")
        ttk.Button(top, text="Apply to track C1 now", command=self.on_apply_style).pack(side="left", padx=14)

        def cell(row, col, label, widget):
            ttk.Label(sf, text=label).grid(row=row, column=col * 2, sticky="w", padx=(0, 4), pady=2)
            widget.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 12), pady=2)

        st = self.v_st
        families = sorted(set(tkfont.families(self)), key=str.lower)
        cell(1, 0, "Font", ttk.Combobox(sf, textvariable=st["font"], values=families, width=18))
        cell(1, 1, "Weight", ttk.Combobox(sf, textvariable=st["fontStyle"], values=FONT_STYLES,
                                          width=11, state="readonly"))
        cell(1, 2, "Size (px)", ttk.Spinbox(sf, textvariable=st["size"], from_=8, to=300, width=6))
        cell(2, 0, "Text colour", self._colour_widget(sf, "color"))
        cell(2, 1, "Outline", ttk.Spinbox(sf, textvariable=st["outline"], from_=0, to=20,
                                          increment=0.5, width=6))
        cell(2, 2, "Outline colour", self._colour_widget(sf, "outlineColor"))
        cell(3, 0, "Box", ttk.Checkbutton(sf, variable=st["background"]))
        cell(3, 1, "Box colour", self._colour_widget(sf, "backgroundColor"))
        cell(3, 2, "Box opacity %", ttk.Spinbox(sf, textvariable=st["bgOpacity"], from_=0, to=100, width=6))
        cell(4, 0, "Align", ttk.Combobox(sf, textvariable=st["align"], values=["left", "center", "right"],
                                         width=8, state="readonly"))
        cell(4, 1, "Position", ttk.Combobox(sf, textvariable=st["anchor"], values=["top", "middle", "bottom"],
                                            width=8, state="readonly"))
        cell(4, 2, "Edge margin", ttk.Entry(sf, textvariable=st["margin"], width=6))
        cell(5, 0, "Line spacing", ttk.Spinbox(sf, textvariable=st["lineSpacing"], from_=0.5, to=3.0,
                                               increment=0.1, width=6))
        ttk.Label(sf, text="Style JSON").grid(row=6, column=0, sticky="w", pady=(6, 2))
        ttk.Entry(sf, textvariable=self.v_style_json).grid(row=6, column=1, columnspan=5, sticky="ew", pady=(6, 2))
        ttk.Label(sf, wraplength=700, foreground="gray", text=(
            'Optional extra fields for captions.setStyle, e.g. {"size": 48, "outline": 2}. They are '
            "added on top of the panel (and sent even if the panel is off). Blank = nothing extra."
        )).grid(row=7, column=1, columnspan=5, sticky="w")

        r += 1
        ttk.Separator(root).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8)
        r += 1
        bar = ttk.Frame(root)
        bar.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.btn_stt = ttk.Button(bar, text="1. Transcribe", command=self.on_transcribe)
        self.btn_send = ttk.Button(bar, text="2. Send SRT to FilmCraft", command=self.on_send)
        self.btn_both = ttk.Button(bar, text="Transcribe + send", command=self.on_both)
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop_flag.set, state="disabled")
        for b in (self.btn_stt, self.btn_send, self.btn_both, self.btn_stop):
            b.pack(side="left", padx=6, pady=3)

        r += 1
        root.rowconfigure(r, weight=1)
        logf = ttk.Frame(root)
        logf.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.txt = tk.Text(logf, wrap="word", height=14, state="disabled")
        sb = ttk.Scrollbar(logf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

    # ---- helpers ---------------------------------------------------------
    def log(self, msg):
        self.log_q.put(str(msg))

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

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_stt, self.btn_send, self.btn_both):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _save_cfg(self):
        data = {
            "exe": self.v_exe.get(), "model": self.v_model.get(), "device": self.v_device.get(),
            "language": self.v_lang.get(), "extra": self.v_extra.get(),
            "media": self.v_media.get(), "outdir": self.v_outdir.get(), "srt": self.v_srt.get(),
            "host": self.v_host.get(), "port": self.v_port.get(),
            "import_command": self.v_cmd.get(), "import_params": self.v_params.get(),
            "split": self.v_split.get(), "pause": self.v_pause.get(),
            "max_seconds": self.v_maxsec.get(), "max_chars": self.v_maxchars.get(),
            "vad": self.v_vad.get(), "replace_c1": self.v_replace.get(), "shift": self.v_shift.get(),
            "style_on": self.v_style_on.get(), "style_json": self.v_style_json.get(),
            "style": {k: v.get() for k, v in self.v_st.items()},
        }  # the token is never written to disk
        try:
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        self._save_cfg()
        self.destroy()

    def _pick_media(self):
        p = filedialog.askopenfilename(
            title="Choose audio or video",
            filetypes=[("Media", " ".join(f"*.{e}" for e in MEDIA_EXTS) + " *.flac *.aac *.avi *.webm"),
                       ("All files", "*.*")])
        if p:
            self.v_media.set(os.path.normpath(p))

    def _pick_media_folder(self):
        p = filedialog.askdirectory(title="Folder with audio/video files (batch)")
        if p:
            self.v_media.set(os.path.normpath(p))

    def _pick_outdir(self):
        p = filedialog.askdirectory(title="Folder for the .srt files")
        if p:
            self.v_outdir.set(os.path.normpath(p))

    def _pick_srt(self):
        p = filedialog.askopenfilename(
            title="Choose an .srt", filetypes=[("SubRip", "*.srt"), ("All files", "*.*")])
        if p:
            self.v_srt.set(os.path.normpath(p))

    def _colour_widget(self, parent, key):
        var = self.v_st[key]
        f = ttk.Frame(parent)
        ttk.Entry(f, textvariable=var, width=9).pack(side="left")
        sw = tk.Label(f, width=3, relief="solid", borderwidth=1)
        sw.pack(side="left", padx=3)

        def pick():
            try:
                c = colorchooser.askcolor(color=var.get(), parent=self)[1]
            except tk.TclError:  # the entry did not hold a valid colour
                c = colorchooser.askcolor(parent=self)[1]
            if c:
                var.set(c)

        def paint(*_):
            try:
                sw.configure(bg=var.get())
            except tk.TclError:
                pass
        ttk.Button(f, text="...", width=3, command=pick).pack(side="left")
        var.trace_add("write", paint)
        paint()
        return f

    def _style_to_send(self, force_panel=False):
        """captions.setStyle parameters from the panel and/or the JSON field, or None."""
        style = {}
        if force_panel or self.v_style_on.get():
            style.update(style_params({k: v.get() for k, v in self.v_st.items()}))
        txt = self.v_style_json.get().strip()
        if txt:
            extra = json.loads(txt)
            if not isinstance(extra, dict):
                raise ValueError('Style JSON must be an object like {"size": 48}')
            style.update(extra)
        return style or None

    def on_apply_style(self):
        def work():
            try:
                style = self._style_to_send(force_panel=True)
            except ValueError as exc:
                self.log(f"Check the style fields: {exc}")
                return
            try:
                res = self._fc("captions.setStyle", track="C1", **style)
                self.log("Styled track C1: " + json.dumps(res, separators=(",", ":"))[:200])
            except RuntimeError as exc:
                self.log(f"FilmCraft refused it: {exc}")
            except OSError as exc:
                self.log(f"Could not reach FilmCraft: {exc}")
        self._run_thread(work)

    def _on_cmd_selected(self, _event=None):
        cid = self.v_cmd.get()
        for c in self.commands:
            if c["id"] == cid:
                self.log(f"{cid}: {json.dumps({k: v for k, v in c.items() if k != 'id'})}")
                break

    def _conn(self):
        return self.v_host.get().strip(), self.v_port.get().strip(), self.v_token.get().strip()

    def _run_thread(self, fn):
        if self.busy:
            return
        self._save_cfg()
        self.stop_flag.clear()
        self._set_busy(True)

        def runner():
            try:
                fn()
            except Exception as exc:  # show anything unexpected in the log
                self.log(f"ERROR: {exc}")
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=runner, daemon=True).start()

    # ---- FilmCraft actions ----------------------------------------------
    def on_test(self):
        def work():
            host, port, token = self._conn()
            reply = fc_call(host, port, token, "engine.commands", timeout=15)
            if reply.get("ok"):
                n = len(normalize_commands(reply.get("result")))
                self.log(f"Connected to FilmCraft on {host}:{port} ({n} commands).")
            else:
                self.log(f"FilmCraft replied: {reply.get('error')}")
        self._run_thread(work)

    def on_find(self):
        def work():
            host, port, token = self._conn()
            reply = fc_call(host, port, token, "engine.commands", timeout=30)
            if not reply.get("ok"):
                self.log(f"FilmCraft replied: {reply.get('error')}")
                return
            self.commands = normalize_commands(reply.get("result"))
            hits = [c for c in self.commands
                    if any(w in json.dumps(c).lower() for w in CAPTION_WORDS)]
            ids = [c["id"] for c in (hits or self.commands)]
            self.after(0, lambda: self.cb_cmd.configure(values=ids))
            if hits:
                self.log(f"{len(hits)} caption/subtitle-related command(s) found:")
                for c in hits:
                    self.log(f"  {c['id']}  {json.dumps({k: v for k, v in c.items() if k != 'id'})[:300]}")
                if not self.v_cmd.get():
                    self.after(0, lambda: self.v_cmd.set(hits[0]["id"]))
                self.log("Pick the right one in 'Import command' and set its params if the key isn't 'path'.")
            else:
                self.log("No command mentions srt/caption/subtitle. The full list is in the dropdown.")
        self._run_thread(work)

    def _fc(self, command, **params):
        """Run one FilmCraft command; return its result or raise RuntimeError."""
        host, port, token = self._conn()
        reply = fc_call(host, port, token, "engine.execute", {"command": command, "params": params})
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "FilmCraft reported an error")
        return reply.get("result")

    def _send_work(self, srt=None):
        srt = str(srt or self.v_srt.get()).strip()
        if not srt or not Path(srt).exists():
            self.log("No SRT file yet. Transcribe first, or Browse to an existing .srt.")
            return False
        cmd_id = self.v_cmd.get().strip() or "captions.import"
        try:
            params = fill(json.loads(self.v_params.get() or "{}"),
                          {"srt": str(Path(srt).resolve()), "stem": Path(srt).stem})
            shift_s = float(self.v_shift.get().strip() or 0)
            style = self._style_to_send()
        except ValueError as exc:  # bad JSON, a non-numeric shift or a bad style value
            self.log(f"Check 'Command params' (JSON), 'Shift' and the style fields: {exc}")
            return False
        host, port, _ = self._conn()
        try:
            if self.v_replace.get():
                try:
                    self._fc("captions.deleteTrack", track="C1")
                    self.log("Removed the existing C1 caption track.")
                except RuntimeError as exc:
                    self.log(f"No existing C1 track to replace ({exc}).")
            self.log(f"Sending to FilmCraft: {cmd_id} {json.dumps(params)}")
            res = self._fc(cmd_id, **params)
            self.log("  " + json.dumps(res, separators=(",", ":"))[:300])
            track = "C1"
            if isinstance(res, dict) and res.get("track") not in (None, ""):
                track = res["track"]

            if shift_s:
                seq = self._fc("sequence.inspect")
                fr = (seq or {}).get("settings", {}).get("frame_rate", {})
                fps = fr.get("num", 25) / float(fr.get("den", 1) or 1)
                lst = self._fc("captions.list", track=track)
                ids = []

                def walk(x):
                    if isinstance(x, dict):
                        if isinstance(x.get("id"), int) and (
                                "text" in x or "start" in x or "startFrame" in x):
                            ids.append(x["id"])
                        for v in x.values():
                            walk(v)
                    elif isinstance(x, list):
                        for v in x:
                            walk(v)
                walk(lst)
                if ids:
                    self._fc("captions.move", captions=ids, deltaFrames=int(round(shift_s * fps)))
                    self.log(f"Shifted {len(ids)} captions by {shift_s:+g} s.")
                else:
                    self.log("Could not find caption ids to shift; captions.list said: "
                             + json.dumps(lst)[:300])
            if style:
                res = self._fc("captions.setStyle", track=track, **style)
                self.log(f"Styled track {track}: " + json.dumps(res, separators=(",", ":"))[:200])
            self.log("Done - the captions are in FilmCraft.")
            return True
        except RuntimeError as exc:
            self.log(f"FilmCraft refused it: {exc}")
            self.log("Is a sequence open in FilmCraft? Is the import command right? "
                     "(see 'Find caption commands')")
        except OSError as exc:
            self.log(f"Could not reach FilmCraft on {host}:{port}: {exc}")
        return False

    # ---- transcription ---------------------------------------------------
    def _run(self, cmd, cwd):
        """Run one whisper-ctranslate2 call, stream its output, honour Stop."""
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def pump():
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log(line)
        threading.Thread(target=pump, daemon=True).start()

        while proc.poll() is None:
            if self.stop_flag.is_set():
                if os.name == "nt":  # also kills the python process behind the launcher exe
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True)
                else:
                    proc.terminate()
                self.log("Stopped.")
                return None
            time.sleep(0.3)
        if proc.returncode != 0:
            self.log(f"whisper-ctranslate2 exited with code {proc.returncode}.")
        return proc.returncode

    def _transcribe_work(self):
        """Returns the Path of the SRT to send (newest of the run), or None."""
        name = self.v_exe.get().strip() or "whisper-ctranslate2"
        exe = shutil.which(name) or (name if Path(name).exists() else None)
        if not exe:
            self.log(f"Cannot find '{name}'. Start from the FILMCRAFT.bat window (Anaconda base) "
                     "or enter its full path in 'Program'.")
            return None
        media = self.v_media.get().strip()
        if not media or not Path(media).exists():
            self.log("Choose an existing media file or folder first.")
            return None

        target = Path(media)
        if target.is_dir():
            folder = target
            files = sorted(f for f in target.iterdir()
                           if f.is_file() and f.suffix.lower().lstrip(".") in MEDIA_EXTS)
            if not files:
                self.log(f"No {', '.join(MEDIA_EXTS)} files in {folder}.")
                return None
        else:
            folder, files = target.parent, [target]

        outdir = Path(self.v_outdir.get().strip() or (folder / "FilmCraft_Captions"))
        outdir.mkdir(parents=True, exist_ok=True)
        model = self.v_model.get().strip() or "medium"
        device = self.v_device.get().strip() or "cuda"
        lang = self.v_lang.get().strip()
        try:
            extra = shlex.split(self.v_extra.get(), posix=(os.name != "nt"))
        except ValueError as exc:
            self.log(f"Extra arguments could not be read: {exc}")
            return None

        split, vad = self.v_split.get(), self.v_vad.get()
        try:
            pause = float(self.v_pause.get().strip() or 0.5)
            max_s = float(self.v_maxsec.get().strip() or 6)
            max_c = int(float(self.v_maxchars.get().strip() or 50))
        except ValueError:
            self.log("Pause, Max secs and Max chars must be numbers.")
            return None

        made = []
        for i, f in enumerate(files, 1):
            cmd = [exe, str(f), "--model", model, "--device", device,
                   "--output_format", "json" if split else "srt", "--output_dir", str(outdir)]
            if split:
                cmd += ["--word_timestamps", "True"]
            if vad:
                cmd += ["--vad_filter", "True"]
            if lang:
                cmd += ["--language", lang]
            cmd += extra
            self.log(f"[{i}/{len(files)}] {subprocess.list2cmdline(cmd)}")
            t0 = time.time()
            if self._run(cmd, folder) is None:  # stopped
                return None
            srt = outdir / (f.stem + ".srt")
            if split:  # build the SRT ourselves from the word timings
                js = outdir / (f.stem + ".json")
                if not js.exists() or js.stat().st_mtime < t0 - 2:
                    self.log(f"  No .json came back for {f.name}. Check the messages above.")
                    continue
                try:
                    text, n, used = words_to_srt(json.loads(js.read_text(encoding="utf-8")),
                                                 pause, max_s, max_c)
                except (ValueError, KeyError, TypeError) as exc:
                    self.log(f"  Could not read the word timings in {js.name}: {exc}")
                    continue
                if not n:
                    self.log(f"  Whisper heard no speech in {f.name}, so there are no captions.")
                    continue
                srt.write_text(text, encoding="utf-8")
                self.log(f"  Split into {n} captions on pauses >= {pause:g} s"
                         + ("" if used else " (no word timings found, used Whisper's own segments)"))
            if srt.exists() and srt.stat().st_mtime >= t0 - 2:
                made.append(srt)
                self.log(f"  SRT written: {srt}")
            else:
                self.log(f"  No {srt.name} was produced for {f.name}.")

        if not made:
            self.log("No SRT files were produced. Check the messages above.")
            return None
        pick = made[-1]
        names = [str(p) for p in made]
        self.after(0, lambda: (self.cb_srt.configure(values=names), self.v_srt.set(str(pick))))
        self.log(f"SRT ready: {pick}" + (f"  ({len(made)} made; the list in 'SRT to send' has the rest)"
                                         if len(made) > 1 else ""))
        return pick

    def on_transcribe(self):
        self._run_thread(self._transcribe_work)

    def on_send(self):
        self._run_thread(self._send_work)

    def on_both(self):
        def work():
            srt = self._transcribe_work()
            if srt:
                self._send_work(srt)
        self._run_thread(work)


if __name__ == "__main__":
    App().mainloop()
