#!/usr/bin/env python3
"""
filmcraft_apply_session.py - apply per-image rotation and crop from a
.still2video_session.json to the clips of the open FilmCraft sequence.

  python filmcraft_apply_session.py "F:\\still2vid\\VID-SI2022-04\\.still2video_session.json" --dry-run
  python filmcraft_apply_session.py SESSION.json --only 20221228120912
  python filmcraft_apply_session.py SESSION.json

Needs FilmCraft running with --control and FILMCRAFT_CONTROL_TOKEN (see the
slideshow script), the slideshow sequence open, and filmcraft_slideshow.py in
the same folder. Clips are matched to the JSON by file name; clips that are
not in the JSON are left alone.

How the JSON is read (checked against your crop-tool screenshot):
  crop = [x, y, width, height] as fractions of the image AFTER rotation.
  rotation = degrees counter-clockwise, applied to the raw image pixels.
How it is applied: Motion only (anchor, position, scale, rotation). The
anchor is put on the crop centre and the scale makes the crop fill the
frame. If the crop is not frame-shaped (pillar/letterbox), the crop is
also set with FilmCraft's Crop effect so the rest of the image is hidden.

  --dry-run          print what would be set, change nothing
  --only TEXT        only clips whose file name contains TEXT
  --flip-rotation    use if a rotated image comes out upside down
  --exif MODE        auto (default) | applied | ignored: whether FilmCraft
                     already turned phone photos upright on import
  --test             run the built-in geometry checks and exit
"""
import argparse
import json
import math
import os
import re
import sys

FRAME_TOL = 0.01          # crop within 1% of frame aspect counts as "fills"


# ----------------------------------------------------------------------
# Geometry (pure functions; tested by --test)
# ----------------------------------------------------------------------
def to_source(u, phi, w0, h0):
    """Point u in the rotated image -> point in the raw image. The rotated
    image is the raw one turned phi degrees counter-clockwise (y down)."""
    a = math.radians(phi)
    c, s = math.cos(a), math.sin(a)
    w1 = abs(w0 * c) + abs(h0 * s)
    h1 = abs(w0 * s) + abs(h0 * c)
    dx, dy = u[0] - w1 / 2.0, u[1] - h1 / 2.0
    return (w0 / 2.0 + c * dx - s * dy, h0 / 2.0 + s * dx + c * dy)


def plan(w0, h0, rotation, crop, fw, fh, exif="auto", flip=False):
    """Motion/Crop settings for one image. w0,h0 = size FilmCraft reports."""
    notes = []
    phi = float(rotation or 0) % 360.0
    if exif == "applied" or (exif == "auto" and abs(phi % 180 - 90) < 1e-6
                             and h0 > w0):
        if phi:
            notes.append("source already upright; JSON rotation not applied")
        phi = 0.0
    a = math.radians(phi)
    w1 = abs(w0 * math.cos(a)) + abs(h0 * math.sin(a))
    h1 = abs(w0 * math.sin(a)) + abs(h0 * math.cos(a))
    cx, cy, cw, ch = crop if crop else (0.0, 0.0, 1.0, 1.0)
    L, T, W, H = cx * w1, cy * h1, cw * w1, ch * h1
    anchor = to_source((L + W / 2.0, T + H / 2.0), phi, w0, h0)
    r = -phi % 360.0
    if r > 180:
        r -= 360.0
    if flip:
        r = -r
    fills = abs((W / H) / (fw / float(fh)) - 1.0) < FRAME_TOL
    s = (max if fills else min)(fw / W, fh / H)
    out = {"anchor": [round(anchor[0], 3), round(anchor[1], 3)],
           "position": [fw / 2.0, fh / 2.0],
           "scale": round(s * 100.0, 4),
           "rotation": round(r, 4),
           "crop": None, "notes": notes, "fills": fills,
           "rect": (L, T, W, H), "phi": phi, "size1": (w1, h1)}
    if not fills:
        if abs(phi % 90) > 1e-6:
            notes.append("crop is not frame-shaped and rotation is not a "
                         "multiple of 90: bars will show the whole image")
        else:
            p0 = to_source((L, T), phi, w0, h0)
            p1 = to_source((L + W, T + H), phi, w0, h0)
            x0, x1 = sorted((p0[0], p1[0]))
            y0, y1 = sorted((p0[1], p1[1]))
            e = [max(0.0, v) for v in (x0 / w0 * 100, y0 / h0 * 100,
                                       (w0 - x1) / w0 * 100,
                                       (h0 - y1) / h0 * 100)]
            if max(e) > 1e-4:
                out["crop"] = [round(v, 4) for v in e]
    return out


