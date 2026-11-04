#!/usr/bin/env python3
"""
filmcraft_slideshow.py - build a slideshow from a folder of images in FilmCraft.

  python filmcraft_slideshow.py "F:\\still2vid\\VID-SI2022-04"
  python filmcraft_slideshow.py FOLDER --seconds 5 --dissolve 0     (hard cuts)

Needs FilmCraft running with the control channel and a token, started from a
shell that has FILMCRAFT_CONTROL_TOKEN set (64 hex chars), and the same token
set in the shell you run this from (or pass --token):
  filmcraft --control 9876

Steps (each reply is printed so a failure shows exactly where it stopped):
  1. file.import                the images, in natural order (img2 before img10)
  2. project.items              find the imported items by file name
  3. file.newSequence           WIDTHxHEIGHT at FPS (skip with --use-current)
  4. clip.automateToSequence    sequentially, still length and dissolve overlap
  5. clip.fitToFrame            scale every clip to the frame (skip: --no-fit)

Options: --seconds S per image (default 4)   --dissolve FRAMES overlap, 0 = cut
         --width/--height/--fps (default 1920 1080 25)   --name NAME
         --port N   --token T   --use-current   --no-fit   --dry-run
"""
import argparse
import json
import os
import re
import socket
import sys

EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp")


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]


class Client:
    def __init__(self, port, token):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=300)
        self.f = self.s.makefile("rw", encoding="utf-8", newline="\n")
        self.token = token
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.f.write(json.dumps({"id": self.n, "token": self.token,
                                 "method": method, "params": params}) + "\n")
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise RuntimeError("FilmCraft closed the connection")
        r = json.loads(line)
        if not r.get("ok"):
            raise RuntimeError("%s failed: %s" % (method, r.get("error", r)))
        return r.get("result")

    def run(self, command, **params):
        return self.call("engine.execute", command=command, params=params)


def short(o, n=300):
    t = json.dumps(o, separators=(",", ":"), default=str)
    return t if len(t) <= n else t[:n] + "...(%d chars)" % len(t)


def strings(o, out):
    if isinstance(o, str):
        out.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            strings(v, out)
    elif isinstance(o, list):
        for v in o:
            strings(v, out)
    return out


