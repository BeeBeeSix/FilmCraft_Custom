import os
import random
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from PIL import Image, ImageTk, ImageFilter
import numpy as np
import threading
import json

# MoviePy v2.x imports
from moviepy import ImageClip, VideoClip, AudioFileClip, concatenate_videoclips, vfx
try:
    from moviepy.audio.fx import AudioFadeOut as _AudioFadeOut
    def _make_audio_fadeout(duration):
        return _AudioFadeOut(duration)
except ImportError:
    # fallback: build fade manually via with_volume_scaled in worker
    _AudioFadeOut = None
    def _make_audio_fadeout(duration):
        return None
from proglog import ProgressBarLogger

THUMB_W   = 100
THUMB_H   = 70
THUMB_PAD = 6

VU_H         = 90
VU_BG        = "#0e0e0e"
VU_WAVE_COL  = "#00cc66"
VU_OVER_COL  = "#ff4444"
VU_CLIP_LINE = "#ff4444"
VU_TEXT_COL  = "#555555"
VU_SEG_LINE  = "#4da6ff"    # image boundary markers
VU_TRANS_COL = "#ffffff"    # transition zone tint (low alpha via stipple)
VU_PLAYHEAD  = "#ffcc00"

def fmt_time(s):
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"

def _item_dur(item, default_dur):
    """Return the effective duration for one image item (float)."""
    d = item.get("duration")
    return float(d) if d is not None else float(default_dur)

def norm_rot(v):
    """Normalise a rotation angle to 0..360 (2 dp); whole numbers become int."""
    v = round(float(v) % 360, 2)
    return int(v) if v == int(v) else v


def rotate_img(img, angle):
    """Rotate a PIL image by `angle` degrees (CCW, expand). One pass only."""
    angle = float(angle) % 360
    if angle == 0:
        return img
    if angle % 90 == 0:
        return img.rotate(angle, expand=True)
    return img.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)


def calc_video_duration(n, img_dur, trans_dur, trans_type, image_data=None):
    """
    Total video duration.  If image_data is supplied the per-item durations
    are used; otherwise the global img_dur is used for every clip.
    """
    if not image_data:
        # legacy scalar path
        safe = min(trans_dur, img_dur / 2)
        if trans_type in ("Crossfade", "Ken Burns"):
            return max(0.0, n * img_dur - (n - 1) * safe)
        elif trans_type == "Blur Dissolve":
            return max(0.0, n * img_dur + (n - 1) * safe)
        else:
            return max(0.0, n * img_dur)

    durations = [_item_dur(it, img_dur) for it in image_data]
    total = 0.0
    for i, d in enumerate(durations):
        safe = min(trans_dur, d / 2)
        total += d
        if i < n - 1:
            next_safe = min(trans_dur, durations[i + 1] / 2)
            eff_safe  = min(safe, next_safe)
            if trans_type in ("Crossfade", "Ken Burns"):
                total -= eff_safe
            elif trans_type == "Blur Dissolve":
                total += eff_safe
    return max(0.0, total)

def segment_times(n, img_dur, trans_dur, trans_type, image_data=None):
    """
    Return list of (start, end) for each image segment in the final video timeline.
    Supports per-item durations when image_data is provided.
    """
    if not image_data:
        safe = min(trans_dur, img_dur / 2)
        segs = []
        if trans_type in ("Crossfade", "Ken Burns"):
            step = img_dur - safe
            for i in range(n):
                segs.append((i * step, i * step + img_dur))
        elif trans_type == "Blur Dissolve":
            step = img_dur + safe
            for i in range(n):
                segs.append((i * step, i * step + img_dur))
        else:
            for i in range(n):
                segs.append((i * img_dur, (i + 1) * img_dur))
        return segs

    durations = [_item_dur(it, img_dur) for it in image_data]
    segs  = []
    cursor = 0.0
    for i, d in enumerate(durations):
        segs.append((cursor, cursor + d))
        if i < n - 1:
            safe      = min(trans_dur, d / 2)
            next_safe = min(trans_dur, durations[i + 1] / 2)
            eff_safe  = min(safe, next_safe)
            if trans_type in ("Crossfade", "Ken Burns"):
                cursor += d - eff_safe
            elif trans_type == "Blur Dissolve":
                cursor += d + eff_safe
            else:
                cursor += d
    return segs


class ImageToVideoApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Still2Video — Image to Video Creator")
        self.root.geometry("1150x920")

        self.image_folder        = ""
        self.image_data          = []
        self.current_preview_idx = 0
        self.music_path          = ""
        self._audio_rms          = None
        self._audio_dur          = 0.0
        self._audio_sr           = 44100
        self._drag_idx           = None
        self._thumb_cache        = {}
        self._vu_zoom            = 1.0   # waveform horizontal zoom multiplier
        self.title_screen        = None  # dict or None — set by TitleScreenEditor

        # ── Layout: top = controls | preview  ──────────────────────
        self.top_frame = ttk.Frame(self.root)
        self.top_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.left_frame = ttk.Frame(self.top_frame, padding="10")
        self.left_frame.pack(side=tk.LEFT, fill=tk.Y)

        self.right_frame = ttk.Frame(self.top_frame, padding="10")
        self.right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # ── Bottom: thumbnail strip, then waveform below it ─────────
        self.bottom_frame = ttk.Frame(self.root)
        self.bottom_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.strip_outer = ttk.Frame(self.bottom_frame, padding=(6, 4, 6, 0))
        self.strip_outer.pack(side=tk.TOP, fill=tk.X)

        self.vu_outer = ttk.Frame(self.bottom_frame, padding=(6, 2, 6, 4))
        self.vu_outer.pack(side=tk.TOP, fill=tk.X)

        self.setup_left_panel()
        self.setup_right_panel()
        self.setup_timeline_strip()
        self.setup_vu_panel()
        self._setup_shortcuts()

    # ═══════════════════════════════════════════════════════════════
    # LEFT PANEL
    # ═══════════════════════════════════════════════════════════════
    def setup_left_panel(self):
        ttk.Label(self.left_frame, text="1. Select Image Source",
                  font=('Helvetica', 10, 'bold')).pack(anchor=tk.W, pady=(0, 4))
        ttk.Button(self.left_frame, text="Browse Folder",
                   command=self.load_folder).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(self.left_frame, text="🎬 Title Screen…",
                   command=self.open_title_editor).pack(fill=tk.X, pady=(0, 12))

        ttk.Label(self.left_frame, text="2. Image Order",
                  font=('Helvetica', 10, 'bold')).pack(anchor=tk.W, pady=(0, 4))
        lf = ttk.Frame(self.left_frame)
        lf.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL)
        self.image_listbox = tk.Listbox(lf, yscrollcommand=sb.set, selectmode=tk.SINGLE)
        sb.config(command=self.image_listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.image_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.image_listbox.bind('<<ListboxSelect>>', self.on_image_select)

        ef = ttk.Frame(self.left_frame)
        ef.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(ef, text="⟲ Rot L",  width=8, command=lambda: self.rotate_selected(90)).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Button(ef, text="⟳ Rot R",  width=8, command=lambda: self.rotate_selected(-90)).pack(side=tk.LEFT, padx=2)
        ttk.Button(ef, text="✂ Crop",   width=7, command=self.open_crop_editor).pack(side=tk.LEFT, padx=2)
        ttk.Button(ef, text="❌ Delete", width=9, command=self.delete_selected).pack(side=tk.RIGHT, padx=(2, 0))

        ef2 = ttk.Frame(self.left_frame)
        ef2.pack(fill=tk.X, pady=(0, 12))
        ttk.Button(ef2, text="✂✂ Auto Crop 16:9 — All Images",
                   command=self.autocrop_169_all).pack(fill=tk.X)

        ttk.Label(self.left_frame, text="3. Video Configuration",
                  font=('Helvetica', 10, 'bold')).pack(anchor=tk.W, pady=(0, 4))

        def row(lbl, fn):
            f = ttk.Frame(self.left_frame); f.pack(fill=tk.X, pady=2)
            ttk.Label(f, text=lbl).pack(side=tk.LEFT)
            w = fn(f); w.pack(side=tk.RIGHT)
            return w

        self.duration_entry = row("Image Duration (sec):", lambda f: self._entry(f, "3.0", width=6))
        self.duration_entry.bind("<KeyRelease>", lambda e: self._on_params_changed())

        self.trans_combo = row("Transition Effect:", lambda f: self._combo(f,
            ["None", "Fade In/Out", "Crossfade", "Blur Dissolve", "Ken Burns"], "Crossfade", width=14))
        self.trans_combo.bind("<<ComboboxSelected>>", lambda e: self._on_params_changed())

        self.trans_dur_entry = row("Transition (sec):", lambda f: self._entry(f, "1.0", width=6))
        self.trans_dur_entry.bind("<KeyRelease>", lambda e: self._on_params_changed())

        ttk.Separator(self.left_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(8, 4))
        lf2 = ttk.Frame(self.left_frame); lf2.pack(fill=tk.X)
        ttk.Label(lf2, text="Est. Video Length:", font=('Helvetica', 9, 'bold')).pack(side=tk.LEFT)
        self.lbl_video_len = ttk.Label(lf2, text="—", font=('Helvetica', 9), foreground="#4da6ff")
        self.lbl_video_len.pack(side=tk.RIGHT)
        ttk.Separator(self.left_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(4, 8))

        ttk.Label(self.left_frame, text="4. Music Track (optional)",
                  font=('Helvetica', 10, 'bold')).pack(anchor=tk.W, pady=(0, 4))
        mr = ttk.Frame(self.left_frame); mr.pack(fill=tk.X, pady=2)
        self.lbl_music = ttk.Label(mr, text="No track selected.",
                  font=('Helvetica', 8, 'italic'), foreground='red')
        self.lbl_music.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(mr, text="Browse", width=7, command=self.load_music).pack(side=tk.RIGHT)
        ttk.Button(self.left_frame, text="✕ Clear Music",
                   command=self.clear_music).pack(fill=tk.X, pady=(0, 2))

        ttk.Button(self.left_frame, text="▶  Export Video",
                   command=self.export_video).pack(fill=tk.X, pady=(14, 0))

    def _entry(self, p, d, **kw):
        e = ttk.Entry(p, **kw); e.insert(0, d); return e

    def _combo(self, p, vals, d, **kw):
        c = ttk.Combobox(p, values=vals, state="readonly", **kw); c.set(d); return c

    def _on_params_changed(self):
        self.update_video_length()
        self.redraw_waveform()
        self.save_session_state()

    def _effective_image_list(self):
        """
        Returns self.image_data with the title screen (if enabled) prepended
        as a virtual item, so duration/segment calculations automatically
        include it and its transition into image #1.
        """
        items = list(self.image_data)
        if self.title_screen and self.title_screen.get("enabled"):
            title_dur = float(self.title_screen.get("duration", 4.0))
            items = [{"path": None, "rotation": 0,
                      "duration": title_dur, "is_title": True}] + items
        return items

    def update_video_length(self):
        effective = self._effective_image_list()
        if not effective:
            self.lbl_video_len.config(text="—"); return
        try:
            img_dur   = float(self.duration_entry.get())
            trans_dur = float(self.trans_dur_entry.get())
            tt        = self.trans_combo.get()
            n         = len(effective)
        except ValueError:
            self.lbl_video_len.config(text="—"); return
        total = calc_video_duration(n, img_dur, trans_dur, tt, effective)
        self.lbl_video_len.config(text=f"{fmt_time(total)}  ({total:.1f}s)")

    # ═══════════════════════════════════════════════════════════════
    # RIGHT PANEL
    # ═══════════════════════════════════════════════════════════════
    def setup_right_panel(self):
        ttk.Label(self.right_frame, text="Timeline Preview",
                  font=('Helvetica', 12, 'bold')).pack(anchor=tk.W, pady=(0, 8))
        self.preview_canvas = tk.Canvas(self.right_frame, bg="#2d2d2d", width=560, height=360)
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        nf = ttk.Frame(self.right_frame); nf.pack(pady=6)
        ttk.Button(nf, text="◀ Prev", command=lambda: self.navigate_preview(-1)).pack(side=tk.LEFT, padx=5)
        ttk.Button(nf, text="Next ▶", command=lambda: self.navigate_preview(1)).pack(side=tk.LEFT, padx=5)
        self.lbl_status = ttk.Label(self.right_frame, text="No folder loaded.",
                                     font=('Helvetica', 9, 'italic'))
        self.lbl_status.pack(pady=4)

    # ═══════════════════════════════════════════════════════════════
    # THUMBNAIL STRIP
    # ═══════════════════════════════════════════════════════════════
    def setup_timeline_strip(self):
        ttk.Label(self.strip_outer,
                  text="Timeline  (drag to reorder  |  right-click for options  |  [ ] rotate  ← → move  Del delete  D duplicate)",
                  font=('Helvetica', 9, 'bold')).pack(anchor=tk.W)
        sf = ttk.Frame(self.strip_outer); sf.pack(fill=tk.X)
        self.h_scroll = ttk.Scrollbar(sf, orient=tk.HORIZONTAL)
        self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.strip_canvas = tk.Canvas(sf,
            height=THUMB_H + THUMB_PAD * 2 + 20, bg="#1e1e1e",
            xscrollcommand=self.h_scroll.set, highlightthickness=0)
        self.strip_canvas.pack(fill=tk.X)
        self.h_scroll.config(command=self.strip_canvas.xview)
        self.strip_canvas.bind("<ButtonPress-1>",  self._strip_press)
        self.strip_canvas.bind("<B1-Motion>",       self._strip_drag)
        self.strip_canvas.bind("<ButtonRelease-1>", self._strip_release)
        self.strip_canvas.bind("<Button-3>",        self._show_strip_context_menu)  # right-click
        # macOS two-finger click / Ctrl+click
        self.strip_canvas.bind("<Control-Button-1>", self._show_strip_context_menu)

    # ═══════════════════════════════════════════════════════════════
    # KEYBOARD SHORTCUTS
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _is_typing_target(widget):
        """
        True if the currently focused widget is something the user could
        be typing into (Entry, Spinbox, Combobox, Text) — in which case
        global single-key shortcuts (f, c, d, Delete, arrows, etc.) must
        NOT fire, or they'll eat keystrokes / delete timeline images
        while the user is just editing a number field.
        """
        if widget is None:
            return False
        cls = widget.winfo_class()
        return cls in ("Entry", "TEntry", "Spinbox", "TSpinbox",
                       "TCombobox", "Text", "TText")

    def _guarded(self, handler):
        """
        Wrap a shortcut handler so it's skipped (and the keystroke passed
        through normally) whenever an editable field currently has focus.
        """
        def wrapped(event):
            if self._is_typing_target(self.root.focus_get()):
                return None  # let the widget handle the key itself
            handler()
            return "break"
        return wrapped

    def _setup_shortcuts(self):
        """Bind global keyboard shortcuts (skipped while typing in any entry field)."""
        self.root.bind("<Left>",         self._guarded(lambda: self._kb_move(-1)))
        self.root.bind("<Right>",        self._guarded(lambda: self._kb_move(1)))
        self.root.bind("<bracketleft>",  self._guarded(lambda: self.rotate_selected(90)))
        self.root.bind("<bracketright>", self._guarded(lambda: self.rotate_selected(-90)))
        self.root.bind("<Delete>",       self._guarded(self.delete_selected))
        self.root.bind("<BackSpace>",    self._guarded(self.delete_selected))
        self.root.bind("d",              self._guarded(self.duplicate_selected))
        self.root.bind("D",              self._guarded(self.duplicate_selected))
        self.root.bind("c",              self._guarded(self.open_crop_editor))
        self.root.bind("C",              self._guarded(self.open_crop_editor))
        self.root.bind("f",              self._guarded(self.autocrop_169))
        self.root.bind("F",              self._guarded(self.autocrop_169))
        self.root.bind("<Control-Shift-F>", self._guarded(self.autocrop_169_all))
        self.root.bind("<Control-Shift-f>", self._guarded(self.autocrop_169_all))
        # also allow arrow keys when the strip canvas has focus
        self.strip_canvas.bind("<Left>",         self._guarded(lambda: self._kb_move(-1)))
        self.strip_canvas.bind("<Right>",        self._guarded(lambda: self._kb_move(1)))
        self.strip_canvas.bind("<bracketleft>",  self._guarded(lambda: self.rotate_selected(90)))
        self.strip_canvas.bind("<bracketright>", self._guarded(lambda: self.rotate_selected(-90)))
        self.strip_canvas.bind("<Delete>",       self._guarded(self.delete_selected))
        self.strip_canvas.bind("d",              self._guarded(self.duplicate_selected))


    def _kb_move(self, direction):
        """Arrow keys: move the selected image left/right in the timeline."""
        if not self.image_data: return
        idx = self.current_preview_idx
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.image_data): return
        self.image_data[idx], self.image_data[new_idx] = \
            self.image_data[new_idx], self.image_data[idx]
        self.current_preview_idx = new_idx
        self.save_session_state()
        self.refresh_listbox(); self.refresh_strip(); self.redraw_waveform()

    # ═══════════════════════════════════════════════════════════════
    # RIGHT-CLICK CONTEXT MENU ON THUMBNAIL STRIP
    # ═══════════════════════════════════════════════════════════════
    def _show_strip_context_menu(self, event):
        cx  = self.strip_canvas.canvasx(event.x)
        idx = self._strip_idx_at(cx)
        if idx is None: return
        # Select the right-clicked thumbnail first
        self.current_preview_idx = idx
        self.refresh_listbox(); self.update_preview(); self.refresh_strip()

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="🎬 Edit Title Screen…",
                         command=self.open_title_editor)
        menu.add_separator()
        menu.add_command(label="⟲  Rotate Left  ([)",
                         command=lambda: self.rotate_selected(90))
        menu.add_command(label="⟳  Rotate Right  (])",
                         command=lambda: self.rotate_selected(-90))
        menu.add_command(label="✂  Crop / Zoom",
                         command=self.open_crop_editor)
        menu.add_command(label="✂  Auto Crop 16:9  (F)",
                         command=self.autocrop_169)
        menu.add_command(label="✂✂  Auto Crop 16:9 — ALL  (Ctrl+Shift+F)",
                         command=self.autocrop_169_all)
        menu.add_command(label="⏱  Set Duration…",
                         command=self.open_duration_editor)
        menu.add_separator()
        menu.add_command(label="◀  Move Left  (←)",
                         command=lambda: self._kb_move(-1),
                         state=tk.NORMAL if idx > 0 else tk.DISABLED)
        menu.add_command(label="▶  Move Right  (→)",
                         command=lambda: self._kb_move(1),
                         state=tk.NORMAL if idx < len(self.image_data) - 1 else tk.DISABLED)
        menu.add_separator()
        menu.add_command(label="❐  Duplicate  (D)",
                         command=self.duplicate_selected)
        menu.add_separator()
        menu.add_command(label="✕  Delete  (Del)",
                         command=self.delete_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _thumb_key(self, item):
        c  = item.get("crop")
        return (item["path"], item["rotation"],
                c if c is None else tuple(c))

    def _get_thumb(self, item):
        key = self._thumb_key(item)
        if key not in self._thumb_cache:
            try:
                img = Image.open(item["path"])
                img = rotate_img(img, item["rotation"])
                # Apply crop if set
                crop = item.get("crop")
                if crop:
                    iw, ih = img.size
                    nx, ny, nw, nh = crop
                    img = img.crop((int(nx * iw), int(ny * ih),
                                    int((nx + nw) * iw), int((ny + nh) * ih)))
                img.thumbnail((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
                bg = Image.new("RGB", (THUMB_W, THUMB_H), (30, 30, 30))
                bg.paste(img, ((THUMB_W - img.size[0]) // 2, (THUMB_H - img.size[1]) // 2))
                self._thumb_cache[key] = ImageTk.PhotoImage(bg)
            except Exception:
                self._thumb_cache[key] = None
        return self._thumb_cache[key]

    def refresh_strip(self):
        self.strip_canvas.delete("all")
        if not self.image_data:
            self.strip_canvas.config(scrollregion=(0, 0, 0, 0)); return
        total_w = len(self.image_data) * (THUMB_W + THUMB_PAD) + THUMB_PAD
        self.strip_canvas.config(scrollregion=(0, 0, total_w, THUMB_H + THUMB_PAD * 2 + 18))
        for i, item in enumerate(self.image_data):
            x0 = THUMB_PAD + i * (THUMB_W + THUMB_PAD)
            y0 = THUMB_PAD
            if i == self.current_preview_idx:
                self.strip_canvas.create_rectangle(
                    x0-3, y0-3, x0+THUMB_W+3, y0+THUMB_H+3,
                    outline="#4da6ff", width=2, fill="")
            ph = self._get_thumb(item)
            if ph: self.strip_canvas.create_image(x0, y0, anchor=tk.NW, image=ph)
            lbl = os.path.basename(item["path"])
            if len(lbl) > 13: lbl = lbl[:11] + "…"
            if item["rotation"]: lbl += f" {item['rotation']}°"
            # Show custom duration as a badge in top-right corner of thumbnail
            custom_dur = item.get("duration")
            if custom_dur is not None:
                dur_str = f"{custom_dur:.1f}s"
                bx = x0 + THUMB_W - 2
                by = y0 + 2
                self.strip_canvas.create_rectangle(
                    bx - len(dur_str)*5 - 4, by,
                    bx, by + 12,
                    fill="#ff8800", outline="")
                self.strip_canvas.create_text(
                    bx - 2, by + 6,
                    text=dur_str, fill="white",
                    font=("Helvetica", 6, "bold"), anchor=tk.E)
                lbl += f" ⏱{dur_str}"
            self.strip_canvas.create_text(
                x0 + THUMB_W // 2, y0 + THUMB_H + 4,
                text=lbl, fill="#aaaaaa", font=("Helvetica", 7), anchor=tk.N)
        sel_x = THUMB_PAD + self.current_preview_idx * (THUMB_W + THUMB_PAD)
        self.strip_canvas.xview_moveto(max(0, sel_x / max(total_w, 1) - 0.1))

    def _strip_idx_at(self, cx):
        if not self.image_data: return None
        s = int(cx // (THUMB_W + THUMB_PAD))
        return s if 0 <= s < len(self.image_data) else None

    def _strip_press(self, event):
        self.strip_canvas.focus_set()   # so Delete/shortcuts work right after clicking a thumb
        cx = self.strip_canvas.canvasx(event.x)
        idx = self._strip_idx_at(cx)
        if idx is not None:
            self._drag_idx = idx; self.current_preview_idx = idx
            self.refresh_listbox(); self.update_preview(); self.refresh_strip()

    def _strip_drag(self, event):
        if self._drag_idx is None: return
        cx = self.strip_canvas.canvasx(event.x)
        t = self._strip_idx_at(cx)
        if t is not None and t != self._drag_idx:
            self.image_data[self._drag_idx], self.image_data[t] = \
                self.image_data[t], self.image_data[self._drag_idx]
            self.current_preview_idx = t; self._drag_idx = t
            self.save_session_state()
            self.refresh_listbox(); self.refresh_strip(); self.redraw_waveform()

    def _strip_release(self, event): self._drag_idx = None

    # ═══════════════════════════════════════════════════════════════
    # WAVEFORM / VU PANEL  (time-aligned, sits below the strip)
    # ═══════════════════════════════════════════════════════════════
    def setup_vu_panel(self):
        hdr = ttk.Frame(self.vu_outer); hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="Audio Waveform  —  time-aligned to video",
                  font=('Helvetica', 9, 'bold')).pack(side=tk.LEFT)
        self.lbl_peak = ttk.Label(hdr, text="", font=('Helvetica', 8), foreground="#888888")
        self.lbl_peak.pack(side=tk.RIGHT)

        # Zoom controls (right side of header)
        zf = ttk.Frame(hdr); zf.pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(zf, text="−", width=2,
                   command=lambda: self._vu_zoom_by(1/1.5)).pack(side=tk.LEFT)
        self.lbl_vu_zoom = ttk.Label(zf, text="1×", width=4,
                                      font=("Helvetica", 8), foreground="#888888",
                                      anchor=tk.CENTER)
        self.lbl_vu_zoom.pack(side=tk.LEFT, padx=2)
        ttk.Button(zf, text="+", width=2,
                   command=lambda: self._vu_zoom_by(1.5)).pack(side=tk.LEFT)
        ttk.Button(zf, text="↺", width=2,
                   command=lambda: self._vu_zoom_by(None)).pack(side=tk.LEFT, padx=(4, 0))

        # scrollable waveform canvas (same scrollbar logic as strip)
        wf = ttk.Frame(self.vu_outer); wf.pack(fill=tk.X)
        self.vu_hscroll = ttk.Scrollbar(wf, orient=tk.HORIZONTAL)
        self.vu_hscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.vu_canvas = tk.Canvas(wf, bg=VU_BG, height=VU_H,
                                    xscrollcommand=self.vu_hscroll.set,
                                    highlightthickness=1,
                                    highlightbackground="#333333")
        self.vu_canvas.pack(fill=tk.X)
        self.vu_hscroll.config(command=self.vu_canvas.xview)
        self.vu_canvas.bind("<Configure>", lambda e: self.redraw_waveform())
        # Scroll-wheel zoom (Ctrl+wheel) and plain-wheel horizontal scroll
        self.vu_canvas.bind("<Control-MouseWheel>", self._vu_on_ctrl_scroll)
        self.vu_canvas.bind("<Control-Button-4>",   self._vu_on_ctrl_scroll)
        self.vu_canvas.bind("<Control-Button-5>",   self._vu_on_ctrl_scroll)
        self.vu_canvas.bind("<MouseWheel>",          self._vu_on_wheel)
        self.vu_canvas.bind("<Button-4>",            self._vu_on_wheel)
        self.vu_canvas.bind("<Button-5>",            self._vu_on_wheel)

        # info row below waveform
        info = ttk.Frame(self.vu_outer); info.pack(fill=tk.X, pady=(2, 0))
        self.lbl_audio_dur = ttk.Label(info, text="—", font=('Helvetica', 8), foreground="#888888")
        self.lbl_audio_dur.pack(side=tk.LEFT)
        # legend
        for col, lbl in (("#4da6ff", "▎image"), ("#666666", "▎transition"), ("#00cc66", "▎audio")):
            ttk.Label(info, text=lbl, font=('Helvetica', 7), foreground=col).pack(side=tk.RIGHT, padx=4)
        ttk.Label(info, text="Ctrl+scroll or +/− to zoom",
                  font=("Helvetica", 7, "italic"), foreground="#555555").pack(side=tk.RIGHT, padx=8)

        self._draw_vu_placeholder("No audio loaded")

    def _vu_zoom_by(self, factor):
        """Multiply zoom by factor, or reset to 1.0 if factor is None."""
        if factor is None:
            self._vu_zoom = 1.0
        else:
            self._vu_zoom = max(0.25, min(32.0, self._vu_zoom * factor))
        # Update label
        z = self._vu_zoom
        self.lbl_vu_zoom.config(text=f"{z:.1f}×" if z != int(z) else f"{int(z)}×")
        self.redraw_waveform()

    def _vu_on_ctrl_scroll(self, event):
        """Ctrl+scroll = zoom in/out, centred on the mouse position."""
        delta = -1 if (getattr(event, "delta", 0) < 0 or
                        getattr(event, "num", 0) == 5) else 1
        # Remember canvas x position under cursor before zoom
        cx = self.vu_canvas.canvasx(event.x)
        old_zoom = self._vu_zoom
        self._vu_zoom_by(1.25 if delta > 0 else 1/1.25)
        # Adjust scroll so the point under the cursor stays put
        new_zoom = self._vu_zoom
        if old_zoom > 0:
            ratio = new_zoom / old_zoom
            new_cx = cx * ratio
            scroll_target = (new_cx - event.x) / max(
                self.vu_canvas.winfo_width(), 1)
            total_w = self.vu_canvas.bbox("all")
            if total_w:
                self.vu_canvas.xview_moveto(
                    max(0.0, scroll_target / ((total_w[2] - total_w[0]) /
                        max(self.vu_canvas.winfo_width(), 1))))

    def _vu_on_wheel(self, event):
        """Plain scroll = horizontal pan."""
        delta = -1 if (getattr(event, "delta", 0) < 0 or
                        getattr(event, "num", 0) == 5) else 1
        self.vu_canvas.xview_scroll(-delta, "units")

    def _draw_vu_placeholder(self, msg):
        self.vu_canvas.delete("all")
        self.vu_canvas.config(scrollregion=(0, 0, self.vu_canvas.winfo_width() or 800, VU_H))
        cw = self.vu_canvas.winfo_width() or 800
        self.vu_canvas.create_text(cw // 2, VU_H // 2,
            text=msg, fill=VU_TEXT_COL, font=("Helvetica", 9, "italic"))

    def redraw_waveform(self):
        """
        Render the waveform canvas. The x-axis represents the video timeline.
        Draws segment markers and ruler even when no audio is loaded.
        """
        # ── current params ────────────────────────────────────────
        try:
            img_dur   = float(self.duration_entry.get())
            trans_dur = float(self.trans_dur_entry.get())
            tt        = self.trans_combo.get()
        except ValueError:
            img_dur, trans_dur, tt = 3.0, 1.0, "Crossfade"

        effective = self._effective_image_list()
        n         = len(effective)
        video_dur = calc_video_duration(n, img_dur, trans_dur, tt, effective) if n > 0 else self._audio_dur
        safe_fade = min(trans_dur, img_dur / 2)
        segs      = segment_times(n, img_dur, trans_dur, tt, effective) if n > 0 else []

        # No audio and no images — show placeholder
        if self._audio_rms is None and n == 0:
            self._draw_vu_placeholder("No audio loaded")
            return

        # ── canvas sizing ─────────────────────────────────────────
        ref_dur    = max(video_dur, self._audio_dur if self._audio_dur else video_dur, 1.0)
        base_pps   = max(80, (self.vu_canvas.winfo_width() or 800) / ref_dur)
        px_per_sec = base_pps * self._vu_zoom
        canvas_w   = int(ref_dur * px_per_sec) + 40
        canvas_h   = VU_H

        self.vu_canvas.config(scrollregion=(0, 0, canvas_w, canvas_h))
        self.vu_canvas.delete("all")

        cy = canvas_h // 2

        # ── dB guide lines (only if audio loaded) ────────────────
        if self._audio_rms is not None:
            for db in (-6, -12, -20):
                lin = 10 ** (db / 20)
                yp  = int(cy - lin * cy * 0.88)
                self.vu_canvas.create_line(0, yp, canvas_w, yp, fill="#1c1c1c", dash=(2, 4))
                self.vu_canvas.create_text(3, yp - 1, text=f"{db}dB",
                    fill="#2e2e2e", font=("Helvetica", 6), anchor=tk.SW)

        # ── waveform bars ─────────────────────────────────────────
        if self._audio_rms is not None and self._audio_dur > 0:
            rms   = self._audio_rms
            n_rms = len(rms)
            bar_w_px = max(1.0, (self._audio_dur / n_rms) * px_per_sec)
            threshold = 10 ** (-3 / 20)
            for i, v in enumerate(rms):
                x   = i * bar_w_px
                amp = v * cy * 0.88
                col = VU_OVER_COL if v >= threshold else VU_WAVE_COL
                self.vu_canvas.create_rectangle(
                    x, cy - amp, x + max(1, bar_w_px - 0.5), cy + amp,
                    fill=col, outline="")

        # ── clip line ─────────────────────────────────────────────
        if self._audio_rms is not None:
            self.vu_canvas.create_line(0, 1, canvas_w, 1, fill=VU_CLIP_LINE, width=1)

        # ── transition zone shading ───────────────────────────────
        if n > 1 and tt not in ("None", "Fade In/Out") and safe_fade > 0:
            if tt in ("Crossfade", "Ken Burns"):
                # overlap zones: end of seg[i] overlaps start of seg[i+1]
                for i in range(len(segs) - 1):
                    zone_start = segs[i + 1][0]          # clip i+1 starts here
                    zone_end   = segs[i][1]               # clip i ends here
                    # For overlapping transitions zone_end > zone_start
                    xs = zone_start * px_per_sec
                    xe = segs[i][1] * px_per_sec
                    if xe > xs:
                        self.vu_canvas.create_rectangle(
                            xs, 2, xe, canvas_h - 14,
                            fill="#ffffff", outline="", stipple="gray25")
                        self.vu_canvas.create_line(xs, 0, xs, canvas_h - 14,
                            fill=VU_SEG_LINE, width=1, dash=(3, 3))
            elif tt == "Blur Dissolve":
                # inserted clips: gap between seg[i].end and seg[i+1].start
                for i in range(len(segs) - 1):
                    xs = segs[i][1] * px_per_sec
                    xe = segs[i + 1][0] * px_per_sec
                    if xe > xs:
                        self.vu_canvas.create_rectangle(
                            xs, 2, xe, canvas_h - 14,
                            fill="#ffffff", outline="", stipple="gray25")
                        self.vu_canvas.create_line(xs, 0, xs, canvas_h - 14,
                            fill=VU_SEG_LINE, width=1, dash=(3, 3))

        # ── image segment boundary lines ──────────────────────────
        for i, (s_start, s_end) in enumerate(segs):
            # left edge of each segment
            x_start = s_start * px_per_sec
            x_end   = s_end   * px_per_sec
            is_title_seg = (i < len(effective) and effective[i].get("is_title"))
            # solid line at start of each clip (title gets a distinct colour)
            self.vu_canvas.create_line(
                x_start, 0, x_start, canvas_h - 14,
                fill=("#ffcc00" if is_title_seg else VU_SEG_LINE), width=1)
            # label: "T" for title screen, otherwise image number
            label = "T" if is_title_seg else f"#{i}"
            self.vu_canvas.create_text(
                x_start + 4, 4,
                text=label, fill=("#ffcc00" if is_title_seg else VU_SEG_LINE),
                font=("Helvetica", 6, "bold" if is_title_seg else "normal"),
                anchor=tk.NW)

        # right edge of last segment
        if segs:
            x_last_end = segs[-1][1] * px_per_sec
            self.vu_canvas.create_line(
                x_last_end, 0, x_last_end, canvas_h - 14,
                fill=VU_SEG_LINE, width=1)

        # ── video end marker ──────────────────────────────────────
        xve = video_dur * px_per_sec
        self.vu_canvas.create_line(xve, 0, xve, canvas_h, fill="#ffcc00", width=2)
        self.vu_canvas.create_text(xve + 3, 3,
            text=fmt_time(video_dur), fill="#ffcc00",
            font=("Helvetica", 7), anchor=tk.NW)

        # ── audio fade-out zone ───────────────────────────────────
        if self._audio_rms is not None and self._audio_dur > 0 and video_dur > 0:
            fade_out_dur = min(3.0, video_dur / 2)
            fade_start_x = (video_dur - fade_out_dur) * px_per_sec
            fade_end_x   = video_dur * px_per_sec
            # draw as hatched orange band to signal the fade region
            self.vu_canvas.create_rectangle(
                fade_start_x, 2, fade_end_x, canvas_h - 14,
                fill="#ff8800", outline="", stipple="gray12")
            self.vu_canvas.create_text(
                fade_start_x + 3, canvas_h - 26,
                text=f"fade {fade_out_dur:.1f}s", fill="#ff8800",
                font=("Helvetica", 6), anchor=tk.SW)
        if self._audio_rms is not None and self._audio_dur > video_dur + 0.5:
            xae = self._audio_dur * px_per_sec
            self.vu_canvas.create_line(xae, 0, xae, canvas_h, fill="#888888", width=1, dash=(4, 4))
            self.vu_canvas.create_text(xae + 3, 3,
                text=fmt_time(self._audio_dur), fill="#888888",
                font=("Helvetica", 7), anchor=tk.NW)

        # ── time ruler ────────────────────────────────────────────
        ruler_y = canvas_h - 13
        total_t = max(video_dur, self._audio_dur if self._audio_dur else video_dur)
        # choose tick interval
        for interval in (1, 2, 5, 10, 15, 30, 60):
            if total_t / interval <= canvas_w / 40:
                break
        t = 0.0
        while t <= total_t + 0.01:
            x = t * px_per_sec
            is_major = (round(t) % max(1, interval * 2) == 0)
            tick_h = 6 if is_major else 3
            self.vu_canvas.create_line(x, ruler_y, x, ruler_y + tick_h,
                fill="#444444", width=1)
            if is_major:
                self.vu_canvas.create_text(x, ruler_y + tick_h + 1,
                    text=fmt_time(t), fill="#555555",
                    font=("Helvetica", 6), anchor=tk.N)
            t += interval

        # ── centre baseline ───────────────────────────────────────
        self.vu_canvas.create_line(0, cy, canvas_w, cy, fill="#1e1e1e", width=1)

    def _analyse_audio_thread(self, path):
        try:
            audio = AudioFileClip(path)
            self._audio_dur = audio.duration
            sr   = audio.fps
            self._audio_sr = sr
            data = audio.to_soundarray(fps=sr)
            audio.close()

            if data.ndim > 1:
                data = data.mean(axis=1)
            if data.dtype.kind == 'i':
                data = data.astype(np.float32) / np.iinfo(data.dtype).max
            else:
                data = data.astype(np.float32)

            peak_linear = np.abs(data).max()
            peak_db     = 20 * np.log10(peak_linear + 1e-9)

            # target ~1200 RMS buckets for fine resolution
            n_buckets = 1200
            chunk  = max(1, len(data) // n_buckets)
            n_full = (len(data) // chunk) * chunk
            rms    = np.sqrt((data[:n_full].reshape(-1, chunk) ** 2).mean(axis=1))
            mx = rms.max()
            if mx > 0: rms /= mx
            self._audio_rms = rms

            db_str = f"{peak_db:.1f} dBFS"
            col    = "#ff4444" if peak_db > -3 else "#888888"

            self.root.after(0, lambda d=db_str, c=col: (
                self.lbl_peak.config(text=f"Peak: {d}", foreground=c),
                self.lbl_audio_dur.config(
                    text=f"Audio: {fmt_time(self._audio_dur)}  ({self._audio_dur:.1f}s)"),
                self.redraw_waveform()
            ))
        except Exception as ex:
            self.root.after(0, lambda m=str(ex):
                self._draw_vu_placeholder(f"Error: {m}"))

    # ═══════════════════════════════════════════════════════════════
    # DATA / SESSION
    # ═══════════════════════════════════════════════════════════════
    def load_folder(self):
        folder = filedialog.askdirectory()
        if not folder: return
        self.image_folder = folder
        valid = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff')
        found = [os.path.join(folder, f)
                 for f in sorted(os.listdir(folder)) if f.lower().endswith(valid)]
        if not found:
            messagebox.showerror("Error", "No valid images found!"); return
        sf = os.path.join(folder, ".still2video_session.json")
        if os.path.exists(sf):
            try:
                with open(sf, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                # Support both old format (plain list) and new format (dict with settings)
                if isinstance(saved, list):
                    image_list = saved
                    settings   = {}
                else:
                    image_list = saved.get("images", [])
                    settings   = saved.get("settings", {})
                self.image_data = [d for d in image_list if os.path.exists(d["path"])]
                # Migrate older sessions: fold custom_rotation into rotation
                for d in self.image_data:
                    d["rotation"] = norm_rot(d.get("rotation", 0)
                                             + d.pop("custom_rotation", 0.0))
                if not self.image_data:
                    self.image_data = [{"path": p, "rotation": 0} for p in found]
                # Restore settings into UI widgets
                if settings.get("image_duration") is not None:
                    self.duration_entry.delete(0, tk.END)
                    self.duration_entry.insert(0, str(settings["image_duration"]))
                if settings.get("transition_type") is not None:
                    self.trans_combo.set(settings["transition_type"])
                if settings.get("transition_duration") is not None:
                    self.trans_dur_entry.delete(0, tk.END)
                    self.trans_dur_entry.insert(0, str(settings["transition_duration"]))
                if settings.get("music_path") and os.path.exists(settings["music_path"]):
                    self.music_path = settings["music_path"]
                    self.lbl_music.config(
                        text=os.path.basename(self.music_path), foreground="black")
                    self._draw_vu_placeholder("Analysing audio…")
                    self.lbl_audio_dur.config(text="Analysing…")
                    self.lbl_peak.config(text="")
                    threading.Thread(
                        target=self._analyse_audio_thread,
                        args=(self.music_path,), daemon=True).start()
                if settings.get("title_screen"):
                    self.title_screen = settings["title_screen"]
            except Exception:
                self.image_data = [{"path": p, "rotation": 0} for p in found]
        else:
            self.image_data = [{"path": p, "rotation": 0} for p in found]
        self._thumb_cache.clear()
        self.current_preview_idx = 0
        self.refresh_listbox(); self.update_preview()
        self.refresh_strip(); self._on_params_changed()
        self.save_session_state()   # persist immediately after folder load

    def save_session_state(self):
        if not self.image_folder or not os.path.exists(self.image_folder): return
        try:
            settings = {
                "image_duration":    self.duration_entry.get(),
                "transition_type":   self.trans_combo.get(),
                "transition_duration": self.trans_dur_entry.get(),
                "music_path":        self.music_path if self.music_path else None,
                "title_screen":      self.title_screen,
            }
            payload = {"settings": settings, "images": self.image_data}
            with open(os.path.join(self.image_folder, ".still2video_session.json"),
                      'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=4)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Session save failed: {e}")
            try:
                self.lbl_status.config(text=f"Session save FAILED: {e}")
            except Exception:
                pass

    def load_music(self):
        path = filedialog.askopenfilename(
            title="Select Music Track",
            filetypes=[("Audio Files", "*.mp3 *.wav *.aac *.ogg *.flac"), ("All Files", "*.*")])
        if not path: return
        self.music_path = path
        self.lbl_music.config(text=os.path.basename(path), foreground="black")
        self._draw_vu_placeholder("Analysing audio…")
        self.lbl_audio_dur.config(text="Analysing…")
        self.lbl_peak.config(text="")
        self.save_session_state()
        threading.Thread(target=self._analyse_audio_thread, args=(path,), daemon=True).start()

    def clear_music(self):
        self.music_path = ""; self._audio_rms = None; self._audio_dur = 0.0
        self.lbl_music.config(text="No track selected.", foreground="red")
        self.lbl_audio_dur.config(text="—"); self.lbl_peak.config(text="")
        self._draw_vu_placeholder("No audio loaded")
        self.save_session_state()

    # ═══════════════════════════════════════════════════════════════
    # LISTBOX / NAVIGATION
    # ═══════════════════════════════════════════════════════════════
    def refresh_listbox(self):
        self.image_listbox.delete(0, tk.END)
        for item in self.image_data:
            n = os.path.basename(item["path"])
            r = f" ({item['rotation']}°)" if item["rotation"] else ""
            d = f" ⏱{item['duration']:.2f}s" if item.get("duration") is not None else ""
            self.image_listbox.insert(tk.END, f"{n}{r}{d}")
        if self.image_data:
            self.image_listbox.selection_clear(0, tk.END)
            self.image_listbox.selection_set(self.current_preview_idx)
            self.image_listbox.see(self.current_preview_idx)
            self.lbl_status.config(text=f"Timeline: {len(self.image_data)} images.")
        else:
            self.lbl_status.config(text="Timeline is empty.")

    def on_image_select(self, event):
        sel = self.image_listbox.curselection()
        if sel:
            self.current_preview_idx = int(sel[0])
            self.update_preview(); self.refresh_strip()

    def navigate_preview(self, direction):
        if not self.image_data: return
        self.current_preview_idx = (self.current_preview_idx + direction) % len(self.image_data)
        self.refresh_listbox(); self.update_preview(); self.refresh_strip()

    # ═══════════════════════════════════════════════════════════════
    # PREVIEW CANVAS
    # ═══════════════════════════════════════════════════════════════
    def update_preview(self):
        if not self.image_data:
            self.preview_canvas.delete("all"); return
        item = self.image_data[self.current_preview_idx]
        cw = self.preview_canvas.winfo_width()  or 560
        ch = self.preview_canvas.winfo_height() or 360
        if cw < 10: cw, ch = 560, 360
        img = Image.open(item["path"])
        img = rotate_img(img, item["rotation"])
        # Apply crop if set
        crop = item.get("crop")
        if crop:
            iw, ih = img.size
            nx, ny, nw, nh = crop
            img = img.crop((int(nx * iw), int(ny * ih),
                            int((nx + nw) * iw), int((ny + nh) * ih)))
        img.thumbnail((cw, ch), Image.Resampling.LANCZOS)
        self.tk_preview_img = ImageTk.PhotoImage(img)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(cw // 2, ch // 2, anchor=tk.CENTER,
                                          image=self.tk_preview_img)

    # ═══════════════════════════════════════════════════════════════
    # ROTATE / DELETE
    # ═══════════════════════════════════════════════════════════════
    def rotate_selected(self, angle):
        if not self.image_data: return
        item = self.image_data[self.current_preview_idx]
        item["rotation"] = norm_rot(item["rotation"] + angle)
        self._thumb_cache.pop(self._thumb_key(item), None)
        self.save_session_state()
        self.refresh_listbox(); self.update_preview(); self.refresh_strip()

    def delete_selected(self):
        if not self.image_data: return
        del self.image_data[self.current_preview_idx]
        if self.current_preview_idx >= len(self.image_data) and self.image_data:
            self.current_preview_idx = len(self.image_data) - 1
        elif not self.image_data:
            self.current_preview_idx = 0
        self.save_session_state()
        self.refresh_listbox(); self.update_preview()
        self.refresh_strip(); self._on_params_changed()

    def duplicate_selected(self):
        """Insert a copy of the selected image right after it."""
        if not self.image_data: return
        idx  = self.current_preview_idx
        copy = dict(self.image_data[idx])            # shallow copy of {path, rotation}
        self.image_data.insert(idx + 1, copy)
        self.current_preview_idx = idx + 1           # land on the new copy
        self.save_session_state()
        self.refresh_listbox(); self.update_preview()
        self.refresh_strip(); self._on_params_changed()

    # ═══════════════════════════════════════════════════════════════
    # CROP / ZOOM EDITOR  (with custom rotation)
    # ═══════════════════════════════════════════════════════════════
    def open_crop_editor(self):
        """Open a Toplevel dialog to draw/adjust a crop rectangle and set custom rotation."""
        if not self.image_data: return
        item = self.image_data[self.current_preview_idx]

        # ── Working state (mutable inside closures) ─────────────
        # Single stored value: item["rotation"].  In the editor it is split into
        # a 90-degree "base" and a fine offset (the slider) purely for convenience.
        _rot0 = float(item.get("rotation", 0))
        _base = round(_rot0 / 90.0) * 90
        work = {
            "base":       _base % 360,
            "custom_rot": round(_rot0 - _base, 2),   # fine offset, -45..45
        }

        MAX_CW, MAX_CH = 700, 520

        def build_display_image():
            """Load, snap-rotate, then custom-rotate, then fit to canvas."""
            img = Image.open(item["path"])
            img = rotate_img(img, work["base"] + work["custom_rot"])
            orig_w, orig_h = img.size
            scale = min(MAX_CW / orig_w, MAX_CH / orig_h, 1.0)
            dw = int(orig_w * scale)
            dh = int(orig_h * scale)
            return img.resize((dw, dh), Image.Resampling.LANCZOS), scale, dw, dh

        disp_img, scale, disp_w, disp_h = build_display_image()
        with Image.open(item["path"]) as _im0:
            src_w, src_h = _im0.size          # un-rotated source size

        # ── Window ──────────────────────────────────────────────────
        win = tk.Toplevel(self.root)
        win.title("Crop / Rotate  —  drag to select region")
        win.resizable(False, False)
        win.grab_set()

        ttk.Label(win,
                  text="Drag to draw crop  |  handles to resize  |  drag inside to move  |  scroll to zoom",
                  font=("Helvetica", 9, "italic"), foreground="#888888").pack(pady=(6, 2))

        # ── Canvas (fixed outer container so window doesn't resize on rotate) ──
        canvas_frame = tk.Frame(win, width=MAX_CW, height=MAX_CH, bg="#1e1e1e")
        canvas_frame.pack(padx=10, pady=4)
        canvas_frame.pack_propagate(False)

        canvas = tk.Canvas(canvas_frame, width=disp_w, height=disp_h,
                           cursor="crosshair", highlightthickness=1,
                           highlightbackground="#444444", bg="#1e1e1e")
        canvas.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        tk_img_ref = [None]

        def put_image(img):
            tk_img_ref[0] = ImageTk.PhotoImage(img)
            canvas.config(width=img.width, height=img.height)
            canvas.delete("bg_img")
            canvas.create_image(0, 0, anchor=tk.NW, image=tk_img_ref[0], tags="bg_img")

        put_image(disp_img)

        # ── Restore saved crop (normalised 0-1) ──────────────────
        saved = item.get("crop")
        if saved:
            nx, ny, nw, nh = saved
            rx0 = nx * disp_w;  ry0 = ny * disp_h
            rx1 = rx0 + nw * disp_w; ry1 = ry0 + nh * disp_h
        else:
            rx0, ry0, rx1, ry1 = 0, 0, disp_w, disp_h

        state = {
            "rx0": rx0, "ry0": ry0, "rx1": rx1, "ry1": ry1,
            "drag_mode": None,
            "drag_ox": 0, "drag_oy": 0,
            "drag_rx0": 0, "drag_ry0": 0,
            "drag_rx1": 0, "drag_ry1": 0,
        }
        HANDLE = 8
        TARGET = 16 / 9
        RATIO_TOL = 0.004          # within 0.4 % counts as "16:9"

        # Default ON when there is no crop yet or the saved crop is already 16:9
        if saved:
            _sr = (saved[2] * disp_w) / max(saved[3] * disp_h, 1e-6)
            _keep_default = abs(_sr - TARGET) / TARGET < 0.01
        else:
            _keep_default = True
        keep169 = tk.BooleanVar(value=_keep_default)

        rect_id     = [None]
        handle_ids  = []
        overlay_ids = []

        def clamp(v, lo, hi): return max(lo, min(hi, v))

        def handle_positions():
            x0, y0 = state["rx0"], state["ry0"]
            x1, y1 = state["rx1"], state["ry1"]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            return {
                "nw": (x0, y0), "n": (cx, y0), "ne": (x1, y0),
                "w":  (x0, cy),                "e":  (x1, cy),
                "sw": (x0, y1), "s": (cx, y1), "se": (x1, y1),
            }

        def hit_handle(mx, my):
            for key, (hx, hy) in handle_positions().items():
                if abs(mx - hx) <= HANDLE + 2 and abs(my - hy) <= HANDLE + 2:
                    return key
            return None

        def inside_rect(mx, my):
            return (state["rx0"] < mx < state["rx1"] and
                    state["ry0"] < my < state["ry1"])

        def redraw():
            cw = canvas.winfo_width()  or disp_w
            ch = canvas.winfo_height() or disp_h
            for cid in overlay_ids: canvas.delete(cid)
            overlay_ids.clear()
            if rect_id[0]: canvas.delete(rect_id[0])
            for hid in handle_ids: canvas.delete(hid)
            handle_ids.clear()

            x0, y0 = int(state["rx0"]), int(state["ry0"])
            x1, y1 = int(state["rx1"]), int(state["ry1"])

            for coords in [
                (0, 0, cw, y0), (0, y1, cw, ch),
                (0, y0, x0, y1), (x1, y0, cw, y1),
            ]:
                oid = canvas.create_rectangle(*coords, fill="black", outline="",
                                              stipple="gray50")
                overlay_ids.append(oid)

            _ww, _hh = (x1 - x0), (y1 - y0)
            ratio  = (_ww / _hh) if _hh > 0 else 0.0
            is169  = _hh > 0 and abs(ratio - TARGET) / TARGET < RATIO_TOL
            col    = "#2ecc71" if is169 else "#4da6ff"

            rid = canvas.create_rectangle(x0, y0, x1, y1,
                                           outline=col, width=2)
            rect_id[0] = rid

            for frac in (1/3, 2/3):
                gx = x0 + (x1 - x0) * frac
                gy = y0 + (y1 - y0) * frac
                overlay_ids.append(canvas.create_line(gx, y0, gx, y1,
                    fill=col, dash=(3, 4), width=1))
                overlay_ids.append(canvas.create_line(x0, gy, x1, gy,
                    fill=col, dash=(3, 4), width=1))

            for key, (hx, hy) in handle_positions().items():
                hid = canvas.create_rectangle(
                    hx - HANDLE, hy - HANDLE, hx + HANDLE, hy + HANDLE,
                    fill=col, outline="white", width=1)
                handle_ids.append(hid)

            pw = int((x1 - x0) / scale)
            ph = int((y1 - y0) / scale)
            lbl_id = canvas.create_text(x0 + 4, y1 - 4, anchor=tk.SW,
                text=(f"{pw}×{ph}px   {ratio:.3f}:1" +
                      ("   ✓ 16:9" if is169 else "")),
                fill=("#7dffb0" if is169 else "white"),
                font=("Helvetica", 9, "bold"))
            overlay_ids.append(lbl_id)

        redraw()

        # ── Rotation row ─────────────────────────────────────────
        rot_frame = ttk.Frame(win)
        rot_frame.pack(fill=tk.X, padx=10, pady=(2, 0))

        ttk.Label(rot_frame, text="Custom Rotation:").pack(side=tk.LEFT)
        rot_var = tk.DoubleVar(value=work["custom_rot"])
        rot_slider = ttk.Scale(rot_frame, from_=-4.5, to=4.5,
                               orient=tk.HORIZONTAL, variable=rot_var, length=340)
        rot_slider.pack(side=tk.LEFT, padx=(8, 6))

        rot_lbl = ttk.Label(rot_frame, text=f"{work['custom_rot']:+.1f}°", width=7)
        rot_lbl.pack(side=tk.LEFT)

        rot_entry_var = tk.StringVar(value=f"{work['custom_rot']:.1f}")
        rot_entry = ttk.Entry(rot_frame, textvariable=rot_entry_var, width=7)
        rot_entry.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(rot_frame, text="↺ 0°", width=5,
                   command=lambda: rot_var.set(0.0)).pack(side=tk.LEFT, padx=(6, 0))

        # Preset quick-rotate buttons (+/-90 change the base, the rest the fine offset)
        def _preset(d):
            if abs(d) == 90:
                work["base"] = (work["base"] + d) % 360
                _rebuild_canvas()
            else:
                rot_var.set(max(-45, min(45, rot_var.get() + d)))
        for deg in (-90, -5, -2, -1, 1, 2, 5, 90):
            ttk.Button(rot_frame, text=f"{deg:+d}°", width=4,
                       command=lambda d=deg: _preset(d)).pack(side=tk.LEFT, padx=1)

        def on_rot_slider(*_):
            v = round(rot_var.get(), 1)
            work["custom_rot"] = v
            rot_lbl.config(text=f"{v:+.1f}°")
            rot_entry_var.set(f"{v:.1f}")
            _rebuild_canvas()

        def on_rot_entry(*_):
            try:
                v = float(rot_entry_var.get())
                v = max(-45.0, min(45.0, v))
                rot_var.set(v)
                # on_rot_slider fires via trace
            except ValueError:
                pass

        rot_var.trace_add("write", on_rot_slider)
        rot_entry_var.trace_add("write", on_rot_entry)

        def _rebuild_canvas():
            """Re-render the image at the new rotation and rescale crop state."""
            nonlocal disp_img, scale, disp_w, disp_h
            old_w, old_h = disp_w, disp_h
            # Save crop as fractions of old size
            fx0 = state["rx0"] / max(old_w, 1)
            fy0 = state["ry0"] / max(old_h, 1)
            fx1 = state["rx1"] / max(old_w, 1)
            fy1 = state["ry1"] / max(old_h, 1)

            disp_img, scale, disp_w, disp_h = build_display_image()
            put_image(disp_img)

            # Re-map crop to new canvas size
            state["rx0"] = fx0 * disp_w; state["ry0"] = fy0 * disp_h
            state["rx1"] = fx1 * disp_w; state["ry1"] = fy1 * disp_h
            # Clamp to new bounds
            state["rx0"] = clamp(state["rx0"], 0, disp_w)
            state["ry0"] = clamp(state["ry0"], 0, disp_h)
            state["rx1"] = clamp(state["rx1"], 0, disp_w)
            state["ry1"] = clamp(state["ry1"], 0, disp_h)
            if keep169.get():
                fit_169()          # also redraws
            else:
                redraw()

        # ── Mouse events ─────────────────────────────────────────
        def on_press(event):
            mx, my = event.x, event.y
            hk = hit_handle(mx, my)
            if hk:
                state["drag_mode"] = hk
            elif inside_rect(mx, my):
                state["drag_mode"] = "move"
                state["drag_ox"]  = mx; state["drag_oy"]  = my
                state["drag_rx0"] = state["rx0"]; state["drag_ry0"] = state["ry0"]
                state["drag_rx1"] = state["rx1"]; state["drag_ry1"] = state["ry1"]
            else:
                state["drag_mode"] = "draw"
                state["rx0"] = state["rx1"] = mx
                state["ry0"] = state["ry1"] = my
            redraw()

        def on_drag(event):
            mx = clamp(event.x, 0, disp_w)
            my = clamp(event.y, 0, disp_h)
            mode = state["drag_mode"]
            if mode == "draw":
                state["rx1"] = mx; state["ry1"] = my
            elif mode == "move":
                dx = mx - state["drag_ox"]
                dy = my - state["drag_oy"]
                w = state["drag_rx1"] - state["drag_rx0"]
                h = state["drag_ry1"] - state["drag_ry0"]
                nx0 = clamp(state["drag_rx0"] + dx, 0, disp_w - w)
                ny0 = clamp(state["drag_ry0"] + dy, 0, disp_h - h)
                state["rx0"] = nx0; state["ry0"] = ny0
                state["rx1"] = nx0 + w; state["ry1"] = ny0 + h
            elif mode:
                hk = mode
                if "w" in hk: state["rx0"] = clamp(mx, 0, state["rx1"] - 10)
                if "e" in hk: state["rx1"] = clamp(mx, state["rx0"] + 10, disp_w)
                if "n" in hk: state["ry0"] = clamp(my, 0, state["ry1"] - 10)
                if "s" in hk: state["ry1"] = clamp(my, state["ry0"] + 10, disp_h)
            redraw()

        def on_release(event):
            state["rx0"], state["rx1"] = sorted([state["rx0"], state["rx1"]])
            state["ry0"], state["ry1"] = sorted([state["ry0"], state["ry1"]])
            if state["rx1"] - state["rx0"] < 10 or state["ry1"] - state["ry0"] < 10:
                state["rx0"] = 0; state["ry0"] = 0
                state["rx1"] = disp_w; state["ry1"] = disp_h
            state["drag_mode"] = None
            redraw()

        canvas.bind("<ButtonPress-1>",   on_press)
        canvas.bind("<B1-Motion>",       on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)

        CURSORS = {
            "nw": "top_left_corner",  "ne": "top_right_corner",
            "sw": "bottom_left_corner", "se": "bottom_right_corner",
            "n": "top_side", "s": "bottom_side",
            "e": "right_side", "w": "left_side",
        }
        def on_motion(event):
            hk = hit_handle(event.x, event.y)
            if hk:   canvas.config(cursor=CURSORS.get(hk, "crosshair"))
            elif inside_rect(event.x, event.y): canvas.config(cursor="fleur")
            else:    canvas.config(cursor="crosshair")
        canvas.bind("<Motion>", on_motion)

        def on_scroll(event):
            delta = -1 if (getattr(event, "delta", 0) < 0 or
                            getattr(event, "num", 0) == 5) else 1
            cx_ = (state["rx0"] + state["rx1"]) / 2
            cy_ = (state["ry0"] + state["ry1"]) / 2
            w = (state["rx1"] - state["rx0"]) * (0.9 if delta > 0 else 1.1)
            h = (state["ry1"] - state["ry0"]) * (0.9 if delta > 0 else 1.1)
            w = clamp(w, 20, disp_w); h = clamp(h, 20, disp_h)
            state["rx0"] = clamp(cx_ - w / 2, 0, disp_w - w)
            state["ry0"] = clamp(cy_ - h / 2, 0, disp_h - h)
            state["rx1"] = state["rx0"] + w
            state["ry1"] = state["ry0"] + h
            redraw()
        canvas.bind("<MouseWheel>", on_scroll)
        canvas.bind("<Button-4>",   on_scroll)
        canvas.bind("<Button-5>",   on_scroll)

        # ── 16:9 fit helper ──────────────────────────────────────
        def fit_169():
            """Largest centred 16:9 rectangle that lies entirely inside the
            rotated picture (i.e. excludes the black corner wedges)."""
            import math
            bw, bh = (src_h, src_w) if work["base"] % 180 else (src_w, src_h)
            W0, H0 = bw * scale, bh * scale            # picture size on canvas
            th = math.radians(work["custom_rot"])
            c, sn = abs(math.cos(th)), abs(math.sin(th))
            m = 0.0 if abs(th) < 1e-9 else 2.0         # safety margin vs. soft edge
            b = min((W0 / 2 - m) / (TARGET * c + sn),
                    (H0 / 2 - m) / (TARGET * sn + c))
            b = max(b, 1.0); a = TARGET * b
            cx, cy = disp_w / 2, disp_h / 2
            state["rx0"] = clamp(cx - a, 0, disp_w); state["rx1"] = clamp(cx + a, 0, disp_w)
            state["ry0"] = clamp(cy - b, 0, disp_h); state["ry1"] = clamp(cy + b, 0, disp_h)
            redraw()

        # ── Bottom buttons ────────────────────────────────────────
        bf = ttk.Frame(win); bf.pack(pady=(6, 10), padx=10, fill=tk.X)

        def do_reset():
            work["base"] = 0
            rot_var.set(0.0)
            _rebuild_canvas()
            state["rx0"] = 0; state["ry0"] = 0
            state["rx1"] = disp_w; state["ry1"] = disp_h
            redraw()

        def do_apply():
            # Save custom rotation (0 = remove key).
            # Read the live control values instead of trusting the trace-updated
            # cache, so the value saved always matches what the user sees.
            try:
                live = float(rot_var.get())
            except Exception:
                try:
                    live = float(rot_entry_var.get())
                except Exception:
                    live = work["custom_rot"]
            work["custom_rot"] = live
            cr = round(live, 2)
            item["rotation"] = norm_rot(work["base"] + cr)
            item.pop("custom_rotation", None)      # legacy key, no longer used

            # Save crop
            x0 = min(state["rx0"], state["rx1"])
            y0 = min(state["ry0"], state["ry1"])
            x1 = max(state["rx0"], state["rx1"])
            y1 = max(state["ry0"], state["ry1"])
            if x1 - x0 < 5 or y1 - y0 < 5 or \
               (x0 == 0 and y0 == 0 and x1 == disp_w and y1 == disp_h):
                item.pop("crop", None)
            else:
                item["crop"] = (x0 / disp_w, y0 / disp_h,
                                (x1 - x0) / disp_w, (y1 - y0) / disp_h)

            self._thumb_cache.pop(self._thumb_key(item), None)
            self.save_session_state()
            self.refresh_listbox(); self.update_preview(); self.refresh_strip()
            win.destroy()

        win.bind("f", lambda e: fit_169())
        win.bind("F", lambda e: fit_169())

        ttk.Button(bf, text="↺ Reset All",  width=12, command=do_reset).pack(side=tk.LEFT)
        ttk.Button(bf, text="⬛ Fit 16:9 (F)", width=14, command=fit_169).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(bf, text="Keep 16:9 on rotate", variable=keep169,
                        command=lambda: fit_169() if keep169.get() else None
                        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(bf, text="  Scroll to zoom",
                  font=("Helvetica", 8, "italic"), foreground="#888888").pack(side=tk.LEFT, expand=True)
        ttk.Button(bf, text="Cancel",   width=8, command=win.destroy).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(bf, text="✔ Apply",  width=8, command=do_apply).pack(side=tk.RIGHT)

    # ═══════════════════════════════════════════════════════════════
    # PER-SLIDE DURATION EDITOR
    # ═══════════════════════════════════════════════════════════════
    def open_duration_editor(self):
        """Popup to set a custom duration (float seconds) for the selected slide."""
        if not self.image_data: return
        idx  = self.current_preview_idx
        item = self.image_data[idx]
        name = os.path.basename(item["path"])

        try:
            default_dur = float(self.duration_entry.get())
        except ValueError:
            default_dur = 3.0

        current = item.get("duration")   # None = uses global default

        win = tk.Toplevel(self.root)
        win.title("Slide Duration")
        win.resizable(False, False)
        win.grab_set()

        pad = {"padx": 14, "pady": 6}

        ttk.Label(win, text=f"Slide:  {name}",
                  font=("Helvetica", 9, "bold")).pack(anchor=tk.W, **pad)
        ttk.Label(win,
                  text=f"Global default: {default_dur:.2f}s  —  enter a custom value\n"
                       f"or clear the field to restore the default.",
                  font=("Helvetica", 8, "italic"), foreground="#888888",
                  justify=tk.LEFT).pack(anchor=tk.W, padx=14, pady=(0, 6))

        ef = ttk.Frame(win); ef.pack(fill=tk.X, padx=14, pady=4)
        ttk.Label(ef, text="Duration (seconds):").pack(side=tk.LEFT)
        var = tk.StringVar(value=f"{current:.3f}".rstrip("0").rstrip(".")
                                  if current is not None else "")
        entry = ttk.Entry(ef, textvariable=var, width=10)
        entry.pack(side=tk.LEFT, padx=(8, 0))
        entry.focus_set()
        entry.selection_range(0, tk.END)

        # Live error hint
        hint = ttk.Label(win, text="", foreground="#ff6666",
                         font=("Helvetica", 8, "italic"))
        hint.pack(anchor=tk.W, padx=14)

        def validate_entry(*_):
            v = var.get().strip()
            if v == "":
                hint.config(text="(will use global default)")
                return True
            try:
                f = float(v)
                if f <= 0:
                    hint.config(text="Must be > 0"); return False
                hint.config(text=f"= {f:.3f}s"); return True
            except ValueError:
                hint.config(text="Not a valid number"); return False

        var.trace_add("write", validate_entry)
        validate_entry()

        bf = ttk.Frame(win); bf.pack(fill=tk.X, padx=14, pady=(8, 12))

        def do_apply():
            v = var.get().strip()
            if v == "":
                item.pop("duration", None)      # revert to global
            else:
                try:
                    f = float(v)
                    if f <= 0: return
                    item["duration"] = f
                except ValueError:
                    return
            self.save_session_state()
            self.refresh_listbox(); self._on_params_changed()
            win.destroy()

        def do_clear():
            var.set("")

        win.bind("<Return>", lambda e: do_apply())
        win.bind("<Escape>", lambda e: win.destroy())

        ttk.Button(bf, text="✕ Clear (use default)", command=do_clear).pack(side=tk.LEFT)
        ttk.Button(bf, text="Cancel", width=8, command=win.destroy).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(bf, text="✔ Apply", width=8, command=do_apply).pack(side=tk.RIGHT)

    # ═══════════════════════════════════════════════════════════════
    # AUTO-CROP TO 16:9 (no dialog — applies immediately)
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _compute_autocrop_169(item):
        """
        Compute the largest centred 16:9 crop rect for one image item.
        Returns the crop tuple (x, y, w, h) in normalised 0..1 coords,
        or None if the image can't be opened.
        """
        try:
            img = Image.open(item["path"])
        except Exception:
            return None
        img = rotate_img(img, item.get("rotation", 0))
        iw, ih = img.size
        if item.get("crop"):
            nx, ny, nw, nh = item["crop"]
        else:
            nx, ny, nw, nh = 0.0, 0.0, 1.0, 1.0
        rw = nw * iw; rh = nh * ih
        rx = nx * iw; ry = ny * ih
        tw = rw; th = round(tw * 9 / 16)
        if th > rh:
            th = rh; tw = round(th * 16 / 9)
        x0 = rx + (rw - tw) / 2
        y0 = ry + (rh - th) / 2
        return (x0 / iw, y0 / ih, tw / iw, th / ih)

    def autocrop_169(self):
        """Instantly crop the selected image to the largest centred 16:9 rectangle."""
        if not self.image_data: return
        item = self.image_data[self.current_preview_idx]
        crop = self._compute_autocrop_169(item)
        if crop is None: return
        item["crop"] = crop
        self._thumb_cache.pop(self._thumb_key(item), None)
        self.save_session_state()
        self.refresh_listbox(); self.update_preview(); self.refresh_strip()

    def autocrop_169_all(self):
        """Apply the largest centred 16:9 auto-crop to every image in the timeline."""
        if not self.image_data: return
        n = len(self.image_data)
        if not messagebox.askyesno(
            "Auto Crop All",
            f"Apply auto-crop 16:9 to all {n} images?\n"
            "This overwrites any existing crop on each image."):
            return
        applied = 0
        for item in self.image_data:
            crop = self._compute_autocrop_169(item)
            if crop is not None:
                item["crop"] = crop
                self._thumb_cache.pop(self._thumb_key(item), None)
                applied += 1
        self.save_session_state()
        self.refresh_listbox(); self.update_preview(); self.refresh_strip()
        self.lbl_status.config(text=f"Auto-cropped {applied} of {n} images to 16:9.")

    @staticmethod
    def _blur_dissolve_transition(clip_a, clip_b, duration, fps=24):
        frames = int(duration * fps)
        arr_a  = np.array(clip_a.get_frame(clip_a.duration - duration / 2))
        arr_b  = np.array(clip_b.get_frame(duration / 2))
        result_frames = []
        for i in range(frames):
            t = i / max(frames - 1, 1)
            pa = Image.fromarray(arr_a)
            r = int(t * 12)
            if r > 0: pa = pa.filter(ImageFilter.GaussianBlur(radius=r))
            pb = Image.fromarray(arr_b)
            rb = int((1 - t) * 12)
            if rb > 0: pb = pb.filter(ImageFilter.GaussianBlur(radius=rb))
            result_frames.append(np.array(Image.blend(pa, pb, alpha=t)))
        def make_frame(t_sec):
            return result_frames[min(int(t_sec * fps), len(result_frames) - 1)]
        return VideoClip(make_frame, duration=duration)

    @staticmethod
    def _ken_burns_clip(frame_array, duration, fps=24):
        H, W = frame_array.shape[:2]
        # Subtle pan - kept small to avoid jitter at low frame counts
        dx = random.choice([-1, 1]) * 0.015
        dy = random.choice([-1, 1]) * 0.010

        # Pre-compute every frame and cache as numpy arrays.
        # This eliminates per-frame PIL overhead AND floating-point
        # rounding jitter since each crop box is computed once.
        n_frames = max(1, int(round(duration * fps)))
        frames   = []
        for i in range(n_frames):
            progress = i / max(n_frames - 1, 1)      # 0.0 → 1.0, exact
            scale    = 1.08 - 0.08 * progress        # 1.08 → 1.00 (gentler zoom)

            # Use round() everywhere so sub-pixel motion is smooth,
            # not truncated to the nearest pixel boundary
            crop_w = round(W / scale)
            crop_h = round(H / scale)

            # clamp crop size to frame bounds
            crop_w = min(crop_w, W)
            crop_h = min(crop_h, H)

            cx = round(W / 2 + dx * W * progress)
            cy = round(H / 2 + dy * H * progress)

            x0 = max(0, min(cx - crop_w // 2, W - crop_w))
            y0 = max(0, min(cy - crop_h // 2, H - crop_h))
            x1 = x0 + crop_w
            y1 = y0 + crop_h

            cropped = frame_array[y0:y1, x0:x1]
            resized = np.array(
                Image.fromarray(cropped).resize((W, H), Image.Resampling.LANCZOS)
            )
            frames.append(resized)

        def make_frame(t):
            idx = min(int(round(t * fps)), len(frames) - 1)
            return frames[idx]

        return VideoClip(make_frame, duration=duration)

    # ═══════════════════════════════════════════════════════════════
    # EXPORT
    # ═══════════════════════════════════════════════════════════════
    def export_video(self):
        if not self.image_data:
            messagebox.showwarning("Warning", "Timeline is empty!"); return
        save_path = filedialog.asksaveasfilename(
            defaultextension=".mp4", filetypes=[("MP4 Video", "*.mp4")])
        if not save_path: return
        try:
            img_duration   = float(self.duration_entry.get())
            trans_duration = float(self.trans_dur_entry.get())
            trans_type     = self.trans_combo.get()
        except ValueError:
            messagebox.showerror("Error", "Duration values must be floats."); return

        self.progress_win = tk.Toplevel(self.root)
        self.progress_win.title("Exporting Video")
        self.progress_win.geometry("440x150")
        self.progress_win.resizable(False, False)
        self.progress_win.grab_set()
        self.progress_var = tk.DoubleVar()
        self.lbl_msg = ttk.Label(self.progress_win, text="Preparing…", font=('Helvetica', 10))
        self.lbl_msg.pack(pady=(20, 5), padx=20, anchor=tk.W)
        self.bar = ttk.Progressbar(self.progress_win, variable=self.progress_var, maximum=100)
        self.bar.pack(pady=10, fill=tk.X, padx=20)

        class TkProgressLogger(ProgressBarLogger):
            def __init__(self, bw, lw, pv):
                super().__init__(); self.bw = bw; self.lw = lw; self.pv = pv
            def bars_callback(self, bar_name, attr, value, old_value=None):
                total = self.bars[bar_name]['total']
                if total and total > 0:
                    pct = (value / total) * 100
                    self.bw.after(0, lambda p=pct: self.pv.set(p))
                    self.bw.after(0, lambda b=bar_name, p=pct:
                        self.lw.config(text=f"Encoding {b}: {int(p)}%"))

        def worker():
            try:
                TARGET_W, TARGET_H = 1920, 1080
                FPS   = 24
                raw_frames = []
                clip_durations = []   # per-image effective duration

                # ── Title screen (prepended if configured) ────────
                if self.title_screen and self.title_screen.get("enabled"):
                    ts       = self.title_screen
                    base_img = self.image_data[0]["path"] if self.image_data else None
                    self.progress_win.after(0, lambda: self.lbl_msg.config(
                        text="Rendering title screen…"))
                    title_pil  = self._render_title_frame(base_img, ts, TARGET_W, TARGET_H)
                    title_arr  = np.array(title_pil)
                    title_dur  = float(ts.get("duration", 4.0))
                    raw_frames.append(title_arr)
                    clip_durations.append(title_dur)

                total = len(self.image_data) + len(raw_frames)  # includes title screen if present
                img_count = len(self.image_data)
                for idx, item in enumerate(self.image_data):
                    self.progress_win.after(0, lambda i=idx+1: self.lbl_msg.config(
                        text=f"Loading image {i} of {img_count}…"))
                    img = Image.open(item["path"])
                    img = rotate_img(img, item["rotation"])
                    # Apply crop if set
                    crop = item.get("crop")
                    if crop:
                        iw, ih = img.size
                        nx, ny, nw, nh = crop
                        img = img.crop((int(nx * iw), int(ny * ih),
                                        int((nx + nw) * iw), int((ny + nh) * ih)))
                    img.thumbnail((TARGET_W, TARGET_H), Image.Resampling.LANCZOS)
                    bg = Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0))
                    bg.paste(img, ((TARGET_W - img.width) // 2, (TARGET_H - img.height) // 2))
                    raw_frames.append(np.array(bg))
                    clip_durations.append(_item_dur(item, img_duration))

                self.progress_win.after(0, lambda: self.lbl_msg.config(text="Building clips…"))

                if trans_type == "Blur Dissolve":
                    base_clips = [ImageClip(f).with_duration(d)
                                  for f, d in zip(raw_frames, clip_durations)]
                    parts = []
                    for i, clip in enumerate(base_clips):
                        parts.append(clip)
                        if i < len(base_clips) - 1:
                            safe_i = min(trans_duration, clip_durations[i] / 2,
                                         clip_durations[i + 1] / 2)
                            parts.append(self._blur_dissolve_transition(
                                base_clips[i], base_clips[i+1], safe_i, FPS))
                    final_video = concatenate_videoclips(parts, method="compose")
                elif trans_type == "Ken Burns":
                    kb_clips = [self._ken_burns_clip(f, d, FPS)
                                for f, d in zip(raw_frames, clip_durations)]
                    for i, clip in enumerate(kb_clips):
                        safe_i = min(trans_duration, clip_durations[i] / 2)
                        efx = []
                        if i > 0:         efx.append(vfx.CrossFadeIn(safe_i))
                        if i < total - 1: efx.append(vfx.CrossFadeOut(safe_i))
                        if efx: kb_clips[i] = clip.with_effects(efx)
                    # padding must be uniform for concatenate; use the smallest safe fade
                    min_safe = min(min(trans_duration, d / 2) for d in clip_durations)
                    final_video = concatenate_videoclips(kb_clips, method="compose",
                                                         padding=-min_safe)
                elif trans_type == "Crossfade":
                    clips = []
                    for i, (f, d) in enumerate(zip(raw_frames, clip_durations)):
                        clip  = ImageClip(f).with_duration(d)
                        safe_i = min(trans_duration, d / 2)
                        efx = []
                        if i > 0:         efx.append(vfx.CrossFadeIn(safe_i))
                        if i < total - 1: efx.append(vfx.CrossFadeOut(safe_i))
                        if efx: clip = clip.with_effects(efx)
                        clips.append(clip)
                    min_safe = min(min(trans_duration, d / 2) for d in clip_durations)
                    final_video = concatenate_videoclips(clips, method="compose",
                                                         padding=-min_safe)
                elif trans_type == "Fade In/Out":
                    clips = []
                    for f, d in zip(raw_frames, clip_durations):
                        safe_i = min(trans_duration, d / 2)
                        clips.append(ImageClip(f).with_duration(d).with_effects(
                            [vfx.FadeIn(safe_i), vfx.FadeOut(safe_i)]))
                    final_video = concatenate_videoclips(clips, method="compose")
                else:
                    clips = [ImageClip(f).with_duration(d)
                             for f, d in zip(raw_frames, clip_durations)]
                    final_video = concatenate_videoclips(clips, method="compose")

                # ── Automatic fade-to-black on the very last frame ────
                # Applied to the fully composed video (after all transitions),
                # so it always lands on the true end regardless of which
                # transition type was used.
                self.progress_win.after(0, lambda: self.lbl_msg.config(
                    text="Applying final fade to black…"))
                video_fade_out = min(3.0, final_video.duration / 2)
                final_video = final_video.with_effects([vfx.FadeOut(video_fade_out)])

                if self.music_path and os.path.exists(self.music_path):
                    self.progress_win.after(0, lambda: self.lbl_msg.config(text="Mixing audio…"))
                    video_dur    = final_video.duration
                    fade_out_dur = min(3.0, video_dur / 2)  # match the video fade-to-black
                    audio = AudioFileClip(self.music_path)
                    if audio.duration < video_dur:
                        from moviepy import concatenate_audioclips
                        copies = int(np.ceil(video_dur / audio.duration))
                        audio  = concatenate_audioclips([audio] * copies)
                    audio = audio.subclipped(0, video_dur)

                    # ── AudioFadeOut: try vfx first, then manual numpy fallback ──
                    fade_applied = False
                    # Try moviepy.audio.fx.AudioFadeOut (v2 correct path)
                    try:
                        from moviepy.audio.fx import AudioFadeOut as AFO
                        audio = audio.with_effects([AFO(fade_out_dur)])
                        fade_applied = True
                    except Exception:
                        pass
                    # Fallback: manual frame-level fade via make_frame
                    if not fade_applied:
                        try:
                            orig_make = audio.make_frame
                            dur       = audio.duration
                            def faded_frame(t, _omf=orig_make, _d=dur, _fd=fade_out_dur):
                                frame = _omf(t)
                                fade_start = _d - _fd
                                if t >= fade_start:
                                    gain = max(0.0, 1.0 - (t - fade_start) / _fd)
                                    frame = frame * gain
                                return frame
                            from moviepy import AudioClip
                            audio = AudioClip(faded_frame, duration=dur, fps=audio.fps)
                        except Exception as fe:
                            print(f"Audio fade fallback failed: {fe}")

                    final_video = final_video.with_audio(audio)

                gui_logger = TkProgressLogger(self.bar, self.lbl_msg, self.progress_var)
                final_video.write_videofile(save_path, fps=FPS, codec="h264_nvenc",
                                             logger=gui_logger)
                self.root.after(0, lambda: self._on_export_success(save_path))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda m=err: self._on_export_failed(m))

        threading.Thread(target=worker, daemon=True).start()

    # ═══════════════════════════════════════════════════════════════
    # TITLE SCREEN EDITOR
    # ═══════════════════════════════════════════════════════════════
    def open_title_editor(self):
        """Open the title screen popup editor."""
        TitleScreenEditor(self.root, self)

    def _render_title_frame(self, base_image_path, ts, W=1920, H=1080):
        """
        Render the title screen as a PIL Image at output resolution.
        base_image_path : path to the first image (used as blurred/darkened background)
        ts              : title_screen dict from TitleScreenEditor
        Returns a PIL Image (RGB).
        """
        from PIL import ImageDraw, ImageFont

        # ── Background ────────────────────────────────────────────
        bg_mode = ts.get("bg_mode", "blur")
        if base_image_path and os.path.exists(base_image_path):
            src = Image.open(base_image_path).convert("RGB")
            src_ratio    = src.width / src.height
            target_ratio = W / H
            if src_ratio > target_ratio:
                new_h = H; new_w = int(H * src_ratio)
            else:
                new_w = W; new_h = int(W / src_ratio)
            src = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
            x0 = (new_w - W) // 2; y0 = (new_h - H) // 2
            bg = src.crop((x0, y0, x0 + W, y0 + H))
        else:
            bg = Image.new("RGB", (W, H), (0, 0, 0))

        if bg_mode == "blur":
            radius = ts.get("blur_radius", 20)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=radius))
        elif bg_mode == "darken":
            factor = ts.get("darken_factor", 0.4)
            bg = Image.blend(Image.new("RGB", (W, H), (0, 0, 0)), bg, factor)
        elif bg_mode == "blur+darken":
            bg = bg.filter(ImageFilter.GaussianBlur(radius=ts.get("blur_radius", 20)))
            bg = Image.blend(Image.new("RGB", (W, H), (0, 0, 0)), bg,
                             ts.get("darken_factor", 0.5))
        elif bg_mode == "solid":
            col = ts.get("solid_color", "#000000")
            r, g, b = int(col[1:3],16), int(col[3:5],16), int(col[5:7],16)
            bg = Image.new("RGB", (W, H), (r, g, b))
        # "original" → keep bg unchanged

        draw = ImageDraw.Draw(bg)

        # ── Font loader ───────────────────────────────────────────
        font_face = ts.get("font_face", "arial")

        def _load_font(size, bold=False):
            candidates = []
            if bold:
                candidates = [
                    f"{font_face}bd.ttf", f"{font_face}b.ttf",
                    f"{font_face}-Bold.ttf", f"{font_face}bold.ttf",
                    "arialbd.ttf", "DejaVuSans-Bold.ttf",
                ]
            candidates += [
                f"{font_face}.ttf", f"{font_face}.otf",
                "arial.ttf", "DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
            ]
            for name in candidates:
                try:
                    return ImageFont.truetype(name, size)
                except Exception:
                    pass
            return ImageFont.load_default()

        # ── Layout params ─────────────────────────────────────────
        align   = ts.get("text_align", "center")
        v_pos   = ts.get("v_position", 0.5)
        shadow  = ts.get("shadow", True)
        padding = ts.get("line_padding", 20)
        margin  = int(W * 0.05)   # 5% side margin

        # ── Collect text lines (skip empty) ───────────────────────
        line_defs = [
            {"text": ts.get("title", ""),    "size": ts.get("title_size", 96),
             "color": ts.get("title_color", "#ffffff"), "bold": True,
             "is_title": True},
            {"text": ts.get("subtitle", ""), "size": ts.get("subtitle_size", 48),
             "color": ts.get("subtitle_color", "#cccccc"), "bold": False,
             "is_title": False},
            {"text": ts.get("date", ""),     "size": ts.get("date_size", 36),
             "color": ts.get("date_color", "#aaaaaa"), "bold": False,
             "is_title": False},
        ]

        # ── Horizontal rule settings ──────────────────────────────
        show_rule    = ts.get("show_rule", False)
        rule_color   = ts.get("rule_color", "#ffffff")
        rule_width_f = ts.get("rule_width", 0.6)    # fraction of W
        rule_thick   = ts.get("rule_thickness", 3)  # px
        rule_gap     = ts.get("rule_gap", 18)        # px above/below rule

        def _hex_to_rgb(col):
            try: return (int(col[1:3],16), int(col[3:5],16), int(col[5:7],16))
            except Exception: return (255, 255, 255)

        rendered = []
        for ld in line_defs:
            if not ld["text"].strip():
                continue
            fnt  = _load_font(ld["size"], ld["bold"])
            bbox = draw.textbbox((0, 0), ld["text"], font=fnt)
            tw   = bbox[2] - bbox[0]
            th   = bbox[3] - bbox[1]
            rendered.append({**ld, "font": fnt, "tw": tw, "th": th})

        if not rendered:
            return bg

        # ── Calculate total block height ─────────────────────────
        # Include horizontal rule height after first (title) line if enabled
        rule_h = (rule_gap * 2 + rule_thick) if show_rule and len(rendered) > 0 else 0
        total_h = sum(r["th"] for r in rendered) + padding * (len(rendered) - 1)
        # Insert rule space after title line
        if show_rule and len(rendered) > 1:
            total_h += rule_h

        y_start = int(H * v_pos - total_h / 2)
        y_start = max(40, min(y_start, H - total_h - 40))

        # ── Draw each line ────────────────────────────────────────
        y = y_start
        rule_drawn = False
        for idx, r in enumerate(rendered):
            if align == "center": x = (W - r["tw"]) // 2
            elif align == "right": x = W - r["tw"] - margin
            else: x = margin

            # Drop shadow
            if shadow:
                draw.text((x + 4, y + 4), r["text"], font=r["font"], fill=(0, 0, 0))

            draw.text((x, y), r["text"], font=r["font"],
                      fill=_hex_to_rgb(r["color"]))
            y += r["th"]

            # Draw horizontal rule after the title line (index 0)
            if show_rule and r.get("is_title") and not rule_drawn and idx < len(rendered) - 1:
                rule_drawn = True
                y += rule_gap
                rule_px_w = int(W * rule_width_f)
                if align == "center":
                    rx = (W - rule_px_w) // 2
                elif align == "right":
                    rx = W - rule_px_w - margin
                else:
                    rx = margin
                draw.rectangle([rx, y, rx + rule_px_w, y + rule_thick],
                               fill=_hex_to_rgb(rule_color))
                y += rule_thick + rule_gap
            else:
                y += padding

        return bg

    def _on_export_success(self, path):
        if hasattr(self, 'progress_win') and self.progress_win.winfo_exists():
            self.progress_win.destroy()
        self.lbl_status.config(text="Export complete.")
        messagebox.showinfo("Success", f"Video exported!\n{path}")

    def _on_export_failed(self, msg):
        if hasattr(self, 'progress_win') and self.progress_win.winfo_exists():
            self.progress_win.destroy()
        self.lbl_status.config(text="Export failed.")
        messagebox.showerror("Export Failed", f"Rendering error:\n{msg}")



# ═════════════════════════════════════════════════════════════════════
# TITLE SCREEN EDITOR  — standalone Toplevel popup
# ═════════════════════════════════════════════════════════════════════
class TitleScreenEditor:
    """
    Popup editor for configuring a title card that appears before image #1.
    Settings are stored back into app.title_screen (dict).
    A live 16:9 preview canvas updates as the user types.
    """

    PREVIEW_W = 640
    PREVIEW_H = 360

    BG_MODES = ["blur", "darken", "blur+darken", "solid", "original"]
    ALIGNS   = ["center", "left", "right"]

    def __init__(self, parent, app):
        self.app    = app
        self.win    = tk.Toplevel(parent)
        self.win.title("🎬 Title Screen Editor")
        self.win.geometry("1020x680")
        self.win.resizable(True, True)
        self.win.grab_set()

        # Load existing settings or defaults
        ts = app.title_screen or {}
        self._enabled       = tk.BooleanVar(value=ts.get("enabled", True))
        self._title         = tk.StringVar(value=ts.get("title", ""))
        self._subtitle      = tk.StringVar(value=ts.get("subtitle", ""))
        self._date          = tk.StringVar(value=ts.get("date", ""))
        self._title_size    = tk.IntVar(value=ts.get("title_size", 96))
        self._subtitle_size = tk.IntVar(value=ts.get("subtitle_size", 48))
        self._date_size     = tk.IntVar(value=ts.get("date_size", 36))
        self._title_color   = tk.StringVar(value=ts.get("title_color", "#ffffff"))
        self._subtitle_color= tk.StringVar(value=ts.get("subtitle_color", "#cccccc"))
        self._date_color    = tk.StringVar(value=ts.get("date_color", "#aaaaaa"))
        self._font_face     = tk.StringVar(value=ts.get("font_face", "arial"))
        self._align         = tk.StringVar(value=ts.get("text_align", "center"))
        self._v_pos         = tk.DoubleVar(value=ts.get("v_position", 0.5))
        self._bg_mode       = tk.StringVar(value=ts.get("bg_mode", "blur+darken"))
        self._blur_radius   = tk.IntVar(value=ts.get("blur_radius", 20))
        self._darken_factor = tk.DoubleVar(value=ts.get("darken_factor", 0.5))
        self._solid_color   = tk.StringVar(value=ts.get("solid_color", "#1a1a2e"))
        self._shadow        = tk.BooleanVar(value=ts.get("shadow", True))
        self._duration      = tk.DoubleVar(value=ts.get("duration", 4.0))
        self._line_padding  = tk.IntVar(value=ts.get("line_padding", 20))
        # Horizontal rule
        self._show_rule      = tk.BooleanVar(value=ts.get("show_rule", False))
        self._rule_color     = tk.StringVar(value=ts.get("rule_color", "#ffffff"))
        self._rule_width     = tk.DoubleVar(value=ts.get("rule_width", 0.6))
        self._rule_thickness = tk.IntVar(value=ts.get("rule_thickness", 3))
        self._rule_gap       = tk.IntVar(value=ts.get("rule_gap", 18))

        self._preview_img   = None  # keep PhotoImage reference alive

        self._build_ui()
        self._refresh_preview()

        for var in [self._title, self._subtitle, self._date,
                    self._title_color, self._subtitle_color, self._date_color,
                    self._font_face, self._align, self._bg_mode,
                    self._solid_color, self._shadow, self._rule_color, self._show_rule]:
            var.trace_add("write", lambda *_: self._refresh_preview())
        for var in [self._title_size, self._subtitle_size, self._date_size,
                    self._blur_radius, self._line_padding,
                    self._rule_thickness, self._rule_gap]:
            var.trace_add("write", lambda *_: self._refresh_preview())
        self._v_pos.trace_add("write",         lambda *_: self._refresh_preview())
        self._darken_factor.trace_add("write", lambda *_: self._refresh_preview())
        self._rule_width.trace_add("write",    lambda *_: self._refresh_preview())

    # ── UI ────────────────────────────────────────────────────────
    def _build_ui(self):
        main = ttk.Frame(self.win, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Left: controls  |  Right: preview
        left  = ttk.Frame(main, width=340)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        left.pack_propagate(False)

        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── Left controls ─────────────────────────────────────────
        scroll_frame = ttk.Frame(left)
        scroll_frame.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(scroll_frame, highlightthickness=0)
        vsb    = ttk.Scrollbar(scroll_frame, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def sep(): ttk.Separator(inner, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)
        def hdr(t): ttk.Label(inner, text=t, font=("Helvetica", 9, "bold")).pack(anchor=tk.W, pady=(4,2))
        def lrow(label, widget_fn):
            f = ttk.Frame(inner); f.pack(fill=tk.X, pady=2)
            ttk.Label(f, text=label, width=16).pack(side=tk.LEFT)
            widget_fn(f).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Enable toggle
        ttk.Checkbutton(inner, text="Enable title screen",
                        variable=self._enabled).pack(anchor=tk.W, pady=(0, 4))

        # Duration
        lrow("Duration (sec):", lambda f: ttk.Spinbox(
            f, from_=1, to=30, increment=0.5,
            textvariable=self._duration, width=6))

        sep()
        hdr("Text Content")

        def text_block(label, var, size_var, color_var):
            ttk.Label(inner, text=label, foreground="#888888",
                      font=("Helvetica", 8)).pack(anchor=tk.W)
            tf = ttk.Frame(inner); tf.pack(fill=tk.X, pady=(0, 4))
            ttk.Entry(tf, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,4))
            ttk.Spinbox(tf, from_=12, to=200, increment=4,
                        textvariable=size_var, width=5).pack(side=tk.LEFT)
            # Colour swatch button
            swatch = tk.Button(tf, width=2, bg=color_var.get(),
                               command=lambda cv=color_var, btn=None: self._pick_color(cv, swatch_refs[id(var)]))
            swatch.pack(side=tk.LEFT, padx=(4, 0))
            swatch_refs[id(var)] = swatch

        swatch_refs = {}
        text_block("Title",    self._title,    self._title_size,    self._title_color)
        text_block("Subtitle", self._subtitle, self._subtitle_size, self._subtitle_color)
        text_block("Date / caption", self._date, self._date_size,  self._date_color)

        sep()
        hdr("Typography")

        lrow("Font:", lambda f: ttk.Combobox(
            f, textvariable=self._font_face, width=14,
            values=["arial","times","courier","verdana","georgia",
                    "trebuchet","impact","DejaVuSans","Helvetica"]))
        lrow("Alignment:", lambda f: ttk.Combobox(
            f, textvariable=self._align, state="readonly",
            values=self.ALIGNS, width=10))
        lrow("Line padding:", lambda f: ttk.Spinbox(
            f, from_=0, to=80, increment=4,
            textvariable=self._line_padding, width=5))
        lrow("Vertical pos:", lambda f: ttk.Scale(
            f, variable=self._v_pos, from_=0.1, to=0.9,
            orient=tk.HORIZONTAL))
        ttk.Checkbutton(inner, text="Drop shadow",
                        variable=self._shadow).pack(anchor=tk.W, pady=2)

        sep()
        hdr("Horizontal Rule  (under title)")

        ttk.Checkbutton(inner, text="Show rule",
                        variable=self._show_rule).pack(anchor=tk.W, pady=(0, 4))

        # Rule colour swatch
        rf = ttk.Frame(inner); rf.pack(fill=tk.X, pady=2)
        ttk.Label(rf, text="Colour:", width=16).pack(side=tk.LEFT)
        self._rule_swatch = tk.Button(rf, width=3, bg=self._rule_color.get(),
                                      command=lambda: self._pick_color(
                                          self._rule_color, self._rule_swatch,
                                          refresh=True))
        self._rule_swatch.pack(side=tk.LEFT)
        ttk.Label(rf, textvariable=self._rule_color,
                  foreground="#888888", font=("Helvetica", 8)).pack(side=tk.LEFT, padx=4)

        lrow("Width (0–1):", lambda f: ttk.Scale(
            f, variable=self._rule_width, from_=0.1, to=1.0,
            orient=tk.HORIZONTAL))
        lrow("Thickness (px):", lambda f: ttk.Spinbox(
            f, from_=1, to=20, increment=1,
            textvariable=self._rule_thickness, width=5))
        lrow("Gap (px):", lambda f: ttk.Spinbox(
            f, from_=0, to=60, increment=2,
            textvariable=self._rule_gap, width=5))

        sep()
        hdr("Background")

        lrow("Mode:", lambda f: ttk.Combobox(
            f, textvariable=self._bg_mode, state="readonly",
            values=self.BG_MODES, width=14))
        lrow("Blur radius:", lambda f: ttk.Spinbox(
            f, from_=0, to=60, increment=2,
            textvariable=self._blur_radius, width=5))
        lrow("Darken (0-1):", lambda f: ttk.Scale(
            f, variable=self._darken_factor, from_=0.0, to=1.0,
            orient=tk.HORIZONTAL))

        # Solid colour picker
        sf2 = ttk.Frame(inner); sf2.pack(fill=tk.X, pady=2)
        ttk.Label(sf2, text="Solid colour:", width=16).pack(side=tk.LEFT)
        self._solid_swatch = tk.Button(sf2, width=4, bg=self._solid_color.get(),
                                       command=lambda: self._pick_color(
                                           self._solid_color, self._solid_swatch))
        self._solid_swatch.pack(side=tk.LEFT)
        ttk.Label(sf2, textvariable=self._solid_color,
                  foreground="#888888", font=("Helvetica", 8)).pack(side=tk.LEFT, padx=4)

        # ── Right: preview + buttons ──────────────────────────────
        ttk.Label(right, text="Preview  (live — 16:9)",
                  font=("Helvetica", 10, "bold")).pack(anchor=tk.W, pady=(0, 4))

        # Canvas fills available space; we letterbox the render into it
        self.preview_canvas = tk.Canvas(right,
            bg="#1a1a1a", highlightthickness=1,
            highlightbackground="#444444")
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        # Redraw when canvas is resized so the preview always fills correctly
        self.preview_canvas.bind("<Configure>", lambda e: self._refresh_preview())

        btn_row = ttk.Frame(right)
        btn_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_row, text="🔄 Refresh Preview",
                   command=self._refresh_preview).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="✅ Apply",
                   command=self._apply).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btn_row, text="❌ Cancel",
                   command=self.win.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="🗑 Clear Title",
                   command=self._clear).pack(side=tk.RIGHT, padx=(0, 8))

    def _pick_color(self, var, button, refresh=False):
        from tkinter.colorchooser import askcolor
        result = askcolor(color=var.get(), parent=self.win, title="Pick colour")
        if result and result[1]:
            var.set(result[1])
            if button:
                button.config(bg=result[1])
            self._refresh_preview()

    def _refresh_preview(self):
        # Get the actual canvas pixel dimensions (may not be ready on first call)
        cw = self.preview_canvas.winfo_width()
        ch = self.preview_canvas.winfo_height()
        if cw < 10 or ch < 10:
            # Canvas not yet realised — schedule a retry after layout pass
            self.win.after(80, self._refresh_preview)
            return

        # Fit a 16:9 rect inside the canvas, centred with black bars
        target_ratio = 16 / 9
        canvas_ratio = cw / ch
        if canvas_ratio >= target_ratio:
            rh = ch; rw = int(ch * target_ratio)
        else:
            rw = cw; rh = int(cw / target_ratio)

        ox = (cw - rw) // 2
        oy = (ch - rh) // 2

        ts   = self._collect()
        base = self.app.image_data[0]["path"] if self.app.image_data else None
        try:
            pil = self.app._render_title_frame(base, ts, rw, rh)
            self._preview_img = ImageTk.PhotoImage(pil)
            self.preview_canvas.delete("all")
            # Black letterbox fill
            self.preview_canvas.create_rectangle(0, 0, cw, ch, fill="#000000", outline="")
            self.preview_canvas.create_image(ox, oy, anchor=tk.NW,
                                              image=self._preview_img)
            # Thin border around render area
            self.preview_canvas.create_rectangle(ox, oy, ox + rw, oy + rh,
                outline="#333333", fill="")
        except Exception as e:
            self.preview_canvas.delete("all")
            self.preview_canvas.create_text(
                cw // 2, ch // 2,
                text=f"Preview error:\n{e}", fill="#ff6666",
                font=("Helvetica", 9), justify=tk.CENTER)

    def _collect(self):
        return {
            "enabled":        self._enabled.get(),
            "title":          self._title.get(),
            "subtitle":       self._subtitle.get(),
            "date":           self._date.get(),
            "title_size":     self._title_size.get(),
            "subtitle_size":  self._subtitle_size.get(),
            "date_size":      self._date_size.get(),
            "title_color":    self._title_color.get(),
            "subtitle_color": self._subtitle_color.get(),
            "date_color":     self._date_color.get(),
            "font_face":      self._font_face.get(),
            "text_align":     self._align.get(),
            "v_position":     round(self._v_pos.get(), 3),
            "bg_mode":        self._bg_mode.get(),
            "blur_radius":    self._blur_radius.get(),
            "darken_factor":  round(self._darken_factor.get(), 2),
            "solid_color":    self._solid_color.get(),
            "shadow":         self._shadow.get(),
            "duration":       self._duration.get(),
            "line_padding":   self._line_padding.get(),
            # Horizontal rule
            "show_rule":      self._show_rule.get(),
            "rule_color":     self._rule_color.get(),
            "rule_width":     round(self._rule_width.get(), 2),
            "rule_thickness": self._rule_thickness.get(),
            "rule_gap":       self._rule_gap.get(),
        }

    def _apply(self):
        self.app.title_screen = self._collect()
        self.app.save_session_state()
        self.app.update_video_length()
        self.app.redraw_waveform()
        self.win.destroy()

    def _clear(self):
        self.app.title_screen = None
        self.app.save_session_state()
        self.app.update_video_length()
        self.app.redraw_waveform()
        self.win.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app  = ImageToVideoApp(root)
    root.bind("<Configure>", lambda e: app.update_preview() if e.widget == root else None)

    def on_close():
        app.save_session_state()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()