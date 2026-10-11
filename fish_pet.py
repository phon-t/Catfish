"""Floating transparent fish desktop pet (v3).
- Loads giphy.webp, chroma-keys the blue background to transparent.
- Body-locked swim animation (only the tail wiggles), head-first steering.
- Layered light-wave wake + trailing bubbles + soft breathing halo.
- Follow-mouse mode: press Ctrl+Shift+F anywhere to toggle (fish chases
  your cursor; press again to go back to random wandering).
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

from PIL import Image, ImageTk

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

# Global hotkey: Ctrl+Shift+F toggles follow-mouse mode.
HOTKEY_MODS = 0x0002 | 0x0004  # MOD_CONTROL | MOD_SHIFT
HOTKEY_VK = 0x46               # 'F'
WM_HOTKEY = 0x0312

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
            if not user32.RegisterHotKey(None, 1, HOTKEY_MODS, HOTKEY_VK):
                self.q.put("hotkey_busy")
                return
            msg = wintypes.MSG()
            while user32.GetMessageA(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.q.put("toggle_follow")
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

    def centroid(f):
        a = f.crop((0, y0, W, H)).split()[3]
        rw, rh = W, H - y0
        total = sx = sy = 0
        for i, v in enumerate(a.getdata()):
            if v > 128:
                total += 1
                sx += i % rw
                sy += i // rw
        if total == 0:
            return (W / 2, (H + y0) / 2)
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
        m.paste(f.split()[3].point(lambda a: 255 if a > 128 else 0),
                (dx, dy))
        union = m if union is None else ImageChops.lighter(union, m)

    bx0, by0, bx1, by1 = union.getbbox()
    pad = 6
    x0, yt = max(0, bx0 - pad), max(0, by0 - pad)
    x1, y1 = min(W, bx1 + pad), min(H, by1 + pad)
    return [f.crop((x0, yt, x1, y1)) for f in shifted]


def load_frames():
    im = Image.open(SRC)
    n = getattr(im, "n_frames", 1)
    raw = []
    for i in range(n):
        im.seek(i)
        raw.append(key_out_blue(im.copy()))
    frames = register_frames(raw)
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
        self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.root.configure(bg=TRANSPARENT_KEY)

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
        self.canvas = tk.Canvas(self.root, bg=TRANSPARENT_KEY,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.sw, self.sh = sw, sh
        self.px = random.uniform(sw * 0.3, sw * 0.7)
        self.py = random.uniform(sh * 0.3, sh * 0.7)
        self.vx, self.vy = 1.5, 0.5
        self.heading = math.atan2(self.vy, self.vx)
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

        # global hotkey -> queue -> polled on the Tk thread
        self.msg_q = queue.Queue()
        HotkeyThread(self.msg_q).start()
        self.hotkey_ok = True

        self.bind_events()
        self.build_menu()
        self.update_window_size()
        self.root.geometry(f"+{int(self.px)}+{int(self.py)}")
        self.poll_hotkey()
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
        m = Menu(self.root, tearoff=0)
        m.add_command(label="Follow mouse  (Ctrl+Shift+F)",
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
        m.add_command(label="Quit (Q)", command=self.quit_app)
        self.menu = m

    def refresh_follow_label(self):
        mark = "\u2713 " if self.follow_mode else ""
        try:
            self.menu.entryconfig(self.follow_menu_idx,
                                  label=f"{mark}Follow mouse  (Ctrl+Shift+F)")
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
                elif msg == "hotkey_busy":
                    self.hotkey_ok = False
                    print("Note: Ctrl+Shift+F is already used by another app; "
                          "use right-click menu or the F key instead.")
                elif isinstance(msg, tuple) and msg[0] == "hotkey_error":
                    self.hotkey_ok = False
                    print("Global hotkey unavailable:", msg[1])
        except queue.Empty:
            pass
        self.root.after(120, self.poll_hotkey)

    def quit_app(self):
        try:
            ctypes.windll.user32.UnregisterHotKey(None, 1)
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
        self.vx, self.vy = 0.0, 0.0
        self.root.geometry(f"+{int(self.px)}+{int(self.py)}")

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

    def pick_target(self):
        margin = 90
        w = self.win_size()
        cx, cy = self.px + w / 2, self.py + w / 2
        for _ in range(12):
            tx = random.uniform(margin, max(margin + 1, self.sw - margin))
            ty = random.uniform(margin, max(margin + 1, self.sh - margin))
            if math.hypot(tx - cx, ty - cy) >= 200:
                return (tx, ty)
        return (self.sw / 2, self.sh / 2)

    @staticmethod
    def _ang_diff(a, b):
        return (a - b + math.pi) % (2 * math.pi) - math.pi

    def mouse_pos(self):
        return (self.root.winfo_pointerx(), self.root.winfo_pointery())

    def step_movement(self):
        if self.paused or self.dragging:
            return 0.0
        w = self.win_size()
        cx, cy = self.px + w / 2, self.py + w / 2

        if self.follow_mode:
            tx, ty = self.mouse_pos()
            dx, dy = tx - cx, ty - cy
            dist = math.hypot(dx, dy)
            if dist < 1:
                return 0.0
            # chase the cursor, ease in close so it hovers nearby
            speed_want = self.max_speed * 1.15 * min(1.0, dist / 220.0)
            if dist < 42:
                # gentle orbit so it doesn't sit dead still under the cursor
                ox, oy = -dy / dist, dx / dist
                dx, dy = dx * 0.3 + ox * dist * 0.7, dy * 0.3 + oy * dist * 0.7
                dist = math.hypot(dx, dy) or 1
                speed_want = self.max_speed * 0.35
            dvx, dvy = dx / dist * speed_want, dy / dist * speed_want
        else:
            if self.rest_ticks > 0:
                self.rest_ticks -= 1
                self.vx *= 0.94
                self.vy *= 0.94
                self.px += self.vx
                self.py += self.vy
                if self.rest_ticks == 0:
                    self.target = self.pick_target()
                self.root.geometry(f"+{int(self.px)}+{int(self.py)}")
                return math.hypot(self.vx, self.vy)
            tx, ty = self.target
            dx, dy = tx - cx, ty - cy
            dist = math.hypot(dx, dy)
            if dist < 30:
                self.rest_ticks = random.randint(40, 130)
                self.target = self.pick_target()
                return math.hypot(self.vx, self.vy)
            speed_want = self.max_speed * min(1.0, dist / 160.0)
            speed_want = max(speed_want, 1.1)
            dvx, dvy = dx / dist * speed_want, dy / dist * speed_want

        margin = 130
        if cx < margin:
            dvx += (margin - cx) / margin * self.max_speed * 2
        if cx > self.sw - margin:
            dvx -= (cx - (self.sw - margin)) / margin * self.max_speed * 2
        if cy < margin:
            dvy += (margin - cy) / margin * self.max_speed * 2
        if cy > self.sh - margin:
            dvy -= (cy - (self.sh - margin)) / margin * self.max_speed * 2

        max_force = 0.22
        sx, sy = dvx - self.vx, dvy - self.vy
        sm = math.hypot(sx, sy)
        if sm > max_force:
            sx, sy = sx / sm * max_force, sy / sm * max_force
        self.vx += sx
        self.vy += sy

        if math.hypot(self.vx, self.vy) > 0.3:
            want = math.atan2(self.vy, self.vx)
            turn = max(-0.09, min(0.09, self._ang_diff(want, self.heading)))
            self.heading += turn

        self.px += self.vx
        self.py += self.vy
        self.root.geometry(f"+{int(self.px)}+{int(self.py)}")
        return math.hypot(self.vx, self.vy)

    # ---------------- rendering ----------------
    @lru_cache(maxsize=128)
    def render_sprite(self, dir_idx, frame_idx, size):
        frame = self.frames[frame_idx].resize((size, size), Image.LANCZOS)
        side = int(self.rot_unit * size / self.sprite_unit)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(frame, ((side - size) // 2, (side - size) // 2), frame)
        heading = dir_idx * 2 * math.pi / DIRS
        canvas = canvas.rotate(90.0 - math.degrees(heading),
                               resample=Image.BICUBIC)
        return ImageTk.PhotoImage(canvas)

    def dir_index(self):
        return int(round(self.heading / (2 * math.pi) * DIRS)) % DIRS

    def speed_now(self):
        return math.hypot(self.vx, self.vy)

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
        """One faint double-ring ripple around the tail. Kept subtle."""
        _, (tx, ty) = self.head_tail_pos()
        ch, sh = abs(math.cos(self.heading)), abs(math.sin(self.heading))
        ex, ey = 0.7 + 0.6 * ch, 0.7 + 0.6 * sh  # stretch along swim axis
        rings = []
        for _ in range(2):
            rid = self.canvas.create_oval(tx, ty, tx + 1, ty + 1,
                                          outline=WAVE_COLS[2], width=1)
            self.canvas.tag_lower(rid)
            rings.append(rid)
        if halo:
            max_r, life = self.size * 0.60, 110
        else:
            max_r = self.size * (0.36 + random.random() * 0.14)
            life = 40
        self.waves.append({"cx": tx, "cy": ty, "age": 0, "life": life,
                           "max_r": max_r, "ex": ex, "ey": ey,
                           "rings": rings, "halo": halo,
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
                    self.canvas.itemconfig(rid, outline=ci, width=1)
                except Exception:
                    pass

    def spawn_bubble(self):
        (hx, hy), _ = self.head_tail_pos()
        hx += random.uniform(-8, 8)
        hy += random.uniform(-8, 8)
        r = random.uniform(1.5, 3.5)
        bid = self.canvas.create_oval(hx - r, hy - r, hx + r, hy + r,
                                      outline=BUBBLE_COL, width=1)
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
        # occasional faint ripple while cruising (rare at rest)
        self.ripple_timer += 1
        interval = 30 if spd < 1.5 else (22 if spd < 2.5 else 16)
        if self.ripple_timer >= interval:
            self.ripple_timer = 0
            if spd >= 1.0 or random.random() < 0.12:
                self.spawn_wave()
        # odd bubble only when swimming hard
        self.bubble_timer += 1
        if self.bubble_timer >= 12:
            self.bubble_timer = 0
            if spd > 2.2 and random.random() < 0.35:
                self.spawn_bubble()
        # barely-there halo every ~5s at rest
        self.halo_timer += 1
        if self.halo_timer >= 320:
            self.halo_timer = 0
            self.spawn_wave(halo=True)
        self.step_waves()
        self.step_bubbles()

    # ---------------- floating status text ----------------
    def flash_text(self, text):
        try:
            if self.flash:
                self.canvas.delete(self.flash["id"])
        except Exception:
            pass
        w = self.win_size()
        fid = self.canvas.create_text(w / 2, w / 2 + self.size * 0.62,
                                      text=text, fill="#eafcff",
                                      font=("Segoe UI", 10, "bold"))
        self.flash = {"id": fid, "age": 0}

    def step_flash(self):
        if not self.flash:
            return
        self.flash["age"] += 1
        t = self.flash["age"] / 90.0
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
        self.canvas.config(width=w, height=w)
        photo = self.render_sprite(self.dir_index(), self.frame_idx, self.size)
        if self.img_id is None:
            self.img_id = self.canvas.create_image(w / 2, w / 2, image=photo)
        else:
            self.canvas.coords(self.img_id, w / 2, w / 2)
            self.canvas.itemconfig(self.img_id, image=photo)
            self.canvas.tag_raise(self.img_id)
        self._photo = photo
        if self.flash:
            try:
                self.canvas.tag_raise(self.flash["id"])
            except Exception:
                pass

    def tick(self):
        spd = self.step_movement()
        self.step_animation()
        self.step_fx(spd if isinstance(spd, float) else self.speed_now())
        self.step_flash()
        self.draw()
        self.root.after(16, self.tick)

    def run(self):
        print("Fish pet running: Ctrl+Shift+F = follow mouse | drag = move | "
              "wheel = size | right-click = menu | space = pause | q = quit")
        self.root.mainloop()


if __name__ == "__main__":
    FishPet().run()