def to_frame(q, pl):
    """Where a raw-image point lands in the frame (Motion model: scale and
    clockwise rotation about the anchor, then placed at Position)."""
    r = math.radians(pl["rotation"])
    c, s = math.cos(r), math.sin(r)
    dx, dy = q[0] - pl["anchor"][0], q[1] - pl["anchor"][1]
    k = pl["scale"] / 100.0
    return (pl["position"][0] + k * (c * dx - s * dy),
            pl["position"][1] + k * (s * dx + c * dy))


def self_test():
    ok = True
    fw, fh = 1920, 1080

    def check(name, cond):
        nonlocal ok
        print("%-62s %s" % (name, "ok" if cond else "FAIL"))
        ok &= bool(cond)

    # 1. 4032x3024, no rotation, symmetric 16:9 crop: centred, fit 47.619%
    p = plan(4032, 3024, 0, [0.0, 0.125, 1.0, 0.75], fw, fh)
    check("0 deg 16:9: anchor = image centre",
          p["anchor"] == [2016.0, 1512.0])
    check("0 deg 16:9: scale 47.619%", abs(p["scale"] - 47.619) < 1e-3)
    check("0 deg 16:9: no Crop effect needed", p["crop"] is None)
    # 2. screenshot case: crop [0.1414.., 0, 0.8586.., 1.0] (3460x3024 px)
    p = plan(4032, 3024, 0, [0.14141414141414, 0.0, 0.85858585858586, 1.0],
             fw, fh)
    check("screenshot crop: not frame-shaped -> Crop left 14.14%",
          p["crop"] and abs(p["crop"][0] - 14.1414) < 1e-3
          and abs(p["crop"][2]) < 1e-3 and abs(p["crop"][1]) < 1e-3)
    check("screenshot crop: fit scale = 1080/3024",
          abs(p["scale"] - 100 * 1080 / 3024) < 1e-3)
    # 3. rotation 270 (CCW) + 16:9 crop: Motion rotation +90 (clockwise)
    p = plan(4032, 3024, 270, [0.0, 0.2890625, 1.0, 0.421875], fw, fh)
    check("270 deg: Motion rotation = +90", p["rotation"] == 90.0)
    check("270 deg: scale 1920/3024", abs(p["scale"] - 100 * 1920 / 3024) < 1e-3)
    # 4. corners of every crop rectangle must land on the frame corners
    cases = [(4032, 3024, 0, [0, .125, 1, .75]),
             (4032, 3024, 270, [0, .2890625, 1, .421875]),
             (4032, 3024, 270, [0, .41790865384615383, 1, .421875]),
             (4032, 3024, 90, [0, .2890625, 1, .421875]),
             (4032, 3024, 180, [0, .125, 1, .75]),
             (4032, 3024, 0, [.022524350649350634, .1596153846153846,
                              .910218253968254, .6826923076923077])]
    for w0, h0, rot, crop in cases:
        p = plan(w0, h0, rot, crop, fw, fh)
        L, T, W, H = p["rect"]
        pts = [(L, T), (L + W, T), (L, T + H), (L + W, T + H)]
        want = [(0, 0), (fw, 0), (0, fh), (fw, fh)]
        got = [to_frame(to_source(u, p["phi"], w0, h0), p) for u in pts]
        err = max(max(abs(g[0] - t[0]), abs(g[1] - t[1]))
                  for g, t in zip(got, want))
        check("corners land on frame: rot %3d crop %s" % (rot, crop[1:4:2]),
              err < 2.0)
    # 5. already-upright phone photo (FilmCraft reports 3024x4032)
    p = plan(3024, 4032, 270, [0.0, 0.2890625, 1.0, 0.421875], fw, fh)
    check("auto-detect upright source: rotation 0",
          p["rotation"] == 0 and p["notes"])
    return 0 if ok else 1


# ----------------------------------------------------------------------
# FilmCraft side
# ----------------------------------------------------------------------
def row_dims(tree):
    """{item id: (w, h)} from project.items 'Video Info' ("4032 x 2268 ...")."""
    out = {}

    def walk(o):
        if isinstance(o, dict):
            cells = o.get("cells")
            if isinstance(cells, dict):
                m = re.search(r"(\d+)\s*x\s*(\d+)",
                              str(cells.get("Video Info", "")))
                if m:
                    for k in ("item", "id", "itemId", "itemID"):
                        if k in o and not isinstance(o[k], (dict, list)):
                            out[o[k]] = (int(m.group(1)), int(m.group(2)))
                            break
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(tree)
    return out


