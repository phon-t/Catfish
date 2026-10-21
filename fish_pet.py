"""Floating transparent fish desktop pet.
- Loads giphy.webp, chroma-keys the blue background to transparent.
- Body-locked swim animation (only the tail wiggles).
- Heading-first swimming: the fish only moves where its head points,
  turning in smooth arcs. Works across multiple monitors.
- Faint dashed shimmer rings trail the tail while cruising.
- Follow-mouse mode: Ctrl+Shift+F anywhere toggles it.
- Quit from anywhere: Ctrl+Shift+Q (or right-click menu).
- Scroll wheel / +/- adjusts size. Drag to move. Right-click for menu.
Run: python fish_pet.py  (or double-click run_fish.bat)
"""
import ctypes
import math
import os
import queue
import random
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from functools import lru_cache
from tkinter import Menu

from PIL import Image, ImageDraw, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """Locate bundled data files (works both from source and exe builds)."""
    base = getattr(sys, "_MEIPASS", BASE_DIR)
    return os.path.join(base, name)


SRC = resource_path("giphy.webp")

BG_COLOR = (92, 155, 177)  # solid blue background in the source frames
INNER_TOL = 35
OUTER_TOL = 90

TRANSPARENT_KEY = "black"
DIRS = 16  # heading quantization steps for the render cache
APP_VERSION = "1.2"
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
FONT = ("Segoe UI", 10, "bold") if IS_WINDOWS else ("Helvetica", 10, "bold")

# Global hotkeys: Ctrl+Shift+F toggles follow, Ctrl+Shift+Q quits.
HOTKEY_MODS = 0x0002 | 0x0004  # MOD_CONTROL | MOD_SHIFT
HOTKEY_VK = 0x46               # 'F'
HOTKEY_QUIT_VK = 0x51          # 'Q'
WM_HOTKEY = 0x0312


def get_virtual_screen(root):
    """Full desktop rect (x, y, w, h) spanning ALL monitors."""
    if IS_WINDOWS:
        try:
            u = ctypes.windll.user32
            return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                    u.GetSystemMetrics(78), u.GetSystemMetrics(79))
        except Exception:
            pass
    try:
        x, y = root.winfo_vrootx(), root.winfo_vrooty()
        w, h = root.winfo_vrootwidth(), root.winfo_vrootheight()
        if w > 1 and h > 1:
            return (x, y, w, h)
    except Exception:
        pass
    return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())


class XShapeMasker:
    """Per-pixel window transparency on Linux via the X11 SHAPE extension."""

    def __init__(self, root):
        self.ok = False
        try:
            self.x11 = ctypes.CDLL("libX11.so.6")
            self.xext = ctypes.CDLL("libXext.so.6")
            self.x11.XOpenDisplay.restype = ctypes.c_void_p
            self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self.dpy = self.x11.XOpenDisplay(None)
            if not self.dpy:
                return
            self.win = ctypes.c_ulong(root.winfo_id())
            self.x11.XCreateBitmapFromData.restype = ctypes.c_ulong
            self.x11.XCreateBitmapFromData.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p,
                ctypes.c_uint, ctypes.c_uint]
            self.x11.XFreePixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            self.x11.XFlush.argtypes = [ctypes.c_void_p]
            self.xext.XShapeCombineMask.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_ulong, ctypes.c_int]
            # X bitmaps are LSB-first; PIL packs MSB-first
            self.reverse = bytes(
                int(f"{i:08b}"[::-1], 2) for i in range(256))
            self.ok = True
        except Exception:
            self.ok = False

    def apply(self, mask):
        if not self.ok:
            return
        try:
            w, h = mask.size
            data = mask.tobytes().translate(self.reverse)
            pm = self.x11.XCreateBitmapFromData(self.dpy, self.win, data, w, h)
            if not pm:
                return
            self.xext.XShapeCombineMask(self.dpy, self.win, 0, 0, 0, pm, 0)
            self.x11.XFreePixmap(self.dpy, pm)
            self.x11.XFlush(self.dpy)
        except Exception:
            self.ok = False

# Light-wave palette: bright core -> dim fading edge.
WAVE_COLS = ["#f2feff", "#d3f8ff", "#a9eefa", "#7fdcf0",
             "#54bcd6", "#3593ab", "#22697d"]
