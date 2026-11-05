"""FilmCraft_Dictation_SRT.py - speak, get captions in FilmCraft.

  1. Records your microphone.            (sounddevice)
  2. Saves the recording as an MP3.      (lameenc, or ffmpeg if lameenc is missing)
  3. Transcribes it to an .srt.          (whisper-ctranslate2, GPU with --device cuda)
  4. Imports the .srt into FilmCraft     (captions.import over the control channel)
     as a caption track, optionally shifted and styled.

One-time setup, in the Anaconda (base) window that FILMCRAFT.bat opens:

    pip install sounddevice lameenc

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
from pathlib import Path
from tkinter import ttk, filedialog

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "FilmCraft_Dictation_SRT.json"
DEFAULT_OUT = HERE / "FilmCraft_Dictation"
MIN_SECONDS = 0.5

DEFAULTS = {
    "input_device": "System default",
    "outdir": "",                      # empty = <script folder>\FilmCraft_Dictation
    "exe": "whisper-ctranslate2",
    "model": "medium",
    "device": "cuda",
    "language": "",                    # empty = auto-detect
    "extra": "",
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
def write_wav(path, pcm, sr):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def save_mp3(pcm, sr, mp3_path, log):
    """16-bit mono PCM -> MP3. Returns the file actually written (WAV as a last resort)."""
    try:
        import lameenc
        enc = lameenc.Encoder()
        if hasattr(enc, "silence"):
            enc.silence()
        enc.set_bit_rate(96)
        enc.set_in_sample_rate(sr)
        enc.set_channels(1)
        enc.set_quality(2)
        data = bytes(enc.encode(pcm)) + bytes(enc.flush())
        mp3_path.write_bytes(data)
        return mp3_path
    except ImportError:
        log("lameenc is not installed (pip install lameenc); trying ffmpeg ...")
    except Exception as exc:
        log(f"lameenc failed ({exc}); trying ffmpeg ...")

    wav = mp3_path.with_suffix(".wav")
    write_wav(wav, pcm, sr)
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


def list_input_devices():
    """[(label, device_index_or_None)] for the system default plus real inputs."""
    import sounddevice as sd
    out = [("System default", None)]
    try:
        host_api = sd.query_devices(kind="input")["hostapi"]
    except Exception:
        host_api = None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and (host_api is None or d["hostapi"] == host_api):
            out.append((f"{i}: {d['name']}", i))
    return out


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FilmCraft Dictation to SRT")
        self.geometry("780x700")
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
        self.rec_started = 0.0
        self.last_srt = None
        self.device_map = {"System default": None}

        c = self.cfg
        self.v_dev = tk.StringVar(value=c["input_device"])
        self.v_outdir = tk.StringVar(value=c["outdir"])
        self.v_exe = tk.StringVar(value=c["exe"])
        self.v_model = tk.StringVar(value=c["model"])
        self.v_device = tk.StringVar(value=c["device"])
        self.v_lang = tk.StringVar(value=c["language"])
        self.v_extra = tk.StringVar(value=c["extra"])
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
        ttk.Label(root, text="Microphone").grid(row=r, column=0, sticky="w", **pad)
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
        ttk.Checkbutton(imp, text="Import automatically after transcribing",
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
            "host": self.v_host.get(), "port": self.v_port.get(),
            "auto_import": self.v_auto.get(), "replace_c1": self.v_replace.get(),
            "shift": self.v_shift.get(), "style": self.v_style.get(),
        }  # the token is never written to disk
        try:
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        if self.stream is not None:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception:
                pass
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
        try:
            devs = list_input_devices()
        except ImportError:
            self.log("sounddevice is not installed. In this window run:  pip install sounddevice lameenc")
            return
        except Exception as exc:
            self.log(f"Could not list microphones: {exc}")
            return
        self.device_map = dict(devs)
        names = [n for n, _ in devs]
        self.cb_dev.configure(values=names)
        if self.v_dev.get() not in self.device_map:
            self.v_dev.set("System default")
        if not first:
            self.log(f"{len(names) - 1} microphone(s) found.")

    # ---- recording -------------------------------------------------------
    def on_toggle(self):
        if self.working:
            return
        if self.recording:
            self._stop_and_process()
        else:
            self._start_recording()

    def _start_recording(self):
        try:
            import sounddevice as sd
        except ImportError:
            self.log("sounddevice is not installed. In this window run:  pip install sounddevice lameenc")
            return
        dev = self.device_map.get(self.v_dev.get())
        try:
            info = sd.query_devices(dev, "input")
            self.rec_sr = int(info["default_samplerate"])
            self.chunks = []
            self.peak = 0.0

            def callback(indata, frames, t, status):
                b = bytes(indata)
                self.chunks.append(b)
                a = array("h")
                a.frombytes(b)
                self.peak = (max(map(abs, a)) / 32768.0) if len(a) else 0.0

            self.stream = sd.RawInputStream(samplerate=self.rec_sr, channels=1, dtype="int16",
                                            device=dev, callback=callback)
            self.stream.start()
        except Exception as exc:
            self.log(f"Could not open the microphone: {exc}")
            self.stream = None
            return
        self.recording = True
        self.rec_started = time.time()
        self.btn_rec.configure(text="■  Stop")
        self.log(f"Recording from: {info['name']} at {self.rec_sr} Hz. Speak now; press Stop when done.")

    def _stop_and_process(self):
        self.recording = False
        try:
            self.stream.stop()
            self.stream.close()
        except Exception as exc:
            self.log(f"Warning while stopping the microphone: {exc}")
        self.stream = None
        pcm = b"".join(self.chunks)
        self.chunks = []
        self.btn_rec.configure(text="●  Record")
        secs = len(pcm) / 2 / self.rec_sr
        if secs < MIN_SECONDS:
            self.log(f"That was only {secs:0.1f} s - nothing saved. Try again.")
            self.v_status.set("Ready.")
            return
        self._save_cfg()
        opts = self._snapshot()
        sr = self.rec_sr
        self.working = True
        self.btn_rec.configure(state="disabled")
        self.v_status.set("Working...")

        def work():
            try:
                self._pipeline(pcm, sr, opts)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.working = False
                self.after(0, lambda: (self.btn_rec.configure(state="normal"),
                                       self.v_status.set("Ready.")))
        threading.Thread(target=work, daemon=True).start()

    # ---- pipeline --------------------------------------------------------
    def _pipeline(self, pcm, sr, o):
        outdir = self._outdir(o)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.status("Saving MP3...")
        audio = save_mp3(pcm, sr, outdir / f"dictation_{stamp}.mp3", self.log)
        self.log(f"Saved {audio}  ({len(pcm) / 2 / sr:0.1f} s of speech)")

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
        cmd = [exe, str(audio), "--model", o["model"], "--device", o["device"],
               "--output_format", "srt", "--output_dir", str(outdir)]
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
                self.log("Connected to FilmCraft." if reply.get("ok")
                         else f"FilmCraft replied: {reply.get('error')}")
            except OSError as exc:
                self.log(f"Could not reach FilmCraft on {o['host']}:{o['port']}: {exc}")
        self._bg(work)


if __name__ == "__main__":
    App().mainloop()