def jpeg_size(path):
    """(w, h) from a JPEG header, no libraries; None if unreadable."""
    try:
        with open(path, "rb") as f:
            d = f.read(262144)
    except OSError:
        return None
    i = 2
    while i + 9 < len(d):
        if d[i] != 0xFF:
            i += 1
            continue
        m = d[i + 1]
        if m in (0xC0, 0xC1, 0xC2):
            return (d[i + 7] << 8 | d[i + 8], d[i + 5] << 8 | d[i + 6])
        i += 2 + (d[i + 2] << 8 | d[i + 3])
    return None


def clips_of(seq):
    out = []
    for tr in seq.get("video", []):
        for it in tr.get("items", []):
            out.append(it)
    return out


def image_name(path):
    """File name of a path from the JSON (it mixes / and \\)."""
    return path.replace("\\", "/").split("/")[-1]


def read_session(path):
    """(settings dict, images in file order, {lower name: image dict})."""
    with open(path, "r", encoding="utf-8") as f:
        sess = json.load(f)
    images = sess.get("images", [])
    return (sess.get("settings", {}), images,
            {image_name(im["path"]).lower(): im for im in images})


def apply_to_sequence(cl, imgs, only="", dry_run=False, exif="auto",
                      flip=False, log=print):
    """Set Motion (and Crop) on every clip of the open sequence whose file
    name is in imgs. Returns clips updated, or -1 if nothing matched."""
    seq = cl.run("sequence.inspect")
    st = seq.get("settings", {})
    fw, fh = st.get("width", 1920), st.get("height", 1080)
    dims = row_dims(cl.run("project.items", recursive=True))
    clips = [c for c in clips_of(seq)
             if c.get("name", "").lower() in imgs
             and only.lower() in c["name"].lower()]
    log("sequence %s: %dx%d, %d clips; %d in the session file match"
        % (seq.get("name"), fw, fh, len(clips_of(seq)), len(clips)))
    if not clips:
        log("Nothing to do. Is the sequence for this folder open?")
        return -1
    done = 0
    for c in clips:
        im = imgs[c["name"].lower()]
        size = dims.get(c.get("item")) or jpeg_size(im["path"].replace(
            "\\", "/"))
        if not size:
            log("%-22s SKIP: cannot find the image size" % c["name"])
            continue
        pl = plan(size[0], size[1], im.get("rotation", 0),
                  im.get("crop"), fw, fh, exif, flip)
        log("%-22s %dx%d rot %-4s crop %s -> anchor %s scale %.2f%% "
            "rot %s%s%s" % (
                c["name"], size[0], size[1], im.get("rotation", 0),
                [round(v, 3) for v in im["crop"]] if im.get("crop")
                else "-", pl["anchor"], pl["scale"], pl["rotation"],
                "  +Crop %s" % pl["crop"] if pl["crop"] else "",
                "  (%s)" % "; ".join(pl["notes"]) if pl["notes"] else ""))
        if dry_run:
            continue
        clip = c["clip"]
        cl.run("effects.setParam", clip=clip, effect="motion",
               param="uniform_scale", value=True)
        for param in ("anchor", "position", "scale", "rotation"):
            cl.run("effects.setParam", clip=clip, effect="motion",
                   param=param, value=pl[param])
        has_crop = any(e.get("effect") == "crop"
                       for e in c.get("effects", []))
        if pl["crop"] or has_crop:
            if not has_crop:
                cl.run("effects.apply", clips=[clip], effect="crop")
            edges = pl["crop"] or [0.0, 0.0, 0.0, 0.0]
            for param, v in zip(("left", "top", "right", "bottom"), edges):
                cl.run("effects.setParam", clip=clip, effect="crop",
                       param=param, value=v)
        done += 1
    log("Dry run, nothing changed." if dry_run
        else "Done: %d clips updated." % done)
    return done


def main():
    ap = argparse.ArgumentParser(description="Apply session rotation/crop")
    ap.add_argument("session", nargs="?")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FILMCRAFT_CONTROL_PORT", 9876)))
    ap.add_argument("--token", default=os.environ.get("FILMCRAFT_CONTROL_TOKEN"))
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--flip-rotation", action="store_true")
    ap.add_argument("--exif", choices=("auto", "applied", "ignored"),
                    default="auto")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        return self_test()
    if not a.session:
        ap.error("give the path to .still2video_session.json")
    if not a.token:
        print("No token. Set FILMCRAFT_CONTROL_TOKEN or pass --token.")
        return 1
    _, _, imgs = read_session(a.session)

    from filmcraft_slideshow import Client
    try:
        cl = Client(a.port, a.token)
    except OSError as e:
        print("Cannot connect to 127.0.0.1:%d (%s)" % (a.port, e))
        return 1
    try:
        n = apply_to_sequence(cl, imgs, a.only, a.dry_run, a.exif,
                              a.flip_rotation)
        return 1 if n < 0 else 0
    except RuntimeError as e:
        print("STOPPED:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
