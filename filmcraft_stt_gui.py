"""FilmCraft Speech-to-Text bridge.

1. Transcribes an audio/video file (or every media file in a folder) to .srt with
   whisper-ctranslate2 (faster-whisper CLI), on the GPU if you choose cuda.
2. Sends the .srt to the running FilmCraft app over the control channel.

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
from tkinter import filedialog, ttk

CONFIG_FILE = Path(__file__).with_name("filmcraft_stt_gui.json")
MEDIA_EXTS = ("mp4", "mov", "mkv", "wav", "mp3", "m4a")
CAPTION_WORDS = ("srt", "caption", "subtitle", "transcri")

DEFAULTS = {
    "exe": "whisper-ctranslate2",
    "model": "medium",
    "device": "cuda",
    "language": "",          # empty = auto-detect
    "extra": "",             # extra whisper-ctranslate2 arguments, appended as-is
    "media": "",
    "outdir": "",            # empty = <media folder>\FilmCraft_Captions
    "srt": "",
    "host": "127.0.0.1",
    "port": os.environ.get("FILMCRAFT_CONTROL_PORT", "9876"),
    "import_command": "",
    # {srt} is replaced with the SRT path; rename the key to match the command's param.
    "import_params": '{"path": "{srt}"}',
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


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FilmCraft - Speech to Text")
        self.geometry("820x700")
        self.cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except Exception:
                pass

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
        self.v_media = tk.StringVar(value=c["media"])
        self.v_outdir = tk.StringVar(value=c["outdir"])
        self.v_srt = tk.StringVar(value=c["srt"])
        self.v_host = tk.StringVar(value=c["host"])
        self.v_port = tk.StringVar(value=str(c["port"]))
        self.v_token = tk.StringVar(value=os.environ.get("FILMCRAFT_CONTROL_TOKEN", ""))
        self.v_cmd = tk.StringVar(value=c["import_command"])
        self.v_params = tk.StringVar(value=c["import_params"])

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
        self.cb_cmd = ttk.Combobox(root, textvariable=self.v_cmd)
        self.cb_cmd.grid(row=r, column=1, sticky="ew", **pad)
        self.cb_cmd.bind("<<ComboboxSelected>>", self._on_cmd_selected)
        ttk.Button(root, text="Find caption commands", command=self.on_find).grid(row=r, column=2, **pad)
        r += 1
        ttk.Label(root, text="Command params").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_params).grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        r += 1
        ttk.Label(root, text="JSON; {srt} becomes the SRT path. Rename the key to match the command's param.",
                  foreground="gray").grid(row=r, column=1, columnspan=2, sticky="w", padx=6)

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

    def _send_work(self, srt=None):
        srt = str(srt or self.v_srt.get()).strip()
        if not srt or not Path(srt).exists():
            self.log("No SRT file yet. Transcribe first, or Browse to an existing .srt.")
            return False
        cmd_id = self.v_cmd.get().strip()
        if not cmd_id:
            self.log("Click 'Find caption commands' and choose the import command first.")
            return False
        try:
            params = fill(json.loads(self.v_params.get() or "{}"), {"srt": str(Path(srt).resolve())})
        except json.JSONDecodeError as exc:
            self.log(f"Command params is not valid JSON: {exc}")
            return False
        host, port, token = self._conn()
        self.log(f"Sending to FilmCraft: {cmd_id} {json.dumps(params)}")
        reply = fc_call(host, port, token, "engine.execute", {"command": cmd_id, "params": params})
        if reply.get("ok"):
            self.log(f"FilmCraft: OK {json.dumps(reply.get('result'))[:500]}")
            return True
        self.log(f"FilmCraft refused it: {reply.get('error')}")
        self.log("Check the command's params (see 'Find caption commands') and that a sequence is open.")
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

        made = []
        for i, f in enumerate(files, 1):
            cmd = [exe, str(f), "--model", model, "--device", device,
                   "--output_format", "srt", "--output_dir", str(outdir)]
            if lang:
                cmd += ["--language", lang]
            cmd += extra
            self.log(f"[{i}/{len(files)}] {subprocess.list2cmdline(cmd)}")
            t0 = time.time()
            if self._run(cmd, folder) is None:  # stopped
                return None
            srt = outdir / (f.stem + ".srt")
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
