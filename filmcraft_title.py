#!/usr/bin/env python3
"""
filmcraft_title.py - build the title screen from a .still2video_session.json
in the open FilmCraft sequence.

  python filmcraft_title.py SESSION.json --dry-run     show the layout only
  python filmcraft_title.py SESSION.json               add it to the sequence

What it builds (from the "title_screen" block of the file):
  * V1: a copy of the first slideshow image, title_duration long, INSERTED at
    the very start, so the whole slideshow moves later (bg_mode "blur": it
    gets a Gaussian Blur and crossfades into the real first slide).
  * a graphic on a higher track with, back to front: a darkening rectangle
    (blur) or a solid-colour rectangle (solid), the title text, the rule,
    the subtitle and the date, centred on v_position.
Fonts, sizes, colours, shadow, alignment, rule width/thickness/gap and the
line padding come from the file. Needs FilmCraft with the control channel and
a token, and filmcraft_slideshow.py in the same folder (see that script).

  --blur-scale N   FilmCraft Blurriness = blur_radius x N (default 5; the
                   file's radius is in a different unit, so tune by eye)
  --dry-run        print the layout, change nothing
"""
import argparse
import json
import os
import sys

LINE_H = 1.2        # line box height / font size
BASELINE = 0.95     # first baseline inside the line box / font size
GFX_TRACK = 2       # graphics go on this track index (never V1)


# ----------------------------------------------------------------------
# Layout (pure)
# ----------------------------------------------------------------------
def font_name(face):
    return " ".join(w.capitalize() for w in str(face or "arial").split())


def layout(t, fw, fh):
    """Layers back-to-front for the title graphic, positions in frame px."""
    pad = float(t.get("line_padding", 20))
    gap = float(t.get("rule_gap", 40))
    thick = float(t.get("rule_thickness", 6))
    rw = float(t.get("rule_width", 0.77)) * fw
    elems = []
    if t.get("title"):
        elems.append(("text", "title", t["title"], float(t.get("title_size", 92)),
                      t.get("title_color", "#ffffff")))
    if t.get("show_rule", True) and t.get("title") and (
            t.get("subtitle") or t.get("date")):
        elems.append(("rule", "rule", "", thick, t.get("rule_color", "#ffffff")))
    if t.get("subtitle"):
        elems.append(("text", "subtitle", t["subtitle"],
                      float(t.get("subtitle_size", 60)),
                      t.get("subtitle_color", "#cccccc")))
    if t.get("date"):
        elems.append(("text", "date", t["date"], float(t.get("date_size", 52)),
                      t.get("date_color", "#aaaaaa")))
    heights = [e[3] * LINE_H if e[0] == "text" else e[3] for e in elems]
    spaces = [gap if "rule" in (a[0], b[0]) else pad
              for a, b in zip(elems, elems[1:])]
    total = sum(heights) + sum(spaces)
    y = float(t.get("v_position", 0.5)) * fh - total / 2.0
    align = t.get("text_align", "center")
    margin = 0.1 * fw
    x = {"left": margin, "right": fw - margin}.get(align, fw / 2.0)
    rx = {"left": margin + rw / 2, "right": fw - margin - rw / 2}.get(
        align, fw / 2.0)
    layers = []
    mode = str(t.get("bg_mode", "blur")).lower()
    if mode == "blur":
        layers.append({"kind": "rect", "name": "darken", "pos": [fw / 2.0, fh / 2.0],
                       "size": [fw, fh], "color": "#000000",
                       "opacity": 100.0 * float(t.get("darken_factor", 0.5))})
    elif mode == "solid":
        layers.append({"kind": "rect", "name": "background",
                       "pos": [fw / 2.0, fh / 2.0], "size": [fw, fh],
                       "color": t.get("solid_color", "#000000"), "opacity": 100.0})
    for i, e in enumerate(elems):
        if e[0] == "text":
            layers.append({"kind": "text", "name": e[1], "text": e[2],
                           "size": e[3], "color": e[4], "align": align,
                           "pos": [x, round(y + BASELINE * e[3], 1)],
                           "shadow": bool(t.get("shadow", True))})
        else:
            layers.append({"kind": "rect", "name": "rule",
                           "pos": [rx, round(y + e[3] / 2.0, 1)],
                           "size": [rw, e[3]], "color": e[4], "opacity": 100.0})
        y += heights[i] + (spaces[i] if i < len(spaces) else 0)
    return layers


# ----------------------------------------------------------------------
# FilmCraft side
# ----------------------------------------------------------------------
def video_items(seq):
    """[(track index, item dict)] for every video clip."""
    return [(ti, it) for ti, tr in enumerate(seq.get("video", []))
            for it in tr.get("items", [])]


def find_key(o, keys):
    if isinstance(o, dict):
        for k in keys:
            if isinstance(o.get(k), int) and not isinstance(o.get(k), bool):
                return o[k]
        for v in o.values():
            r = find_key(v, keys)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_key(v, keys)
            if r is not None:
                return r
    return None


def last_layer(reply):
    """Index of the newest layer from a graphics.list reply, or None."""
    def lists(o):
        if isinstance(o, list) and o and all(isinstance(x, dict) for x in o):
            yield o
        if isinstance(o, dict):
            for v in o.values():
                yield from lists(v)
    for lst in lists(reply):
        last = lst[-1]
        for k in ("index", "layer", "id"):
            if isinstance(last.get(k), int) and not isinstance(last.get(k), bool):
                return last[k]
        return len(lst) - 1
    return None


