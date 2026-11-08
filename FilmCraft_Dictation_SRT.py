"""FilmCraft_Dictation_SRT.py - speak, get captions in FilmCraft.

  1. Records your microphone, or whatever the speakers are playing (Windows loopback).
                                           (sounddevice / PyAudioWPatch)
  2. Saves the recording as an MP3.      (lameenc, or ffmpeg if lameenc is missing)
  3. Transcribes it to an .srt.          (whisper-ctranslate2, GPU with --device cuda)
  4. Imports the .srt into FilmCraft     (captions.import over the control channel)
     as a caption track, optionally shifted and styled.

Two modes (choose in the window):
  Record, then transcribe (MP3) - the four steps above.
  Live captions (real-time)     - no MP3. faster-whisper is loaded once; each phrase is
      transcribed as soon as you pause, appended to a live .srt, and the caption track in
      FilmCraft is refreshed after every phrase.

One-time setup, in the Anaconda (base) window that FILMCRAFT.bat opens:

    pip install sounddevice lameenc PyAudioWPatch

(PyAudioWPatch is only needed to record what comes out of the speakers.)

Run it from that same window (it needs FILMCRAFT_CONTROL_TOKEN, and whisper-ctranslate2
on PATH):

    python FilmCraft_Dictation_SRT.py

Open the sequence you want the captions for in FilmCraft before you import.
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
import wave
from array import array
from collections import deque
from pathlib import Path
from tkinter import ttk, filedialog

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "FilmCraft_Dictation_SRT.json"
DEFAULT_OUT = HERE / "FilmCraft_Dictation"
MIN_SECONDS = 0.5
MODE_RECORD = "Record, then transcribe (MP3)"
MODE_LIVE = "Live captions (real-time)"

DEFAULTS = {
    "input_device": "System default microphone",
    "outdir": "",                      # empty = <script folder>\FilmCraft_Dictation
    "exe": "whisper-ctranslate2",
    "model": "medium",
    "device": "cuda",
    "language": "",                    # empty = auto-detect
    "extra": "",
    "split": True,                     # build the SRT from word timings
    "pause": "0.5",                    # new caption after a gap this long (seconds)
    "max_seconds": "6",                # ... or when a caption gets this long
    "max_chars": "50",                 # ... or this many characters
    "vad": True,                       # --vad_filter True
    "mode": MODE_RECORD,
    "live_pause": "0.7",               # live: a phrase ends after this much silence (s)
    "live_max": "12",                  # live: longest phrase before it is cut (s)
    "live_level": "0.015",             # live: minimum loudness that counts as speech (0-1)
    "host": "127.0.0.1",
    "port": os.environ.get("FILMCRAFT_CONTROL_PORT", "9876"),
    "auto_import": True,
    "replace_c1": True,
    "shift": "0",
    "style": "",                       # optional JSON for captions.setStyle
}


# --------------------------------------------------------------------------
# FilmCraft control channel
# --------------------------------------------------------------------------
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


def fc_run(o, command, **params):
    """Run one FilmCraft command; return its result or raise RuntimeError."""
    reply = fc_call(o["host"], o["port"], o["token"], "engine.execute",
                    {"command": command, "params": params})
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error") or "FilmCraft reported an error")
    return reply.get("result")


# --------------------------------------------------------------------------
# Audio helpers
# --------------------------------------------------------------------------
def write_wav(path, pcm, sr, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def save_mp3(pcm, sr, mp3_path, log, channels=1):
    """16-bit interleaved PCM -> MP3. Returns the file actually written (WAV as a last resort)."""
    try:
        import lameenc
        enc = lameenc.Encoder()
        if hasattr(enc, "silence"):
            enc.silence()
        enc.set_bit_rate(96 if channels == 1 else 128)
        enc.set_in_sample_rate(sr)
        enc.set_channels(channels)
        enc.set_quality(2)
        data = bytes(enc.encode(pcm)) + bytes(enc.flush())
        mp3_path.write_bytes(data)
        return mp3_path
    except ImportError:
        log("lameenc is not installed (pip install lameenc); trying ffmpeg ...")
    except Exception as exc:
        log(f"lameenc failed ({exc}); trying ffmpeg ...")

    wav = mp3_path.with_suffix(".wav")
    write_wav(wav, pcm, sr, channels)
    ff = shutil.which("ffmpeg")
    if ff:
        res = subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(wav),
                              "-codec:a", "libmp3lame", "-b:a", "96k", str(mp3_path)],
                             capture_output=True, text=True)
        if res.returncode == 0 and mp3_path.exists():
            wav.unlink(missing_ok=True)
            return mp3_path
        log(f"ffmpeg failed: {res.stderr.strip()[:300]}")
    log("Could not make an MP3, so the recording is kept as WAV (pip install lameenc "
        "to get MP3). Transcribing the WAV instead.")
    return wav


def _ts(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s_, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s_:02d},{ms:03d}"


def captions_from_data(data, pause, max_seconds, max_chars):
    """whisper-style {"segments": [...]} -> ([(start, end, text)], used_word_timings).

    A new caption starts when the silence between two words is >= pause seconds,
    when the caption would run longer than max_seconds, or longer than max_chars.
    Falls back to whisper's own segments if there are no word timings.
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
        caps = [(a_, b_, t) for a_, b_, t in segs if t]
    return caps, bool(words)