def find_items(tree, names):
    """Map file name -> item id, whatever the field names are: a dict with an
    'id' that holds a string equal to the file name (or its full path)."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if "id" in o and not isinstance(o["id"], (dict, list)):
                strs = strings({k: v for k, v in o.items() if k != "id"}, [])
                low = [s.lower() for s in strs]
                for nm in names:
                    if nm in found:
                        continue
                    ln = nm.lower()
                    if any(s == ln or s.replace("\\", "/").endswith("/" + ln)
                           for s in low):
                        found[nm] = o["id"]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(tree)
    return found


def row_names(tree):
    """{item id: name} from project.items rows ({"cells":{"Name":..},"item":N})."""
    out = {}

    def walk(o):
        if isinstance(o, dict):
            cells = o.get("cells")
            if isinstance(cells, dict) and isinstance(cells.get("Name"), str):
                for k in ("item", "id", "itemId", "itemID"):
                    if k in o and not isinstance(o[k], (dict, list)):
                        out[o[k]] = cells["Name"]
                        break
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(tree)
    return out


def resolve_items(imp, tree, names):
    """Item ids for names, in order. file.import returns {"items":[ids]} in
    path order; project.items rows are used to verify that (or, if they
    disagree, to match by file name). Returns (ids or None, how)."""
    lst = imp.get("items") if isinstance(imp, dict) else None
    by_import = lst if isinstance(lst, list) and len(lst) == len(names) else None
    rows = row_names(tree)
    if by_import:
        seen = [rows.get(i) for i in by_import]
        if all(x is None for x in seen):
            return by_import, "import order (rows carry no ids to check)"
        if all(x is not None and x.lower() == n.lower()
               for x, n in zip(seen, names)):
            return by_import, "import ids, verified by name"
    rev = {}
    for i, n in rows.items():
        rev.setdefault(n.lower(), i)
    if all(n.lower() in rev for n in names):
        return [rev[n.lower()] for n in names], "matched by name"
    return None, "no match"


def find_clip_ids(tree):
    """Clip ids from sequence.inspect: dicts with an id and source/start data."""
    ids = []

    def walk(o):
        if isinstance(o, dict):
            if "id" in o and ("sourceIn" in o or "sourceOut" in o):
                ids.append(o["id"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(tree)
    return ids


def main():
    ap = argparse.ArgumentParser(description="Image folder -> FilmCraft slideshow")
    ap.add_argument("folder")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--dissolve", type=int, default=15, help="overlap frames; 0 = hard cuts")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--name", default="Slideshow")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FILMCRAFT_CONTROL_PORT", 9876)))
    ap.add_argument("--token", default=os.environ.get("FILMCRAFT_CONTROL_TOKEN"))
    ap.add_argument("--use-current", action="store_true")
    ap.add_argument("--no-fit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    folder = os.path.abspath(a.folder)
    if not os.path.isdir(folder):
        print("Folder not found:", folder)
        return 1
    names = sorted((n for n in os.listdir(folder)
                    if n.lower().endswith(EXTS)), key=natural_key)
    if not names:
        print("No images (%s) in %s" % (", ".join(EXTS), folder))
        return 1
    still = max(1, round(a.seconds * a.fps))
    if a.dissolve and a.dissolve >= still:
        print("--dissolve (%d frames) must be shorter than one image (%d frames)."
              % (a.dissolve, still))
        return 1
    paths = [os.path.join(folder, n) for n in names]
    total = (still * len(names) - a.dissolve * (len(names) - 1)) / a.fps
    print("%d images, %d frames each (%.2fs) at %gfps, %s -> about %.1fs"
          % (len(names), still, still / a.fps, a.fps,
             "%d-frame dissolves" % a.dissolve if a.dissolve else "hard cuts",
             total))
    print("first: %s   last: %s" % (names[0], names[-1]))
    if a.dry_run:
        return 0
    if not a.token:
        print("No token. Set FILMCRAFT_CONTROL_TOKEN or pass --token.")
        return 1

    try:
        cl = Client(a.port, a.token)
    except OSError as e:
        print("Cannot connect to 127.0.0.1:%d (%s)" % (a.port, e))
        return 1
    try:
        print("1/5 importing...")
        r = cl.run("file.import", paths=paths)
        print("    ", short(r))

        print("2/5 finding the imported items...")
        tree = cl.run("project.items", recursive=True)
        item_ids, how = resolve_items(r, tree, names)
        if not item_ids:
            print("    could not match the %d imported images (%s)"
                  % (len(names), how))
            print("    project.items reply:", short(tree, 800))
            return 1
        print("    %d items: %s" % (len(item_ids), how))

        if not a.use_current:
            print("3/5 creating sequence %dx%d @ %gfps..." %
                  (a.width, a.height, a.fps))
            print("    ", short(cl.run("file.newSequence", name=a.name,
                                       width=a.width, height=a.height,
                                       fps=a.fps)))
        else:
            print("3/5 using the current sequence")

        print("4/5 placing on the timeline...")
        r = cl.run("clip.automateToSequence", items=item_ids,
                   ordering="selection", placement="sequentially",
                   method="overwrite", stillFrames=still,
                   overlapFrames=a.dissolve,
                   videoTransition=bool(a.dissolve),
                   ignoreAudio=True)
        print("    ", short(r))

        if not a.no_fit:
            print("5/5 scaling clips to the frame...")
            clips = find_clip_ids(cl.run("sequence.inspect"))
            if not clips:
                print("    no clips found in sequence.inspect; skipped")
            else:
                print("    ", short(cl.run("clip.fitToFrame", clips=clips)))
        print("Done: %d images placed." % len(names))
        return 0
    except RuntimeError as e:
        print("STOPPED:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