def add_title(cl, t, first_item, fps, fw, fh, dis_frames=0, blur_scale=5.0,
              log=print):
    dur = float(t.get("duration", 4.0))
    n = max(1, round(dur * fps))
    layers = layout(t, fw, fh)
    seq0 = cl.run("sequence.inspect")
    before = {it["clip"] for _, it in video_items(seq0)}

    log("title: inserting a %d-frame copy of the first image at the start..." % n)
    cl.run("timeline.place", item=first_item, track="V1", frame=0,
           duration={"frame": n}, insert=True)
    seq = cl.run("sequence.inspect")
    new = [it for ti, it in video_items(seq) if it["clip"] not in before]
    bg = next((it for it in new if it.get("startFrame") == 0), None)
    if bg is None:
        raise RuntimeError("could not find the inserted title background clip")
    later = [it for ti, it in video_items(seq)
             if it["clip"] in before and ti == 0]
    if later and min(it.get("startFrame", 0) for it in later) < n:
        log("    NOTE: the slideshow did not move later; the title sits over "
            "the first %.1fs of it" % dur)
    else:
        log("    slideshow moved %d frames later" % n)

    if str(t.get("bg_mode", "blur")).lower() == "blur":
        try:
            blur = float(t.get("blur_radius", 4)) * blur_scale
            cl.run("effects.apply", clips=[bg["clip"]], effect="gaussian_blur")
            cl.run("effects.setParam", clip=bg["clip"], effect="gaussian_blur",
                   param="blurriness", value=blur)
            log("    blur %.1f on the background" % blur)
        except RuntimeError as e:
            log("    blur skipped: %s" % e)
    if dis_frames:
        try:
            cl.run("sequence.applyVideoTransition", clip=bg["clip"],
                   effect="cross_dissolve", frames=dis_frames, edge="out",
                   align="center")
            log("    %d-frame crossfade into the first slide" % dis_frames)
        except RuntimeError as e:
            log("    crossfade skipped: %s" % e)

    log("title: building the graphic (%d layers)..." % len(layers))
    cl.run("playhead.set", frame=0)
    gid = None
    for L in layers:
        common = dict(seconds=dur, time=0)
        if gid is None:
            common["track"] = GFX_TRACK
        else:
            common["clip"] = gid
        if L["kind"] == "rect":
            r = cl.run("graphics.newRectangle", position=L["pos"],
                       size=L["size"], **common)
            props = {"fill_color": L["color"], "opacity": L["opacity"],
                     "stroke": False}
        else:
            r = cl.run("graphics.newText", text=L["text"], position=L["pos"],
                       size=L["size"], font=font_name(t.get("font_face")),
                       **common)
            props = {"fill_color": L["color"], "align": L["align"],
                     "shadow": L["shadow"]}
        if gid is None:
            gid = find_key(r, ("clip", "clipId", "id"))
            if gid is None:
                after = cl.run("sequence.inspect")
                known = before | {it["clip"] for it in new}
                fresh = [it["clip"] for _, it in video_items(after)
                         if it["clip"] not in known]
                gid = fresh[0] if fresh else None
            if gid is None:
                raise RuntimeError("could not find the new graphic clip; "
                                   "graphics reply: %s" % json.dumps(r)[:300])
        lay = last_layer(cl.run("graphics.list", clip=gid))
        args = dict(clip=gid, props=props)
        if lay is not None:
            args["layer"] = lay
        cl.run("graphics.set", **args)
        log("    %-9s layer %s" % (L["name"], lay))
    return n


def main():
    ap = argparse.ArgumentParser(description="Title screen from a session file")
    ap.add_argument("session")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FILMCRAFT_CONTROL_PORT", 9876)))
    ap.add_argument("--token", default=os.environ.get("FILMCRAFT_CONTROL_TOKEN"))
    ap.add_argument("--blur-scale", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--frame", default="1920x1080",
                    help="for --dry-run without FilmCraft, e.g. 1920x1080")
    a = ap.parse_args()
    with open(a.session, "r", encoding="utf-8") as f:
        sess = json.load(f)
    stg = sess.get("settings", {})
    t = stg.get("title_screen")
    if not t or not t.get("enabled"):
        print("The session file has no enabled title_screen.")
        return 1
    if a.dry_run:
        fw, fh = (int(v) for v in a.frame.lower().split("x"))
        print("title %.1fs, bg %s, frame %dx%d" % (
            float(t.get("duration", 4)), t.get("bg_mode"), fw, fh))
        for L in layout(t, fw, fh):
            print("  %-9s %-4s pos %-20s %s" % (
                L["name"], L["kind"], L["pos"],
                ("size %s" % L["size"]) if L["kind"] == "text"
                else "box %s opacity %g" % (L["size"], L["opacity"])))
        return 0
    if not a.token:
        print("No token. Set FILMCRAFT_CONTROL_TOKEN or pass --token.")
        return 1
    from filmcraft_slideshow import Client
    try:
        cl = Client(a.port, a.token)
    except OSError as e:
        print("Cannot connect to 127.0.0.1:%d (%s)" % (a.port, e))
        return 1
    try:
        seq = cl.run("sequence.inspect")
        st = seq.get("settings", {})
        fw, fh = st.get("width", 1920), st.get("height", 1080)
        fr = st.get("frame_rate", {})
        fps = fr.get("num", 25) / float(fr.get("den", 1) or 1)
        items = sorted((it for ti, it in video_items(seq) if ti == 0),
                       key=lambda it: it.get("startFrame", 0))
        if not items:
            print("No clips on V1; build the slideshow first.")
            return 1
        fps_int = fps
        dis = 0
        tn = [x for x in seq.get("video", [{}])[0].get("transitions", [])]
        if tn:
            dis = round(tn[0].get("duration", 0) / 254016000000.0 * fps)
        add_title(cl, t, items[0]["item"], fps_int, fw, fh, dis, a.blur_scale)
        print("Done.")
        return 0
    except RuntimeError as e:
        print("STOPPED:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
