#!/usr/bin/env python3
"""
filmcraft_slideshow_gui.py - GUI: image folder -> FilmCraft slideshow.

Needs filmcraft_slideshow.py, filmcraft_apply_session.py and filmcraft_title.py
in the same folder.

  1. Start FilmCraft with a token (FILMCRAFT.bat does this), then run this
     from a prompt that has FILMCRAFT_CONTROL_TOKEN set (or paste the token).
  2. Pick the image folder. If it holds a *.still2video_session.json, that
     file is used: its image order, seconds per image, crossfade length,
     per-image durations, rotation/crop, title screen and music (found by file
     name in the same folder, else at the path stored in the file).
  3. Check the list and settings, press Build.

Untick "Use session file" to build from the folder's images in name order.
"""
import argparse
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from filmcraft_slideshow import (Client, EXTS, natural_key, resolve_items,
                                 find_clip_ids, short)
from filmcraft_apply_session import (read_session, image_name,
                                     apply_to_sequence)
from filmcraft_title import add_title


def find_session(folder):
    try:
        for n in sorted(os.listdir(folder)):
            if n.lower().endswith("still2video_session.json"):
                return os.path.join(folder, n)
    except OSError:
        pass
    return None


def tfloat(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def resolve_image(folder, im):
    """The image in the chosen folder (by file name), else at its JSON path."""
    for p in (os.path.join(folder, image_name(im["path"])),
              im["path"].replace("\\", "/")):
        if os.path.isfile(p):
            return p
    return None


def resolve_music(folder, settings):
    mp = settings.get("music_path") or ""
    if not mp:
        return None
    for p in (os.path.join(folder, image_name(mp)), mp.replace("\\", "/")):
        if os.path.isfile(p):
            return os.path.normpath(p)
    return None


def run_gui(port, token):
    root = tk.Tk()
    root.title("FilmCraft Slideshow")
    root.minsize(660, 700)
    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(5, weight=1)
    frm.rowconfigure(8, weight=1)

    V = {k: tk.StringVar(value=v) for k, v in dict(
        folder="", seconds="4", dissolve="15", width="1920", height="1080",
        fps="25", name="Slideshow", port=str(port), token=token or "").items()}
    use_current = tk.BooleanVar(value=False)
    fit = tk.BooleanVar(value=True)
    show_tok = tk.BooleanVar(value=False)
    use_sess = tk.BooleanVar(value=False)
    do_crop = tk.BooleanVar(value=True)
    do_music = tk.BooleanVar(value=True)
    do_title = tk.BooleanVar(value=True)
    info = tk.StringVar()
    sess_info = tk.StringVar()
    q = queue.Queue()
    # names/paths: what will be imported, in order. durations: name -> sec
    state = {"names": [], "paths": [], "durations": {}, "busy": False,
             "session": None, "imgs": {}, "music": None, "missing": [],
             "title": None}

    # ---- folder -----------------------------------------------------------
    ttk.Label(frm, text="Image folder").grid(row=0, column=0, sticky="w")
    ttk.Entry(frm, textvariable=V["folder"]).grid(row=0, column=1,
                                                  sticky="ew", padx=6)
    ttk.Button(frm, text="Browse...", command=lambda: browse()).grid(
        row=0, column=2)

    # ---- session ----------------------------------------------------------
    se = ttk.LabelFrame(frm, text="Session file", padding=8)
    se.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    se.columnconfigure(0, weight=1)
    ttk.Label(se, textvariable=sess_info, wraplength=600,
              justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
    c_use = ttk.Checkbutton(se, text="Use session file", variable=use_sess,
                            command=lambda: on_toggle())
    c_use.grid(row=1, column=0, sticky="w", pady=(4, 0))
    c_crop = ttk.Checkbutton(se, text="Apply rotation / crop",
                             variable=do_crop)
    c_crop.grid(row=1, column=1, sticky="w", pady=(4, 0), padx=10)
    c_music = ttk.Checkbutton(se, text="Add music", variable=do_music)
    c_music.grid(row=1, column=2, sticky="w", pady=(4, 0))
    c_title = ttk.Checkbutton(se, text="Add title screen", variable=do_title)
    c_title.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))

    # ---- settings ---------------------------------------------------------
    st = ttk.LabelFrame(frm, text="Settings", padding=8)
    st.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))

    def field(r, c, label, key, w=8):
        ttk.Label(st, text=label).grid(row=r, column=c * 2, sticky="w",
                                       padx=(0, 4), pady=2)
        e = ttk.Entry(st, textvariable=V[key], width=w)
        e.grid(row=r, column=c * 2 + 1, sticky="w", padx=(0, 14))
        V[key].trace_add("write", lambda *_: update_info())
        return e

    field(0, 0, "Seconds per image", "seconds")
    field(0, 1, "Dissolve (frames)", "dissolve")
    field(0, 2, "Frame rate", "fps")
    field(1, 0, "Width", "width")
    field(1, 1, "Height", "height")
    field(1, 2, "Sequence name", "name", 14)
    ttk.Checkbutton(st, text="Use current sequence instead of making one",
                    variable=use_current).grid(row=2, column=0, columnspan=4,
                                               sticky="w", pady=(6, 0))
    ttk.Checkbutton(st, text="Scale images to frame", variable=fit).grid(
        row=2, column=4, columnspan=2, sticky="w", pady=(6, 0))

    # ---- connection -------------------------------------------------------
    cn = ttk.Frame(frm)
    cn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    cn.columnconfigure(3, weight=1)
    ttk.Label(cn, text="Port").grid(row=0, column=0)
    ttk.Entry(cn, textvariable=V["port"], width=7).grid(row=0, column=1,
                                                        padx=(4, 14))
    ttk.Label(cn, text="Token").grid(row=0, column=2)
    tok = ttk.Entry(cn, textvariable=V["token"], show="\u2022")
    tok.grid(row=0, column=3, sticky="ew", padx=4)
    ttk.Checkbutton(cn, text="show", variable=show_tok,
                    command=lambda: tok.configure(
                        show="" if show_tok.get() else "\u2022")
                    ).grid(row=0, column=4)

    ttk.Label(frm, textvariable=info, foreground="#444").grid(
        row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ---- file list --------------------------------------------------------
    lf = ttk.Frame(frm)
    lf.grid(row=5, column=0, columnspan=3, sticky="nsew")
    lf.columnconfigure(0, weight=1)
    lf.rowconfigure(0, weight=1)
    lb = tk.Listbox(lf, height=8, activestyle="none")
    lb.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(lf, command=lb.yview)
    sb.grid(row=0, column=1, sticky="ns")
    lb.configure(yscrollcommand=sb.set)

    # ---- buttons / log ----------------------------------------------------
    bt = ttk.Frame(frm)
    bt.grid(row=6, column=0, columnspan=3, sticky="e", pady=8)
    build_btn = ttk.Button(bt, text="Build slideshow",
                           command=lambda: start())
    build_btn.grid(row=0, column=0, padx=3)
    ttk.Button(bt, text="Close", command=root.destroy).grid(row=0, column=1,
                                                            padx=3)
    ttk.Label(frm, text="Log").grid(row=7, column=0, sticky="w")
    logb = tk.Text(frm, height=10, wrap="word", state="disabled",
                   font=("TkFixedFont", 9))
    logb.grid(row=8, column=0, columnspan=3, sticky="nsew")

    def log(msg):
        logb.configure(state="normal")
        logb.insert("end", msg + "\n")
        logb.see("end")
        logb.configure(state="disabled")

    # ---- helpers ----------------------------------------------------------
    def num(key, cast, default=None):
        try:
            return cast(V[key].get().strip())
        except ValueError:
            return default

    def frames_for(sec, fps):
        return max(1, round(sec * fps))

    def total_seconds(sec, dis, fps):
        n = len(state["names"])
        still = frames_for(sec, fps)
        extra = sum(frames_for(d, fps) - still
                    for d in state["durations"].values())
        title = (float(state["title"].get("duration", 4.0))
                 if state["title"] and use_sess.get() and do_title.get()
                 else 0.0)
        return (still * n + extra - dis * (n - 1)) / fps + title

    def update_info(*_):
        sec, dis, fps = num("seconds", float), num("dissolve", int), \
            num("fps", float)
        n = len(state["names"])
        if not n:
            info.set("Choose a folder with images.")
        elif None in (sec, dis, fps) or fps <= 0 or sec <= 0:
            info.set("%d images. Enter numbers for seconds, dissolve, fps." % n)
        else:
            still = frames_for(sec, fps)
            if dis >= still or dis < 0:
                info.set("Dissolve must be 0 or shorter than one image "
                         "(%d frames)." % still)
            else:
                info.set("%d images, %d frames each, about %.1f seconds total."
                         % (n, still, total_seconds(sec, dis, fps)))

    def fill_list():
        lb.delete(0, "end")
        for i, n in enumerate(state["names"], 1):
            tag = ""
            if use_sess.get():
                im = state["imgs"].get(n.lower(), {})
                bits = []
                if im.get("rotation"):
                    bits.append("rot %s" % im["rotation"])
                if im.get("crop"):
                    bits.append("crop")
                if n in state["durations"]:
                    bits.append("%gs" % state["durations"][n])
                tag = "   [%s]" % ", ".join(bits) if bits else ""
            lb.insert("end", "%3d  %s%s" % (i, n, tag))

    def compute_names():
        folder = V["folder"].get().strip()
        state["names"], state["paths"] = [], []
        state["durations"], state["missing"] = {}, []
        if not os.path.isdir(folder):
            return
        if use_sess.get() and state["session"]:
            seen = set()
            for im in state["session"][1]:
                p = resolve_image(folder, im)
                nm = image_name(im["path"])
                if not p:
                    state["missing"].append(nm)
                    continue
                if nm.lower() in seen:
                    continue
                seen.add(nm.lower())
                state["names"].append(os.path.basename(p))
                state["paths"].append(os.path.normpath(p))
                if tfloat(im.get("duration"), 0) > 0:
                    state["durations"][os.path.basename(p)] = \
                        tfloat(im["duration"], 0)
        else:
            for n in sorted((n for n in os.listdir(folder)
                             if n.lower().endswith(EXTS)), key=natural_key):
                state["names"].append(n)
                state["paths"].append(os.path.join(folder, n))

    def apply_session_settings():
        stg = state["session"][0]
        sec = tfloat(stg.get("image_duration"), None)
        if sec:
            V["seconds"].set("%g" % sec)
        fps = num("fps", float, 25.0)
        ttype = str(stg.get("transition_type", "")).lower()
        if "cross" in ttype or "dissolve" in ttype:
            V["dissolve"].set(str(round(tfloat(stg.get(
                "transition_duration"), 0) * fps)))
        else:
            V["dissolve"].set("0")
            if ttype and ttype not in ("none", "cut"):
                log("Transition type '%s' is not supported; using cuts."
                    % stg.get("transition_type"))

    def refresh_view():
        compute_names()
        fill_list()
        update_info()

    def on_toggle():
        if use_sess.get() and state["session"]:
            apply_session_settings()
        refresh_view()

    def load_folder(*_):
        d = V["folder"].get().strip()
        state["session"], state["imgs"], state["music"] = None, {}, None
        state["title"] = None
        use_sess.set(False)
        c_title.configure(text="Add title screen")
        for c in (c_use, c_crop, c_music, c_title):
            c.state(["disabled"])
        sess_info.set("No session file in this folder.")
        if os.path.isdir(d):
            sp = find_session(d)
            if sp:
                try:
                    stg, images, imgs = read_session(sp)
                except (OSError, ValueError, KeyError) as e:
                    sess_info.set("Could not read %s: %s" % (
                        os.path.basename(sp), e))
                else:
                    state["session"] = (stg, images)
                    state["imgs"] = imgs
                    state["music"] = resolve_music(d, stg)
                    mp = stg.get("music_path")
                    sess_info.set("%s: %d images, %s s each, %s %s s. Music: %s"
                                  % (os.path.basename(sp), len(images),
                                     stg.get("image_duration", "?"),
                                     stg.get("transition_type", "no transition"),
                                     stg.get("transition_duration", ""),
                                     os.path.basename(state["music"])
                                     if state["music"] else
                                     ("NOT FOUND (%s)" % image_name(mp)
                                      if mp else "none")))
                    for c in (c_use, c_crop):
                        c.state(["!disabled"])
                    ts = stg.get("title_screen")
                    if ts and ts.get("enabled"):
                        state["title"] = ts
                        c_title.configure(text="Add title screen: %s (%g s)" % (
                            ts.get("title", ""), tfloat(ts.get("duration"), 4.0)))
                        c_title.state(["!disabled"])
                    else:
                        do_title.set(False)
                    if state["music"]:
                        c_music.state(["!disabled"])
                    else:
                        do_music.set(False)
                    use_sess.set(True)
                    apply_session_settings()
        refresh_view()
        if state["missing"]:
            log("%d session images not found in the folder, e.g. %s"
                % (len(state["missing"]), state["missing"][:3]))

    def browse():
        d = filedialog.askdirectory(initialdir=V["folder"].get() or None)
        if d:
            V["folder"].set(os.path.normpath(d))

    V["folder"].trace_add("write", load_folder)

    # ---- the build (worker thread; talks to the UI through a queue) -------
    def worker(cfg):
        say = lambda m: q.put(("log", m))
        try:
            names, paths = cfg["names"], cfg["paths"]
            cl = Client(cfg["port"], cfg["token"])
            say("importing %d images..." % len(names))
            imp = cl.run("file.import", paths=paths)
            say("    " + short(imp))
            say("finding the imported items...")
            tree = cl.run("project.items", recursive=True)
            item_ids, how = resolve_items(imp, tree, names)
            if not item_ids:
                say("    could not match the %d imported images (%s)"
                    % (len(names), how))
                say("    project.items reply: " + short(tree, 800))
                q.put(("done", False))
                return
            say("    %d items: %s" % (len(item_ids), how))
            if cfg["use_current"]:
                say("using the current sequence")
            else:
                say("creating sequence %dx%d @ %gfps..."
                    % (cfg["w"], cfg["h"], cfg["fps"]))
                say("    " + short(cl.run(
                    "file.newSequence", name=cfg["name"], width=cfg["w"],
                    height=cfg["h"], fps=cfg["fps"])))
            say("placing on the timeline...")
            say("    " + short(cl.run(
                "clip.automateToSequence", items=item_ids,
                ordering="selection", placement="sequentially",
                method="overwrite", stillFrames=cfg["still"],
                overlapFrames=cfg["dissolve"],
                videoTransition=bool(cfg["dissolve"]), ignoreAudio=True)))

            if cfg["title"]:
                try:
                    seq = cl.run("sequence.inspect")
                    sst = seq.get("settings", {})
                    fr = sst.get("frame_rate", {})
                    add_title(cl, cfg["title"], item_ids[0],
                              fr.get("num", cfg["fps"]) / float(fr.get("den", 1) or 1),
                              sst.get("width", cfg["w"]),
                              sst.get("height", cfg["h"]), cfg["dissolve"],
                              log=lambda m: say("    " + m))
                except RuntimeError as e:
                    say("    title stopped: %s" % e)

            if cfg["durations"]:           # per-image "duration" overrides
                say("per-image durations (%d)..." % len(cfg["durations"]))
                try:
                    seq = cl.run("sequence.inspect")
                    byname = {}
                    for tr in seq.get("video", []):
                        for it in tr.get("items", []):
                            byname[it.get("name", "").lower()] = it["clip"]
                    for nm, sec in cfg["durations"].items():
                        delta = max(1, round(sec * cfg["fps"])) - cfg["still"]
                        cid = byname.get(nm.lower())
                        if cid is None or delta == 0:
                            continue
                        cl.run("timeline.trim", clip=cid, edge="out",
                               mode="ripple", deltaFrames=delta)
                        say("    %s: %+d frames" % (nm, delta))
                except RuntimeError as e:
                    say("    durations skipped: %s" % e)

            if cfg["crop"]:
                say("applying rotation / crop from the session file...")
                try:
                    apply_to_sequence(cl, cfg["imgs"], log=lambda m: say("    " + m))
                except RuntimeError as e:
                    say("    rotation/crop stopped: %s" % e)
            elif cfg["fit"]:
                say("scaling clips to the frame...")
                clips = find_clip_ids(cl.run("sequence.inspect"))
                if clips:
                    say("    " + short(cl.run("clip.fitToFrame", clips=clips)))
                else:
                    say("    no clips found in sequence.inspect; skipped")

            if cfg["music"]:
                say("adding music: %s" % os.path.basename(cfg["music"]))
                try:
                    mi = cl.run("file.import", paths=[cfg["music"]])
                    mid = (mi.get("items") or [None])[0]
                    cl.run("timeline.place", item=mid, track="A1", frame=0)
                    seq = cl.run("sequence.inspect")
                    vend = max([it.get("endFrame", 0)
                                for tr in seq.get("video", [])
                                for it in tr.get("items", [])] or [0])
                    aclip = None
                    for tr in seq.get("audio", []):
                        for it in tr.get("items", []):
                            if it.get("item") == mid:
                                aclip = it
                    if not aclip:
                        say("    placed, but could not find the music clip")
                    else:
                        over = aclip.get("endFrame", 0) - vend
                        if over > 0:
                            cl.run("timeline.trim", clip=aclip["clip"],
                                   edge="out", mode="regular",
                                   deltaFrames=-over)
                            say("    trimmed %d frames to end with the video"
                                % over)
                        elif over < 0:
                            say("    music is %.1fs shorter than the video"
                                % (-over / cfg["fps"]))
                        try:
                            cl.run("sequence.applyAudioTransition",
                                   clip=aclip["clip"], effect="constant_power",
                                   frames=int(round(2 * cfg["fps"])),
                                   edge="out", align="end")
                            say("    2 s fade-out added")
                        except RuntimeError as e:
                            say("    fade-out skipped: %s" % e)
                except RuntimeError as e:
                    say("    music skipped: %s" % e)
            elif cfg["sess_music_note"]:
                say(cfg["sess_music_note"])
            say("Done: %d images placed." % len(names))
            q.put(("done", True))
        except (RuntimeError, OSError) as e:
            say("STOPPED: %s" % e)
            q.put(("done", False))

    def start():
        if state["busy"]:
            return
        names = state["names"]
        sec, dis, fps = num("seconds", float), num("dissolve", int), \
            num("fps", float)
        w, h, port = num("width", int), num("height", int), num("port", int)
        tokn = V["token"].get().strip()
        if not names:
            messagebox.showinfo("FilmCraft Slideshow", "No images in folder.")
            return
        if None in (sec, dis, fps, w, h, port) or sec <= 0 or fps <= 0:
            messagebox.showinfo("FilmCraft Slideshow",
                                "Check the numbers in Settings.")
            return
        still = frames_for(sec, fps)
        if dis < 0 or dis >= still:
            messagebox.showinfo("FilmCraft Slideshow",
                                "Dissolve must be 0 or shorter than one image "
                                "(%d frames)." % still)
            return
        if not tokn:
            messagebox.showinfo("FilmCraft Slideshow",
                                "Enter the control token (64 hex characters).")
            return
        sess_on = use_sess.get() and state["session"]
        music = state["music"] if (sess_on and do_music.get()) else None
        cfg = dict(names=names, paths=list(state["paths"]),
                   port=port, token=tokn, still=still, dissolve=dis, fps=fps,
                   w=w, h=h, name=V["name"].get().strip() or "Slideshow",
                   use_current=use_current.get(), fit=fit.get(),
                   crop=bool(sess_on and do_crop.get()),
                   imgs=state["imgs"] if sess_on else {},
                   durations=dict(state["durations"]) if sess_on else {},
                   music=music,
                   title=(state["title"] if (sess_on and do_title.get()) else None),
                   sess_music_note=("music file not found; skipped"
                                    if sess_on and do_music.get()
                                    and not state["music"] else ""))
        state["busy"] = True
        build_btn.state(["disabled"])
        log("--- build: %d images, %s ---" % (len(names),
            "session file" if sess_on else "folder order"))
        threading.Thread(target=worker, args=(cfg,), daemon=True).start()

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    log(val)
                else:
                    state["busy"] = False
                    build_btn.state(["!disabled"])
        except queue.Empty:
            pass
        root.after(150, poll)

    load_folder()
    poll()
    root.mainloop()
    return 0


def main():
    ap = argparse.ArgumentParser(description="FilmCraft slideshow GUI")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FILMCRAFT_CONTROL_PORT", 9876)))
    ap.add_argument("--token", default=os.environ.get("FILMCRAFT_CONTROL_TOKEN"))
    a = ap.parse_args()
    return run_gui(a.port, a.token)


if __name__ == "__main__":
    raise SystemExit(main())