BUBBLE_COL = "#e6fcff"


# --------------------------------------------------------------------------
# Global hotkey listener (stdlib only, runs in a daemon thread).
# RegisterHotKey with hWnd=NULL delivers WM_HOTKEY to this thread's queue.
# --------------------------------------------------------------------------
class HotkeyThread(threading.Thread):
    def __init__(self, msg_queue):
        super().__init__(daemon=True)
        self.q = msg_queue

    def run(self):
        try:
            user32 = ctypes.windll.user32
            ok_f = user32.RegisterHotKey(None, 1, HOTKEY_MODS, HOTKEY_VK)
            ok_q = user32.RegisterHotKey(None, 2, HOTKEY_MODS, HOTKEY_QUIT_VK)
            if not ok_f or not ok_q:
                self.q.put("hotkey_busy")
                if not ok_f:
                    user32.UnregisterHotKey(None, 2)
                if not ok_q:
                    user32.UnregisterHotKey(None, 1)
                if not ok_f and not ok_q:
                    return
            msg = wintypes.MSG()
            while user32.GetMessageA(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.q.put("toggle_follow" if msg.wParam == 1
                               else "quit_app")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageA(ctypes.byref(msg))
        except Exception as e:  # non-Windows / restricted env
            self.q.put(("hotkey_error", str(e)))


# --------------------------------------------------------------------------
# Frame processing
# --------------------------------------------------------------------------
def key_out_blue(frame):
    """Turn the blue background transparent with a feathered edge."""
    from PIL import ImageChops
    frame = frame.convert("RGBA")
    bg = Image.new("RGB", frame.size, BG_COLOR)
    # per-channel difference -> brightness = colour distance proxy
    dist = ImageChops.difference(frame.convert("RGB"), bg).convert("L")
    lut = ([0] * 16
           + [int((i - 16) / (48 - 16) * 255) for i in range(16, 48)]
           + [255] * (256 - 48))
    frame.putalpha(dist.point(lut))
    return frame


def register_frames(frames):
    """Shift frames so the HEAD stays locked (only the tail wiggles)."""
    from PIL import ImageChops
    W, H = frames[0].size
    y0 = int(H * 0.55)  # head region: bottom 45%
    bin_lut = [0] * 128 + [1] * 128

    def centroid(f):
        # column/row projections are computed in C - much faster than
        # walking every pixel in Python
        m = f.crop((0, y0, W, H)).split()[3].point(bin_lut)
        cols, rows = m.getprojection()
        total = sum(cols)
        if total == 0:
            return (W / 2, (H + y0) / 2)
        sx = sum(i * c for i, c in enumerate(cols))
        sy = sum(i * r for i, r in enumerate(rows))
        return (sx / total, sy / total + y0)

    ref_cx, ref_cy = centroid(frames[0])
    shifted, union = [], None
    for f in frames:
        cx, cy = centroid(f)
        dx, dy = int(round(ref_cx - cx)), int(round(ref_cy - cy))
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        canvas.paste(f, (dx, dy), f)
        shifted.append(canvas)
        m = Image.new("L", (W, H), 0)
        m.paste(f.split()[3].point([0] * 128 + [255] * 128), (dx, dy))
        union = m if union is None else ImageChops.lighter(union, m)

    bx0, by0, bx1, by1 = union.getbbox()
    pad = 6
    x0, yt = max(0, bx0 - pad), max(0, by0 - pad)
    x1, y1 = min(W, bx1 + pad), min(H, by1 + pad)
    return [f.crop((x0, yt, x1, y1)) for f in shifted]


CACHE_VERSION = "3"


def frame_cache_path():
    """Location for the processed-frame cache (per-user, writable)."""
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("TEMP") or BASE_DIR)
    d = os.path.join(base, "CatfishPet")
    try:
        st = os.stat(SRC)
        key = f"{CACHE_VERSION}_{st.st_size}_{int(st.st_mtime)}"
    except Exception:
        key = CACHE_VERSION
    return d, os.path.join(d, f"frames_{key}.webp")