def captions_to_srt(caps):
    lines = []
    for i, (a_, b_, t) in enumerate(caps, 1):
        if b_ <= a_:
            b_ = a_ + 0.3
        lines.append(f"{i}\n{_ts(a_)} --> {_ts(b_)}\n{t}\n")
    return "\n".join(lines)


def words_to_srt(data, pause, max_seconds, max_chars):
    """Returns (srt_text, caption_count, used_words)."""
    caps, used = captions_from_data(data, pause, max_seconds, max_chars)
    return captions_to_srt(caps), len(caps), used


def resample_16k(x, sr):
    """float32 mono at sr Hz -> float32 mono at 16 kHz (what Whisper expects)."""
    import numpy as np
    if int(sr) == 16000:
        return x
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(16000, int(sr))
        return resample_poly(x, 16000 // g, int(sr) // g).astype(np.float32)
    except ImportError:  # no scipy: box low-pass, then linear interpolation
        k = max(1, int(round(sr / 16000)))
        if k > 1:
            x = np.convolve(x, np.ones(k, dtype=np.float32) / k, mode="same")
        n = max(1, int(len(x) * 16000 / sr))
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def command_ids(result):
    """Command names from an engine.commands result (list or dict, strings or dicts)."""
    items = result
    if isinstance(result, dict):
        for key in ("commands", "items", "result"):
            if isinstance(result.get(key), list):
                items = result[key]
                break
        else:
            return [str(k) for k in result]
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            cid = it.get("id") or it.get("command") or it.get("name")
            if cid:
                out.append(str(cid))
    return out


def list_input_devices():
    """-> (entries, hints). entries = [(label, (kind, ref))] with kind "mic" or "loop"."""
    out, hints = [("System default microphone", ("mic", None))], []
    try:
        import sounddevice as sd
        try:
            host_api = sd.query_devices(kind="input")["hostapi"]
        except Exception:
            host_api = None
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and (host_api is None or d["hostapi"] == host_api):
                out.append((f"Mic {i}: {d['name']}", ("mic", i)))
    except ImportError:
        hints.append("sounddevice is not installed (pip install sounddevice lameenc), "
                     "so no microphones are listed.")
    except Exception as exc:
        hints.append(f"Could not list microphones: {exc}")
    try:
        import pyaudiowpatch as pa
        pyaudio = pa.PyAudio()
        try:
            default_name = ""
            try:
                wasapi = pyaudio.get_host_api_info_by_type(pa.paWASAPI)
                default_name = pyaudio.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
            except Exception:
                pass
            loops = []
            for lb in pyaudio.get_loopback_device_info_generator():
                is_default = bool(default_name) and default_name in lb["name"]
                label = "Speakers: " + lb["name"].replace(" [Loopback]", "") + (" (default)" if is_default else "")
                loops.append((not is_default, label, ("loop", dict(lb))))
            for _, label, ref in sorted(loops, key=lambda t: t[0]):
                out.append((label, ref))
            if not loops:
                hints.append("PyAudioWPatch found no speaker (loopback) devices.")
        finally:
            pyaudio.terminate()
    except ImportError:
        hints.append("To record what comes out of the speakers: pip install PyAudioWPatch (Windows only).")
    except Exception as exc:
        hints.append(f"Speaker capture is unavailable: {exc}")
    return out, hints


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FilmCraft Dictation to SRT")
        self.geometry("780x840")
        self.cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except Exception:
                pass

        self.log_q = queue.Queue()
        self.recording = False
        self.working = False
        self.stream = None
        self.chunks = []
        self.peak = 0.0
        self.rec_sr = 44100
        self.rec_channels = 1
        self.rec_kind = "mic"
        self.pa = None
        self.live_active = False
        self.live_caps = []
        self.live_q = None
        self.live_threads = []
        self.live_srt = None
        self.rec_started = 0.0
        self.last_srt = None
        self.device_map = {"System default microphone": ("mic", None)}

        c = self.cfg
        self.v_dev = tk.StringVar(value=c["input_device"])
        self.v_outdir = tk.StringVar(value=c["outdir"])
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
        self.v_mode = tk.StringVar(value=c["mode"])
        self.v_live_pause = tk.StringVar(value=str(c["live_pause"]))
        self.v_live_max = tk.StringVar(value=str(c["live_max"]))
        self.v_live_level = tk.StringVar(value=str(c["live_level"]))
        self.v_host = tk.StringVar(value=c["host"])
        self.v_port = tk.StringVar(value=str(c["port"]))
        self.v_token = tk.StringVar(value=os.environ.get("FILMCRAFT_CONTROL_TOKEN", ""))
        self.v_auto = tk.BooleanVar(value=bool(c["auto_import"]))
        self.v_replace = tk.BooleanVar(value=bool(c["replace_c1"]))
        self.v_shift = tk.StringVar(value=str(c["shift"]))
        self.v_style = tk.StringVar(value=c["style"])
        self.v_status = tk.StringVar(value="Ready.")
        self.v_level = tk.DoubleVar(value=0.0)

        self._build()
        self._refresh_devices(first=True)
        self._on_mode()
        self.after(100, self._drain_log)
        self.after(80, self._tick)
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

        ttk.Label(root, text="1. Record", font=("", 11, "bold")).grid(
            row=r, column=0, columnspan=3, sticky="w", **pad)
        r += 1
        ttk.Label(root, text="Mode").grid(row=r, column=0, sticky="w", **pad)
        cbm = ttk.Combobox(root, textvariable=self.v_mode, state="readonly",
                           values=(MODE_RECORD, MODE_LIVE))
        cbm.grid(row=r, column=1, sticky="ew", **pad)
        cbm.bind("<<ComboboxSelected>>", self._on_mode)
        r += 1
        ttk.Label(root, text="Audio source").grid(row=r, column=0, sticky="w", **pad)
        self.cb_dev = ttk.Combobox(root, textvariable=self.v_dev, state="readonly")
        self.cb_dev.grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Refresh", command=self._refresh_devices).grid(row=r, column=2, **pad)
        r += 1
        ttk.Label(root, text="Save to folder").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_outdir).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self._pick_outdir).grid(row=r, column=2, **pad)
        r += 1
        ttk.Label(root, text="(blank = FilmCraft_Dictation next to this script)",
                  foreground="gray").grid(row=r, column=1, sticky="w", padx=6)

        r += 1
        lv = ttk.Frame(root)
        lv.grid(row=r, column=0, columnspan=3, sticky="ew")
        ttk.Label(lv, text="Live mode:").pack(side="left", padx=6)
        ttk.Label(lv, text="phrase ends after silence (s)").pack(side="left", padx=(6, 2))
        ttk.Entry(lv, textvariable=self.v_live_pause, width=5).pack(side="left")
        ttk.Label(lv, text="max phrase (s)").pack(side="left", padx=(10, 2))
        ttk.Entry(lv, textvariable=self.v_live_max, width=5).pack(side="left")
        ttk.Label(lv, text="speech level").pack(side="left", padx=(10, 2))
        ttk.Entry(lv, textvariable=self.v_live_level, width=6).pack(side="left")
        r += 1
        ttk.Label(root, wraplength=700, foreground="gray", text=(
            "Live: each phrase is transcribed when you pause, and the caption track in FilmCraft is "
            "replaced with the full, updated set every time. A lower speech level is more sensitive. "
            "No MP3 is made; the live .srt is saved in the output folder.")
                  ).grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        r += 1
        rec = ttk.Frame(root)
        rec.grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        rec.columnconfigure(1, weight=1)
        self.btn_rec = ttk.Button(rec, text="●  Record", width=14, command=self.on_toggle)
        self.btn_rec.grid(row=0, column=0, padx=6)
        ttk.Progressbar(rec, variable=self.v_level, maximum=100).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Label(rec, textvariable=self.v_status, width=34, anchor="w").grid(
            row=0, column=2, padx=6)

        r += 1
        ttk.Separator(root).grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        r += 1
        ttk.Label(root, text="2. Transcribe (whisper-ctranslate2)",
                  font=("", 11, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", **pad)
        r += 1
        opts = ttk.Frame(root)
        opts.grid(row=r, column=0, columnspan=3, sticky="ew")
        opts.columnconfigure(7, weight=1)
        ttk.Label(opts, text="Model").grid(row=0, column=0, **pad)
        ttk.Combobox(opts, textvariable=self.v_model, width=10,
                     values=("tiny", "base", "small", "medium", "large-v2", "large-v3", "turbo")
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
        ttk.Separator(root).grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        r += 1
        ttk.Label(root, text="3. Import into FilmCraft", font=("", 11, "bold")).grid(
            row=r, column=0, columnspan=3, sticky="w", **pad)
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
        ttk.Button(conn, text="Test", command=self.on_test).grid(row=0, column=6, **pad)
        r += 1
        imp = ttk.Frame(root)
        imp.grid(row=r, column=0, columnspan=3, sticky="ew")
        ttk.Checkbutton(imp, text="Send to FilmCraft automatically",
                        variable=self.v_auto).pack(side="left", padx=6)
        ttk.Checkbutton(imp, text="Replace caption track C1",
                        variable=self.v_replace).pack(side="left", padx=6)
        ttk.Label(imp, text="Shift (s)").pack(side="left", padx=(12, 2))
        ttk.Entry(imp, textvariable=self.v_shift, width=7).pack(side="left")
        r += 1
        ttk.Label(root, text="Style JSON").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_style).grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1
        ttk.Label(root, text='Optional, sent to captions.setStyle, e.g. {"size": 48, "outline": 2}. Blank = leave the style alone.',
                  foreground="gray").grid(row=r, column=1, columnspan=2, sticky="w", padx=6)

        r += 1
        bar = ttk.Frame(root)
        bar.grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        self.btn_last = ttk.Button(bar, text="Import last SRT again",
                                   command=self.on_import_last, state="disabled")
        self.btn_last.pack(side="left", padx=6)
        ttk.Button(bar, text="Open output folder", command=self.on_open_folder).pack(side="left", padx=6)

        r += 1
        root.rowconfigure(r, weight=1)
        logf = ttk.Frame(root)
        logf.grid(row=r, column=0, columnspan=3, sticky="nsew")
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.txt = tk.Text(logf, wrap="word", height=12, state="disabled")
        sb = ttk.Scrollbar(logf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

    # ---- helpers ---------------------------------------------------------
    def log(self, msg):
        self.log_q.put(str(msg))

    def status(self, msg):
        self.after(0, lambda: self.v_status.set(msg))

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

    def _tick(self):
        if self.recording:
            self.v_level.set(min(100.0, (self.peak ** 0.5) * 100))
            if self.live_active:
                self.v_status.set(f"Live  {int(time.time() - self.rec_started)} s   "
                                  f"{len(self.live_caps)} captions")
            else:
                self.v_status.set(f"Recording...  {time.time() - self.rec_started:0.1f} s")
        else:
            self.v_level.set(0.0)
        self.after(80, self._tick)

    def _snapshot(self):
        """Read every setting on the main thread; worker threads use this copy."""
        return {
            "outdir": self.v_outdir.get().strip(), "exe": self.v_exe.get().strip(),
            "model": self.v_model.get().strip() or "medium",
            "device": self.v_device.get().strip() or "cuda",
            "language": self.v_lang.get().strip(), "extra": self.v_extra.get(),
            "split": self.v_split.get(), "pause": self.v_pause.get().strip(),
            "max_seconds": self.v_maxsec.get().strip(), "max_chars": self.v_maxchars.get().strip(),
            "vad": self.v_vad.get(), "mode": self.v_mode.get(),
            "live_pause": self.v_live_pause.get().strip(), "live_max": self.v_live_max.get().strip(),
            "live_level": self.v_live_level.get().strip(),
            "host": self.v_host.get().strip(), "port": self.v_port.get().strip(),
            "token": self.v_token.get().strip(), "auto": self.v_auto.get(),
            "replace": self.v_replace.get(), "shift": self.v_shift.get().strip(),
            "style": self.v_style.get().strip(),
        }

    def _save_cfg(self):
        data = {
            "input_device": self.v_dev.get(), "outdir": self.v_outdir.get(),
            "exe": self.v_exe.get(), "model": self.v_model.get(), "device": self.v_device.get(),
            "language": self.v_lang.get(), "extra": self.v_extra.get(),
            "split": self.v_split.get(), "pause": self.v_pause.get(),
            "max_seconds": self.v_maxsec.get(), "max_chars": self.v_maxchars.get(),
            "vad": self.v_vad.get(), "mode": self.v_mode.get(),
            "live_pause": self.v_live_pause.get(), "live_max": self.v_live_max.get(),
            "live_level": self.v_live_level.get(),
            "host": self.v_host.get(), "port": self.v_port.get(),
            "auto_import": self.v_auto.get(), "replace_c1": self.v_replace.get(),
            "shift": self.v_shift.get(), "style": self.v_style.get(),
        }  # the token is never written to disk
        try:
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        self._close_stream(abort=True)
        self._save_cfg()
        self.destroy()

    def _pick_outdir(self):
        p = filedialog.askdirectory(title="Folder for recordings and .srt files")
        if p:
            self.v_outdir.set(os.path.normpath(p))

    def _outdir(self, o):
        d = Path(o["outdir"]) if o["outdir"] else DEFAULT_OUT
        d.mkdir(parents=True, exist_ok=True)
        return d

    def on_open_folder(self):
        d = Path(self.v_outdir.get().strip() or DEFAULT_OUT)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # Windows
        except AttributeError:
            self.log(f"Output folder: {d}")

    def _refresh_devices(self, first=False):
        devs, hints = list_input_devices()
        for h in hints:
            self.log(h)
        self.device_map = dict(devs)
        names = [n for n, _ in devs]
        self.cb_dev.configure(values=names)
        if self.v_dev.get() not in self.device_map:
            self.v_dev.set(names[0])
        if not first:
            self.log(f"{len(names) - 1} audio source(s) found (microphones and speakers).")

    # ---- recording -------------------------------------------------------
    def on_toggle(self):
        if self.working:
            return
        if self.recording:
            if self.live_active:
                self._live_stop()
            else:
                self._stop_and_process()
        elif self._is_live():
            self._live_start()
        else:
            self._start_recording()

    def _peak_of(self, b):
        a = array("h")
        a.frombytes(b[: len(b) - (len(b) % 2)])
        return (max(map(abs, a)) / 32768.0) if len(a) else 0.0

    def _start_recording(self):
        kind, ref = self.device_map.get(self.v_dev.get(), ("mic", None))
        self.chunks = []
        self.peak = 0.0
        try:
            if kind == "loop":
                import pyaudiowpatch as pa
                self.rec_kind = "loop"
                self.rec_sr = int(ref["defaultSampleRate"])
                self.rec_channels = int(ref["maxInputChannels"]) or 2
                bytes_per_frame = 2 * self.rec_channels
                t0 = time.monotonic()
                state = {"frames": 0}

                def loop_cb(in_data, frame_count, time_info, status):
                    # WASAPI loopback sends nothing while the speakers are silent, which
                    # would squeeze the timeline. Fill such gaps with silence.
                    missing = int((time.monotonic() - t0) * self.rec_sr) - state["frames"] - frame_count
                    if missing > 0.25 * self.rec_sr:
                        self._feed(b"\x00" * (missing * bytes_per_frame))
                        state["frames"] += missing
                    self._feed(in_data)
                    state["frames"] += frame_count
                    return (None, pa.paContinue)

                self.pa = pa.PyAudio()
                self.stream = self.pa.open(format=pa.paInt16, channels=self.rec_channels,
                                           rate=self.rec_sr, frames_per_buffer=2048, input=True,
                                           input_device_index=ref["index"], stream_callback=loop_cb)
                self.stream.start_stream()
                what = f"speakers: {ref['name'].replace(' [Loopback]', '')}"
                hint = "Play the audio now; press Stop when done."
            else:
                import sounddevice as sd
                self.rec_kind = "mic"
                info = sd.query_devices(ref, "input")
                self.rec_sr = int(info["default_samplerate"])
                self.rec_channels = 1

                def mic_cb(indata, frames, t, status):
                    self._feed(bytes(indata))

                self.stream = sd.RawInputStream(samplerate=self.rec_sr, channels=1, dtype="int16",
                                                device=ref, callback=mic_cb)
                self.stream.start()
                what = f"microphone: {info['name']}"
                hint = "Speak now; press Stop when done."
        except ImportError as exc:
            self.log(f"A needed package is missing ({exc}). In this window run:  "
                     "pip install sounddevice lameenc PyAudioWPatch")
            self._close_stream(abort=True)
            return
        except Exception as exc:
            self.log(f"Could not open the audio source: {exc}")
            self._close_stream(abort=True)
            return
        self.recording = True
        self.rec_started = time.time()
        self.btn_rec.configure(text="■  Stop")
        self.log(f"Recording from {what} at {self.rec_sr} Hz, {self.rec_channels} ch. {hint}")

    def _close_stream(self, abort=False):
        s_, self.stream = self.stream, None
        pa_, self.pa = self.pa, None
        try:
            if s_ is not None:
                if self.rec_kind == "loop":
                    s_.stop_stream()
                    s_.close()
                else:
                    (s_.abort if abort else s_.stop)()
                    s_.close()
        except Exception as exc:
            self.log(f"Warning while stopping the audio: {exc}")
        if pa_ is not None:
            try:
                pa_.terminate()
            except Exception:
                pass

    def _stop_and_process(self):
        self.recording = False
        self._close_stream()
        pcm = b"".join(self.chunks)
        self.chunks = []
        sr, ch = self.rec_sr, self.rec_channels
        self.btn_rec.configure(text="●  Record")
        secs = len(pcm) / 2 / ch / sr
        if secs < MIN_SECONDS:
            self.log(f"That was only {secs:0.1f} s - nothing saved. Try again.")
            self.v_status.set("Ready.")
            return
        self._save_cfg()
        opts = self._snapshot()
        self.working = True
        self.btn_rec.configure(state="disabled")
        self.v_status.set("Working...")

        def work():
            try:
                self._pipeline(pcm, sr, ch, opts)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.working = False
                self.after(0, lambda: (self.btn_rec.configure(state="normal"),
                                       self.v_status.set("Ready.")))
        threading.Thread(target=work, daemon=True).start()

    # ---- live (real-time) mode -------------------------------------------
    def _is_live(self):
        return self.v_mode.get() == MODE_LIVE

    def _on_mode(self, _event=None):
        if not self.recording and not self.working:
            self.btn_rec.configure(text="●  Start live" if self._is_live() else "●  Record")

    def _feed(self, b):
        """Raw 16-bit PCM from the audio thread: to the live queue, or into the recording."""
        self.peak = self._peak_of(b)
        if self.live_active:
            try:
                self.live_q.put_nowait(b)
            except queue.Full:
                pass
        else:
            self.chunks.append(b)

    def _live_reset_ui(self):
        self.btn_rec.configure(state="normal")
        self._on_mode()
        self.v_status.set("Ready.")

    def _live_start(self):
        o = self._snapshot()
        try:
            float(o["live_pause"]), float(o["live_max"]), float(o["live_level"])
            float(o["shift"] or 0)
            float(o["pause"] or 0.5), float(o["max_seconds"] or 6), float(o["max_chars"] or 50)
            if o["style"]:
                json.loads(o["style"])
        except ValueError as exc:
            self.log(f"Check the Live, Pause, Shift and Style fields: {exc}")
            return
        try:
            import numpy  # noqa: F401
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            self.log(f"Live mode needs numpy and faster-whisper ({exc}). whisper-ctranslate2 normally "
                     "brings both; otherwise run:  pip install faster-whisper")
            return
        self._save_cfg()
        self.working = True
        self.btn_rec.configure(state="disabled")
        self.v_status.set("Loading model...")

        def work():
            try:
                from faster_whisper import WhisperModel
                self.log(f"Loading '{o['model']}' on {o['device']} (the first use downloads the model) ...")
                model = WhisperModel(o["model"], device=o["device"], compute_type="auto")
            except Exception as exc:
                self.log(f"Could not load the model: {exc}")
                self.working = False
                self.after(0, self._live_reset_ui)
                return
            self.after(0, lambda: self._live_begin(model, o))
        threading.Thread(target=work, daemon=True).start()

    def _live_begin(self, model, o):
        outdir = self._outdir(o)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.live_srt = outdir / f"live_{stamp}.srt"
        self.live_caps = []
        self.live_lock = threading.Lock()
        self.live_q = queue.Queue(maxsize=4000)
        self.utt_q = queue.Queue()
        self.push_dirty = threading.Event()
        self.live_done = threading.Event()
        self.live_active = True
        self._start_recording()
        if not self.recording:  # the audio source did not open; the reason is in the log
            self.live_active = False
            self.working = False
            self._live_reset_ui()
            return
        self.btn_rec.configure(text="■  Stop live", state="normal")
        sr, ch = self.rec_sr, self.rec_channels
        self.live_threads = [
            threading.Thread(target=self._live_guard, args=(self._live_audio_loop, o, sr, ch), daemon=True),
            threading.Thread(target=self._live_guard, args=(self._live_asr_loop, model, o), daemon=True),
        ]
        if o["auto"]:
            self.live_threads.append(
                threading.Thread(target=self._live_guard, args=(self._live_push_loop, o), daemon=True))
        for t in self.live_threads:
            t.start()
        self.working = False
        self.log(f"Live captions are being written to {self.live_srt}"
                 + (" and sent to FilmCraft as you speak." if o["auto"] else "."))

    def _live_guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:
            self.log(f"ERROR in live mode ({fn.__name__}): {exc}")

    def _live_stop(self):
        self.recording = False
        self._close_stream()
        self.live_active = False
        self.live_q.put(None)  # tells the audio loop to flush and finish
        self.working = True
        self.btn_rec.configure(state="disabled")
        self.v_status.set("Finishing...")
        threads = list(self.live_threads)

        def finish():
            for t in threads[:2]:  # audio loop, then the transcriber (drains every phrase)
                t.join()
            self.live_done.set()
            self.push_dirty.set()
            for t in threads[2:]:
                t.join()
            n = len(self.live_caps)
            self.log(f"Live session finished: {n} captions. SRT: {self.live_srt}" if n
                     else "Live session finished: no speech was captured.")
            self.working = False
            self.after(0, self._live_reset_ui)
        threading.Thread(target=finish, daemon=True).start()

    def _live_audio_loop(self, o, sr, ch):
        """Cut the audio into phrases (silence = a pause), hand each to the transcriber."""
        import numpy as np
        pause_s, max_s = float(o["live_pause"]), float(o["live_max"])
        level = float(o["live_level"])
        preroll_s, tail_s, min_speech = 0.3, 0.25, 0.25
        energies = deque(maxlen=200)
        floor, n_blocks, t = 0.0, 0, 0.0
        pre, pre_dur = [], 0.0
        utt, utt_start, utt_len, silence, speech_t = [], 0.0, 0.0, 0.0, 0.0
        in_speech = False

        def emit():
            nonlocal utt, utt_len, silence, speech_t, in_speech, pre, pre_dur
            if utt and speech_t >= min_speech:
                arr = np.concatenate(utt)
                drop = int(max(0.0, silence - tail_s) * sr)  # trim most of the trailing silence
                if drop:
                    arr = arr[: max(1, len(arr) - drop)]
                self.utt_q.put((arr, sr, utt_start))
            utt, utt_len, silence, speech_t, in_speech = [], 0.0, 0.0, 0.0, False
            pre, pre_dur = [], 0.0

        try:
            while True:
                b = self.live_q.get()
                if b is None:
                    break
                b = b[: len(b) - (len(b) % (2 * ch))]
                if not b:
                    continue
                x = np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0
                if ch > 1:
                    x = x.reshape(-1, ch).mean(axis=1)
                dur = len(x) / sr
                e = float(np.sqrt(np.mean(x * x)))
                energies.append(e)
                n_blocks += 1
                if n_blocks % 20 == 1:
                    floor = float(np.percentile(energies, 10))  # background level
                speech = e > max(level, min(3.0 * floor, 0.08))
                if not in_speech:
                    pre.append(x)
                    pre_dur += dur
                    while len(pre) > 1 and pre_dur - len(pre[0]) / sr >= preroll_s:
                        pre_dur -= len(pre[0]) / sr
                        pre.pop(0)
                    if speech:
                        in_speech = True
                        utt = list(pre)
                        utt_len, silence, speech_t = pre_dur, 0.0, dur
                        utt_start = t + dur - pre_dur
                        pre, pre_dur = [], 0.0
                else:
                    utt.append(x)
                    utt_len += dur
                    if speech:
                        silence, speech_t = 0.0, speech_t + dur
                    else:
                        silence += dur
                    if silence >= pause_s or utt_len >= max_s:
                        emit()
                t += dur
            if in_speech:
                emit()
        finally:
            self.utt_q.put(None)

    def _live_asr_loop(self, model, o):
        pause = float(o["pause"] or 0.5)
        max_s = float(o["max_seconds"] or 6)
        max_c = int(float(o["max_chars"] or 50))
        while True:
            item = self.utt_q.get()
            if item is None:
                break
            arr, sr, start = item
            try:
                audio = resample_16k(arr, sr)
                segs, _info = model.transcribe(audio, language=o["language"] or None,
                                               vad_filter=bool(o["vad"]), word_timestamps=True,
                                               condition_on_previous_text=False)
                seg_list = []
                for sg in segs:
                    if getattr(sg, "no_speech_prob", 0.0) > 0.6 and getattr(sg, "avg_logprob", 0.0) < -1.0:
                        continue  # most likely silence or noise, not speech
                    words = ([{"word": w.word, "start": w.start, "end": w.end} for w in (sg.words or [])]
                             if o["split"] else [])
                    seg_list.append({"start": sg.start, "end": sg.end, "text": sg.text, "words": words})
                caps, _ = captions_from_data({"segments": seg_list}, pause, max_s, max_c)
            except Exception as exc:
                self.log(f"Transcription error: {exc}")
                continue
            if not caps:
                continue
            caps = [(a_ + start, b_ + start, txt) for a_, b_, txt in caps]
            with self.live_lock:
                self.live_caps.extend(caps)
            self._live_write_srt()
            for a_, _b, txt in caps:
                self.log(f"[{_ts(a_)[:8]}] {txt}")
            self.push_dirty.set()

    def _live_write_srt(self):
        with self.live_lock:
            text = captions_to_srt(self.live_caps)
        try:
            self.live_srt.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.log(f"Could not write {self.live_srt}: {exc}")
        self.last_srt = self.live_srt
        self.after(0, lambda: self.btn_last.configure(state="normal"))

    def _live_push_loop(self, o):
        """Keeps FilmCraft up to date; bursts of new captions are merged into one update."""
        state = {"track": None, "first": True}
        last = 0.0
        while True:
            self.push_dirty.wait()
            self.push_dirty.clear()
            wait = 1.0 - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            try:
                self._live_push_once(o, state)
            except (RuntimeError, OSError) as exc:
                self.log(f"FilmCraft update failed: {exc}")
            last = time.time()
            if self.live_done.is_set() and not self.push_dirty.is_set():
                break

    def _live_push_once(self, o, state):
        with self.live_lock:
            caps = list(self.live_caps)
        shift = float(o["shift"] or 0)
        shifted = [(max(0.0, a_ + shift), b_ + shift, txt) for a_, b_, txt in caps if b_ + shift > 0]
        if not shifted or state.get("sent") == len(shifted):
            return  # nothing new since the last update
        tmp = self.live_srt.with_suffix(".push.srt")
        tmp.write_text(captions_to_srt(shifted), encoding="utf-8")
        if state["track"]:
            try:
                fc_run(o, "captions.deleteTrack", track=state["track"])
            except RuntimeError as exc:
                self.log(f"(could not remove the previous live track: {exc})")
        elif state["first"] and o["replace"]:
            try:
                fc_run(o, "captions.deleteTrack", track="C1")
                self.log("Removed the existing C1 caption track.")
            except RuntimeError:
                pass
        res = fc_run(o, "captions.import", path=str(tmp.resolve()), name=self.live_srt.stem)
        track = "C1"
        if isinstance(res, dict) and res.get("track") not in (None, ""):
            track = res["track"]
        state["track"], state["first"], state["sent"] = track, False, len(shifted)
        if o["style"]:
            fc_run(o, "captions.setStyle", track=track, **json.loads(o["style"]))
        self.log(f"FilmCraft updated: {len(shifted)} captions on track {track}.")

    # ---- pipeline --------------------------------------------------------
    def _pipeline(self, pcm, sr, ch, o):
        outdir = self._outdir(o)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.status("Saving MP3...")
        audio = save_mp3(pcm, sr, outdir / f"dictation_{stamp}.mp3", self.log, ch)
        self.log(f"Saved {audio}  ({len(pcm) / 2 / ch / sr:0.1f} s of audio)")

        self.status("Transcribing...")
        srt = self._transcribe(audio, outdir, o)
        if not srt:
            return
        self.last_srt = srt
        self.after(0, lambda: self.btn_last.configure(state="normal"))
        if o["auto"]:
            self.status("Importing into FilmCraft...")
            self._import(srt, o)
        else:
            self.log("Auto-import is off. Use 'Import last SRT again' when you are ready.")

    def _transcribe(self, audio, outdir, o):
        name = o["exe"] or "whisper-ctranslate2"
        exe = shutil.which(name) or (name if Path(name).exists() else None)
        if not exe:
            self.log(f"Cannot find '{name}'. Start from the FILMCRAFT.bat window (Anaconda base) "
                     "or enter its full path in 'Program'.")
            return None
        try:
            extra = shlex.split(o["extra"], posix=(os.name != "nt"))
        except ValueError as exc:
            self.log(f"Extra arguments could not be read: {exc}")
            return None
        try:
            pause = float(o["pause"] or 0.5)
            max_s = float(o["max_seconds"] or 6)
            max_c = int(float(o["max_chars"] or 50))
        except ValueError:
            self.log("Pause, Max secs and Max chars must be numbers.")
            return None
        fmt = "json" if o["split"] else "srt"
        cmd = [exe, str(audio), "--model", o["model"], "--device", o["device"],
               "--output_format", fmt, "--output_dir", str(outdir)]
        if o["split"]:
            cmd += ["--word_timestamps", "True"]
        if o["vad"]:
            cmd += ["--vad_filter", "True"]
        if o["language"]:
            cmd += ["--language", o["language"]]
        cmd += extra
        self.log("> " + subprocess.list2cmdline(cmd))
        t0 = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.Popen(
            cmd, cwd=str(outdir), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.log(line)
        code = proc.wait()
        srt = Path(audio).with_suffix(".srt")
        if code != 0:
            self.log(f"whisper-ctranslate2 exited with code {code}.")
        if o["split"]:
            js = Path(audio).with_suffix(".json")
            if not js.exists() or js.stat().st_mtime < t0 - 2:
                self.log("No .json came back from whisper-ctranslate2. Check the messages above.")
                return None
            try:
                text, n, used_words = words_to_srt(json.loads(js.read_text(encoding="utf-8")),
                                                   pause, max_s, max_c)
            except (ValueError, KeyError, TypeError) as exc:
                self.log(f"Could not read the word timings in {js.name}: {exc}")
                return None
            if not n:
                self.log("Whisper heard no speech, so there are no captions.")
                return None
            srt.write_text(text, encoding="utf-8")
            self.log(f"Split into {n} captions on pauses >= {pause:g} s"
                     + ("" if used_words else " (no word timings found, used Whisper's own segments)"))
        if not srt.exists() or srt.stat().st_mtime < t0 - 2:
            self.log("No .srt was produced. Check the messages above.")
            return None
        self.log(f"SRT written: {srt}")
        return srt

    def _import(self, srt, o):
        """captions.import (+ optional replace, shift and style), as filmcraft_subtitles_gui does."""
        try:
            shift_s = float(o["shift"] or 0)
            style = json.loads(o["style"]) if o["style"] else None
            if style is not None and not isinstance(style, dict):
                raise ValueError("Style JSON must be an object like {\"size\": 48}")
        except ValueError as exc:  # json errors are ValueErrors too
            self.log(f"Check the Shift / Style JSON fields: {exc}")
            return False
        try:
            track = "C1"
            if o["replace"]:
                try:
                    fc_run(o, "captions.deleteTrack", track="C1")
                    self.log("Removed the existing C1 caption track.")
                except RuntimeError as exc:
                    self.log(f"No existing C1 track to replace ({exc}).")
            self.log(f"Importing {Path(srt).name} ...")
            res = fc_run(o, "captions.import", path=str(Path(srt).resolve()), name=Path(srt).stem)
            self.log("  " + json.dumps(res, separators=(",", ":"))[:300])
            if isinstance(res, dict) and res.get("track") not in (None, ""):
                track = res["track"]

            if shift_s:
                seq = fc_run(o, "sequence.inspect")
                fr = (seq or {}).get("settings", {}).get("frame_rate", {})
                fps = fr.get("num", 25) / float(fr.get("den", 1) or 1)
                lst = fc_run(o, "captions.list", track=track)
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
                    fc_run(o, "captions.move", captions=ids, deltaFrames=int(round(shift_s * fps)))
                    self.log(f"Shifted {len(ids)} captions by {shift_s:+g} s.")
                else:
                    self.log("Could not find caption ids to shift; captions.list said: "
                             + json.dumps(lst)[:300])
            if style:
                res = fc_run(o, "captions.setStyle", track=track, **style)
                self.log(f"Styled track {track}: " + json.dumps(res, separators=(",", ":"))[:200])
            self.log("Done - the captions are in FilmCraft.")
            return True
        except RuntimeError as exc:
            self.log(f"FilmCraft refused it: {exc}")
            self.log("Is a sequence open in FilmCraft? Is the token correct?")
        except OSError as exc:
            self.log(f"Could not reach FilmCraft on {o['host']}:{o['port']}: {exc}")
        return False

    # ---- buttons ---------------------------------------------------------
    def _bg(self, fn, *args):
        def work():
            try:
                fn(*args)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def on_import_last(self):
        if not self.last_srt or not Path(self.last_srt).exists():
            self.log("No SRT from this session yet. Record something first.")
            return
        self._bg(self._import, self.last_srt, self._snapshot())

    def on_test(self):
        o = self._snapshot()

        def work():
            try:
                reply = fc_call(o["host"], o["port"], o["token"], "engine.commands", timeout=15)
                if reply.get("ok"):
                    self.log("Connected to FilmCraft.")
                    caps = [i for i in command_ids(reply.get("result")) if "caption" in i.lower()]
                    if caps:
                        self.log("Caption commands available: " + ", ".join(caps))
                else:
                    self.log(f"FilmCraft replied: {reply.get('error')}")
            except OSError as exc:
                self.log(f"Could not reach FilmCraft on {o['host']}:{o['port']}: {exc}")
        self._bg(work)


if __name__ == "__main__":
    App().mainloop()