def load_frames():
    cache_dir, cache_file = frame_cache_path()

    # fast path: reuse processed frames from a previous run
    try:
        if os.path.exists(cache_file):
            im = Image.open(cache_file)
            frames = []
            for i in range(getattr(im, "n_frames", 1)):
                im.seek(i)
                frames.append(im.convert("RGBA"))
            if frames:
                return frames
    except Exception:
        pass

    im = Image.open(SRC)
    n = getattr(im, "n_frames", 1)
    raw = []
    for i in range(n):
        im.seek(i)
        raw.append(key_out_blue(im.copy()))
    frames = register_frames(raw)

    try:
        os.makedirs(cache_dir, exist_ok=True)
        frames[0].save(cache_file, save_all=True, append_images=frames[1:],
                       lossless=True, quality=100, method=0)
    except Exception:
        pass

    if getattr(sys, "frozen", False):
        return frames  # running as exe: bundled art only, nothing to export
    try:
        gif_out = os.path.join(BASE_DIR, "fish_transparent.gif")
        webp_out = os.path.join(BASE_DIR, "fish_transparent.webp")
        if not os.path.exists(gif_out):
            frames[0].save(gif_out, save_all=True, append_images=frames[1:],
                           duration=im.info.get("duration", 120), loop=0,
                           transparency=0, disposal=2)
        if not os.path.exists(webp_out):
            frames[0].save(webp_out, save_all=True, append_images=frames[1:],
                           duration=im.info.get("duration", 120), loop=0)
    except Exception as e:
        print("Could not export transparent copies:", e)
    return frames


# --------------------------------------------------------------------------
# Pet
# --------------------------------------------------------------------------
class FishPet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Fish Pet")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        # per-pixel transparency differs per platform
        if IS_WINDOWS:
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
            self.root.configure(bg=TRANSPARENT_KEY)
            bg = TRANSPARENT_KEY
            self.shaper = None
        elif IS_MAC:
            bg = "systemTransparent"
            try:
                self.root.configure(bg=bg)
                self.root.wm_attributes("-transparent", True)
            except Exception:
                bg = TRANSPARENT_KEY
            self.shaper = None
        else:
            bg = TRANSPARENT_KEY  # hidden by the window shape mask
            self.shaper = None

        self.frames = load_frames()
        self.n_frames = len(self.frames)
        self.frame_idx = 0
        self.anim_acc = 0.0

        fw, fh = self.frames[0].size
        self.sprite_unit = max(fw, fh)
        self.rot_unit = int(math.hypot(fw, fh)) + 8

        self.size = 180
        self.max_speed = 2.8
        self.show_ripples = True
        self.paused = False
        self.follow_mode = False

        self.ripple_pad = 56
        self.canvas = tk.Canvas(self.root, bg=bg,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.vx0, self.vy0, self.vw, self.vh = get_virtual_screen(self.root)
        if self.vw < sw or self.vh < sh:
            self.vx0, self.vy0, self.vw, self.vh = 0, 0, sw, sh
        self.sw, self.sh = sw, sh
        # start near the middle of the whole desktop (all monitors)
        self.px = self.vx0 + random.uniform(self.vw * 0.35, self.vw * 0.65)
        self.py = self.vy0 + random.uniform(self.vh * 0.35, self.vh * 0.65)
        self.heading = random.uniform(-math.pi, math.pi)
        self.spd = 1.5  # scalar forward speed; fish only moves along heading
        self.target = self.pick_target()
        self.rest_ticks = 0
        self.dragging = False
        self.resume_job = None

        # wave / bubble / halo state
        self.waves = []    # {cx,cy,age,life,max_r,ex,ey,rings:[ids],halo}
        self.bubbles = []  # {x,y,vx,vy,life,age,r,id}
        self.flash = None  # {"id", "age"}
        self.ripple_timer = 0
        self.bubble_timer = 0
        self.halo_timer = 0
        self.img_id = None
        self._photo = None
        self._canvas_wh = None
        self._img_wh = None
        self._last_geom = None
        self._shape_key = None

        # global hotkey -> queue -> polled on the Tk thread (Windows only)
        self.msg_q = queue.Queue()
        self.hotkey_ok = IS_WINDOWS
        if IS_WINDOWS:
            HotkeyThread(self.msg_q).start()

        self.bind_events()
        self.build_menu()
        self.update_window_size()
        self._move_window()
        self.root.update_idletasks()
        if not (IS_WINDOWS or IS_MAC):
            self.shaper = XShapeMasker(self.root)
            # shaped Linux window shows only the fish silhouette, so the
            # wake effects would be clipped away
            self.show_ripples = False
        self.poll_hotkey()
        self.flash_text("Right-click me for options - drag me around", life=260)
        self.tick()

    # ---------------- setup ----------------
    def bind_events(self):
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_drag_end)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<Button-2>", self.on_right_click)
        self.root.bind("<MouseWheel>", self.on_wheel)
        self.root.bind("<Button-4>", lambda e: self.change_size(10))
        self.root.bind("<Button-5>", lambda e: self.change_size(-10))
        self.root.bind("<plus>", lambda e: self.change_size(16))
        self.root.bind("<minus>", lambda e: self.change_size(-16))
        self.root.bind("<equal>", lambda e: self.change_size(16))
        self.root.bind("<space>", lambda e: self.toggle_pause())
        self.root.bind("q", lambda e: self.quit_app())
        # local fallback (works when the pet window has focus)
        self.root.bind("f", lambda e: self.toggle_follow())
        self.root.bind("F", lambda e: self.toggle_follow())

    def build_menu(self):
        hot = "  (Ctrl+Shift+F)" if IS_WINDOWS else ""
        m = Menu(self.root, tearoff=0)
        m.add_command(label=f"Follow mouse{hot}",
                      command=self.toggle_follow)
        self.follow_menu_idx = 0
        m.add_separator()
        m.add_command(label="Bigger (+)", command=lambda: self.change_size(24))
        m.add_command(label="Smaller (-)", command=lambda: self.change_size(-24))
        sm = Menu(m, tearoff=0)
        for label, val in [("Tiny 80", 80), ("Small 120", 120),
                           ("Medium 180", 180), ("Large 250", 250),
                           ("Huge 340", 340)]:
            sm.add_command(label=label, command=lambda v=val: self.set_size(v))
        m.add_cascade(label="Size", menu=sm)
        sp = Menu(m, tearoff=0)
        for label, val in [("Slow", 1.6), ("Normal", 2.8), ("Fast", 4.6)]:
            sp.add_command(label=label, command=lambda v=val: self.set_speed(v))
        m.add_cascade(label="Swim speed", menu=sp)
        m.add_command(label="Toggle light waves", command=self.toggle_ripples)
        m.add_command(label="Pause / Resume (Space)", command=self.toggle_pause)
        quit_label = "Quit (Ctrl+Shift+Q)" if IS_WINDOWS else "Quit"
        m.add_command(label=quit_label, command=self.quit_app)
        m.add_separator()
        m.add_command(label=f"Catfish Pet v{APP_VERSION}", state="disabled")
        self.menu = m

    def refresh_follow_label(self):
        mark = "\u2713 " if self.follow_mode else ""
        hot = "  (Ctrl+Shift+F)" if IS_WINDOWS else ""
        try:
            self.menu.entryconfig(self.follow_menu_idx,
                                  label=f"{mark}Follow mouse{hot}")
        except Exception:
            pass

    # ---------------- controls ----------------
    def on_right_click(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def on_wheel(self, e):
        self.change_size(12 if e.delta > 0 else -12)

    def set_size(self, v):
        self.size = max(64, min(400, int(v)))
        self.render_sprite.cache_clear()
        self.update_window_size()

    def change_size(self, d):
        self.set_size(self.size + d)

    def set_speed(self, v):
        self.max_speed = float(v)

    def toggle_ripples(self):
        self.show_ripples = not self.show_ripples
        if not self.show_ripples:
            for wv in self.waves:
                for rid in wv["rings"]:
                    try:
                        self.canvas.delete(rid)
                    except Exception:
                        pass
            for b in self.bubbles:
                try:
                    self.canvas.delete(b["id"])
                except Exception:
                    pass
            self.waves.clear()
            self.bubbles.clear()

    def toggle_pause(self):
        self.paused = not self.paused

    def toggle_follow(self):
        self.follow_mode = not self.follow_mode
        self.rest_ticks = 0
        if not self.follow_mode:
            self.target = self.pick_target()
        self.refresh_follow_label()
        self.flash_text("Following your mouse!"
                        if self.follow_mode else "Back to wandering")

    def poll_hotkey(self):
        try:
            while True:
                msg = self.msg_q.get_nowait()
                if msg == "toggle_follow":
                    self.toggle_follow()
                elif msg == "quit_app":
                    self.quit_app()
                elif msg == "hotkey_busy":
                    self.hotkey_ok = False
                    print("Note: a global shortcut is already used by another "
                          "app; use the right-click menu instead.")
                elif isinstance(msg, tuple) and msg[0] == "hotkey_error":
                    self.hotkey_ok = False
                    print("Global hotkey unavailable:", msg[1])
        except queue.Empty:
            pass
        self.root.after(120, self.poll_hotkey)

    def quit_app(self):
        try:
            ctypes.windll.user32.UnregisterHotKey(None, 1)
            ctypes.windll.user32.UnregisterHotKey(None, 2)
        except Exception:
            pass
        self.root.destroy()

    def on_drag_start(self, e):
        self.dragging = True
        self._dx, self._dy = e.x, e.y
        if self.resume_job:
            self.root.after_cancel(self.resume_job)
            self.resume_job = None

    def on_drag_move(self, e):
        self.px = self.root.winfo_x() + e.x - self._dx
        self.py = self.root.winfo_y() + e.y - self._dy
        self.spd = 0.0
        self._move_window()

    def on_drag_end(self, e):
        self.dragging = False

        def resume():
            if not self.follow_mode:
                self.target = self.pick_target()
            self.rest_ticks = 0

        self.resume_job = self.root.after(1200, resume)

    # ---------------- movement ----------------
    def win_size(self):
        scale = self.size / self.sprite_unit
        return int(self.rot_unit * scale) + self.ripple_pad * 2

    def update_window_size(self):
        w = self.win_size()
        self.root.geometry(f"{w}x{w}")

    def _move_window(self):
        """Only touch the window manager when the integer position changes."""
        pos = (int(self.px), int(self.py))
        if pos != self._last_geom:
            self._last_geom = pos
            self.root.geometry(f"+{pos[0]}+{pos[1]}")

    def pick_target(self):
        # anywhere on the whole desktop, across all monitors
        m = 120
        x0, y0 = self.vx0 + m, self.vy0 + m
        x1, y1 = self.vx0 + self.vw - m, self.vy0 + self.vh - m
        if x1 <= x0 or y1 <= y0:
            return (self.vx0 + self.vw / 2, self.vy0 + self.vh / 2)
        w = self.win_size()
        cx, cy = self.px + w / 2, self.py + w / 2
        for _ in range(12):
            tx = random.uniform(x0, x1)
            ty = random.uniform(y0, y1)
            if math.hypot(tx - cx, ty - cy) >= 220:
                return (tx, ty)
        return (self.vx0 + self.vw / 2, self.vy0 + self.vh / 2)

    @staticmethod
    def _ang_diff(a, b):
        return (a - b + math.pi) % (2 * math.pi) - math.pi

    def mouse_pos(self):
        return (self.root.winfo_pointerx(), self.root.winfo_pointery())

    # ---------------- movement ----------------
    # Heading-first model: the fish only ever advances along its heading,
    # turning at a limited rate like a real fish. Sideways sliding is
    # impossible by construction - the head always leads.
    TURN_RATE = 0.11
    ACCEL = 0.15

    def step_movement(self):
        if self.paused or self.dragging:
            return 0.0
        w = self.win_size()
        cx, cy = self.px + w / 2, self.py + w / 2

        if self.follow_mode:
            tx, ty = self.mouse_pos()
            dx, dy = tx - cx, ty - cy
            dist = math.hypot(dx, dy)
            if dist < 14:
                # parked on the cursor: hold still, keep facing it
                want_head = self.heading
                want_spd = 0.0
            else:
                # always face the cursor, glide slower when close
                want_head = math.atan2(dy, dx)
                want_spd = self.max_speed * 1.15 * min(1.0, dist / 220.0)
                if dist < 60:
                    want_spd = max(0.5, want_spd * (dist / 60.0))
        else:
            if self.rest_ticks > 0:
                self.rest_ticks -= 1
                self.spd = max(0.0, self.spd - 0.18)
                self.px += math.cos(self.heading) * self.spd
                self.py += math.sin(self.heading) * self.spd
                if self.rest_ticks == 0:
                    self.target = self.pick_target()
                self._clamp_to_desktop(w)
                self._move_window()
                return self.spd
            tx, ty = self.target
            dx, dy = tx - cx, ty - cy
            dist = math.hypot(dx, dy)
            if dist < 34:
                self.rest_ticks = random.randint(40, 130)
                self.target = self.pick_target()
                return self.spd
            want_head = math.atan2(dy, dx)
            want_spd = self.max_speed * min(1.0, dist / 170.0)
            want_spd = max(want_spd, 1.0)

        # bend the wanted heading away from desktop edges (all monitors)
        # before turning, so the body arcs instead of sliding
        m = 150
        push_x = push_y = 0.0
        if cx < self.vx0 + m:
            push_x += (self.vx0 + m - cx) / m
        if cx > self.vx0 + self.vw - m:
            push_x -= (cx - (self.vx0 + self.vw - m)) / m
        if cy < self.vy0 + m:
            push_y += (self.vy0 + m - cy) / m
        if cy > self.vy0 + self.vh - m:
            push_y -= (cy - (self.vy0 + self.vh - m)) / m
        if push_x or push_y:
            bx = math.cos(want_head) + push_x * 2.2
            by = math.sin(want_head) + push_y * 2.2
            want_head = math.atan2(by, bx)

        # limited turn rate = smooth arcs; ease off in hard turns
        diff = self._ang_diff(want_head, self.heading)
        self.heading += max(-self.TURN_RATE, min(self.TURN_RATE, diff))
        if abs(diff) > 0.7:
            want_spd = min(want_spd, self.max_speed * 0.45)
        if self.spd < want_spd:
            self.spd = min(want_spd, self.spd + self.ACCEL)
        else:
            self.spd = max(want_spd, self.spd - self.ACCEL)

        self.px += math.cos(self.heading) * self.spd
        self.py += math.sin(self.heading) * self.spd
        self._clamp_to_desktop(w)
        self._move_window()
        return self.spd

    def _clamp_to_desktop(self, w):
        # keep the fish centre on the desktop: free to visit every monitor,
        # never lost off-screen
        c = 40
        self.px = min(max(self.px, self.vx0 + c - w / 2),
                      self.vx0 + self.vw - c - w / 2)
        self.py = min(max(self.py, self.vy0 + c - w / 2),
                      self.vy0 + self.vh - c - w / 2)

    # ---------------- rendering ----------------
    @lru_cache(maxsize=128)
    def sprite_image(self, dir_idx, frame_idx, size):
        """Rotated, aspect-correct frame (PIL) for a heading/frame/size."""
        frame = self.frames[frame_idx]
        fw, fh = frame.size
        scale = size / self.sprite_unit
        nw, nh = max(1, int(round(fw * scale))), max(1, int(round(fh * scale)))
        frame = frame.resize((nw, nh), Image.LANCZOS)
        side = max(1, int(round(self.rot_unit * scale)))
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(frame, ((side - nw) // 2, (side - nh) // 2), frame)
        heading = dir_idx * 2 * math.pi / DIRS
        return canvas.rotate(90.0 - math.degrees(heading),
                             resample=Image.BICUBIC)

    @lru_cache(maxsize=128)
    def render_sprite(self, dir_idx, frame_idx, size):
        return ImageTk.PhotoImage(self.sprite_image(dir_idx, frame_idx,
                                                    size))

    def update_shape(self, w):
        """Apply the fish silhouette as the Linux window mask."""
        if not self.shaper or not self.shaper.ok:
            return
        try:
            sprite = self.sprite_image(self.dir_index(), self.frame_idx,
                                       self.size)
            alpha = sprite.split()[3].point([0] * 150 + [255] * 106)
            side = sprite.size[0]
            mask = Image.new("L", (w, w), 0)
            mask.paste(alpha, ((w - side) // 2, (w - side) // 2))
            if self.flash:
                # keep the hint text readable: a small pill behind it
                tw = min(w - 8, len(self.flash["text"]) * 6 + 18)
                cy = w / 2 + self.size * 0.62
                x0, y0 = (w - tw) / 2, cy - 11
                ImageDraw.Draw(mask).rounded_rectangle(
                    (x0, y0, x0 + tw, y0 + 22), radius=9, fill=255)
            self.shaper.apply(mask.convert("1", dither=Image.NONE))
        except Exception:
            self.shaper.ok = False

    def dir_index(self):
        return int(round(self.heading / (2 * math.pi) * DIRS)) % DIRS

    def speed_now(self):
        return self.spd

    def step_animation(self):
        self.anim_acc += max(0.25, self.speed_now() / self.max_speed)
        if self.anim_acc >= 1.6:
            self.anim_acc = 0.0
            self.frame_idx = (self.frame_idx + 1) % self.n_frames

    # ---------------- light waves / bubbles / halo ----------------
    def head_tail_pos(self):
        w = self.win_size()
        cx = cy = w / 2
        dx, dy = math.cos(self.heading), math.sin(self.heading)
        jx, jy = random.uniform(-4, 4), random.uniform(-4, 4)
        tail = (cx - dx * self.size * 0.40 + jx,
                cy - dy * self.size * 0.40 + jy)
        head = (cx + dx * self.size * 0.36,
                cy + dy * self.size * 0.36)
        return head, tail

    def spawn_wave(self, halo=False):
        """One thin dashed shimmer ring drifting off the tail."""
        _, (tx, ty) = self.head_tail_pos()
        ch, sh = abs(math.cos(self.heading)), abs(math.sin(self.heading))
        ex, ey = 0.7 + 0.6 * ch, 0.7 + 0.6 * sh  # stretch along swim axis
        rid = self.canvas.create_oval(tx, ty, tx + 1, ty + 1,
                                      outline=WAVE_COLS[2], width=1,
                                      dash=(6, 5))
        self.canvas.tag_lower(rid)
        if halo:
            max_r, life = self.size * 0.50, 120
        else:
            max_r = self.size * (0.30 + random.random() * 0.10)
            life = 44
        self.waves.append({"cx": tx, "cy": ty, "age": 0, "life": life,
                           "max_r": max_r, "ex": ex, "ey": ey,
                           "rings": [rid], "halo": halo,
                           "col0": 3 if halo else 2})
        while len(self.waves) > 3:
            old = self.waves.pop(0)
            for rid in old["rings"]:
                try:
                    self.canvas.delete(rid)
                except Exception:
                    pass

    def step_waves(self):
        for wv in list(self.waves):
            wv["age"] += 1
            t = wv["age"] / wv["life"]
            if t >= 1.0:
                for rid in wv["rings"]:
                    try:
                        self.canvas.delete(rid)
                    except Exception:
                        pass
                self.waves.remove(wv)
                continue
            # ease-out expansion: fast burst, gentle settle
            r = wv["max_r"] * (1 - (1 - t) ** 2)
            cols = WAVE_COLS[wv["col0"]:]
            for i, rid in enumerate(wv["rings"]):
                rr = r - i * 6
                if rr <= 1:
                    try:
                        self.canvas.coords(rid, -10, -10, -9, -9)
                    except Exception:
                        pass
                    continue
                # outer ring starts one fade-step behind -> soft depth
                ti = min(1.0, t + i * 0.22)
                ci = cols[min(int(ti * len(cols)), len(cols) - 1)]
                try:
                    self.canvas.coords(rid,
                                       wv["cx"] - rr * wv["ex"],
                                       wv["cy"] - rr * wv["ey"],
                                       wv["cx"] + rr * wv["ex"],
                                       wv["cy"] + rr * wv["ey"])
                    if ci != wv.get("ci"):
                        self.canvas.itemconfig(rid, outline=ci, width=1)
                        wv["ci"] = ci
                except Exception:
                    pass

    def spawn_bubble(self):
        (hx, hy), _ = self.head_tail_pos()
        hx += random.uniform(-8, 8)
        hy += random.uniform(-8, 8)
        r = random.uniform(1.5, 3.5)
        bid = self.canvas.create_oval(hx - r, hy - r, hx + r, hy + r,
                                      outline=BUBBLE_COL, width=1)
        self.canvas.tag_lower(bid)
        self.bubbles.append({"x": hx, "y": hy,
                             "vx": random.uniform(-0.4, 0.4),
                             "vy": random.uniform(-1.4, -0.8),
                             "life": random.randint(45, 75), "age": 0,
                             "r": r, "id": bid, "wob": random.uniform(0, 6)})
        if len(self.bubbles) > 14:
            old = self.bubbles.pop(0)
            try:
                self.canvas.delete(old["id"])
            except Exception:
                pass

    def step_bubbles(self):
        for b in list(self.bubbles):
            b["age"] += 1
            if b["age"] >= b["life"]:
                try:
                    self.canvas.delete(b["id"])
                except Exception:
                    pass
                self.bubbles.remove(b)
                continue
            b["wob"] += 0.15
            b["x"] += b["vx"] + math.sin(b["wob"]) * 0.3
            b["y"] += b["vy"]
            shrink = 1 - 0.4 * b["age"] / b["life"]
            r = max(0.8, b["r"] * shrink)
            try:
                self.canvas.coords(b["id"], b["x"] - r, b["y"] - r,
                                   b["x"] + r, b["y"] + r)
            except Exception:
                pass

    def step_fx(self, spd):
        if not self.show_ripples or self.paused:
            return
        # occasional faint shimmer while cruising (rare at rest)
        self.ripple_timer += 1
        interval = 40 if spd < 1.5 else (30 if spd < 2.5 else 22)
        if self.ripple_timer >= interval:
            self.ripple_timer = 0
            if spd >= 1.4 or random.random() < 0.06:
                self.spawn_wave()
        # odd bubble only when swimming hard
        self.bubble_timer += 1
        if self.bubble_timer >= 12:
            self.bubble_timer = 0
            if spd > 2.2 and random.random() < 0.35:
                self.spawn_bubble()
        # barely-there halo every ~7s at rest
        self.halo_timer += 1
        if self.halo_timer >= 420:
            self.halo_timer = 0
            self.spawn_wave(halo=True)
        self.step_waves()
        self.step_bubbles()

    # ---------------- floating status text ----------------
    def flash_text(self, text, life=90):
        try:
            if self.flash:
                self.canvas.delete(self.flash["id"])
        except Exception:
            pass
        w = self.win_size()
        fid = self.canvas.create_text(w / 2, w / 2 + self.size * 0.62,
                                      text=text, fill="#eafcff",
                                      font=FONT)
        self.flash = {"id": fid, "age": 0, "life": life, "text": text}

    def step_flash(self):
        if not self.flash:
            return
        self.flash["age"] += 1
        t = self.flash["age"] / float(self.flash.get("life", 90))
        if t >= 1.0:
            try:
                self.canvas.delete(self.flash["id"])
            except Exception:
                pass
            self.flash = None
            return
        if t > 0.6:  # fade through the palette
            cols = ["#eafcff", "#a9eefa", "#54bcd6", "#22697d"]
            try:
                self.canvas.itemconfig(
                    self.flash["id"],
                    fill=cols[min(int((t - 0.6) / 0.4 * len(cols)),
                                  len(cols) - 1)])
            except Exception:
                pass

    # ---------------- main loop ----------------
    def draw(self):
        w = self.win_size()
        if w != self._canvas_wh:
            # resizing the canvas is expensive - only on size changes
            self.canvas.config(width=w, height=w)
            self._canvas_wh = w
        photo = self.render_sprite(self.dir_index(), self.frame_idx, self.size)
        if self.img_id is None:
            self.img_id = self.canvas.create_image(w / 2, w / 2, image=photo)
            self._img_wh = w
        elif photo is not self._photo or w != self._img_wh:
            # sprite swap + recenter only when they actually differ
            self.canvas.coords(self.img_id, w / 2, w / 2)
            self.canvas.itemconfig(self.img_id, image=photo)
            self._img_wh = w
        self._photo = photo
        key = (self.dir_index(), self.frame_idx, self.size, w,
               self.flash["id"] if self.flash else None)
        if key != self._shape_key:
            self._shape_key = key
            self.update_shape(w)

    def tick(self):
        spd = self.step_movement()
        self.step_animation()
        self.step_fx(spd)
        self.step_flash()
        self.draw()
        # adaptive frame rate: full speed while swimming, relaxed at rest
        if self.paused:
            delay = 60
        elif spd > 0.25 or self.dragging:
            delay = 16
        else:
            delay = 33
        self.root.after(delay, self.tick)

    def run(self):
        print("Fish pet running: Ctrl+Shift+F = follow mouse | "
              "Ctrl+Shift+Q = quit | drag = move | wheel = size | "
              "right-click = menu | space = pause")
        self.root.mainloop()


if __name__ == "__main__":
    FishPet().run()
