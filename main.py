# main.py

# --- Standard library imports ---
import os                    # Read environment variables (TASKSWITCHER_TZ override)
import json                  # Read/parse the schedule.json file
import threading             # Run background tasks (tray + file watcher) without blocking the UI
import time                  # Sleep in the file watcher loop
from pathlib import Path     # Build paths relative to this file
from datetime import datetime, timedelta  # Handle dates/times for scheduling
from zoneinfo import ZoneInfo             # IANA timezone support (Python 3.9+)

# --- Local timezone detection / override ---
from tzlocal import get_localzone_name    # Detect the machine's IANA timezone name

# --- Third-party / GUI imports ---
import tkinter as tk                     # Tkinter GUI for the floating color window
from PIL import Image, ImageDraw         # Build an in-memory tray icon image
import pystray                           # System tray icon + menu
##from plyer import notification           # Cross-platform notifications (Windows toast on Win10+) (using a different method for notifications)
from apscheduler.schedulers.background import BackgroundScheduler  # Scheduler for timed jobs

# ---------- Config ----------
# Single source of truth for the app’s timezone (override with env var if needed).
TZ_NAME = os.environ.get("TASKSWITCHER_TZ", get_localzone_name())
APP_TZ = ZoneInfo(TZ_NAME)

# Paths
SCHEDULE_PATH = Path(__file__).with_name("schedule.json")
CONFIG_PATH   = Path(__file__).with_name("config.json")

# Defaults
DEFAULT_COLOR = "#3b82f6"  # Tailwind blue-500
WINDOW_W = 200
WINDOW_H = 120
CAT_COLORS = {
    "focus":  "#3b82f6",  # blue
    "admin":  "#22c55e",  # green
    "break":  "#06b6d4",  # cyan
    "collab": "#f59e0b",  # amber
    "off":    "#64748b",  # slate
}

# Try to load config.json and override defaults
try:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Replace category colors if present
    if "category_colors" in cfg and isinstance(cfg["category_colors"], dict):
        CAT_COLORS.update(cfg["category_colors"])

    # Window size
    if cfg.get("compact_window"):
        WINDOW_W, WINDOW_H = 180, 80

    # (Optional) you can grab notify_seconds_before, show_notifications, etc.
    # and store them in variables here too
except FileNotFoundError:
    # no config.json, just stick with defaults
    cfg = {}
except Exception as e:
    print(f"[config] failed to load config.json: {e}")
    cfg = {}

# --- Normalize a few cfg values with safe fallbacks ---
_cfg_window = cfg.get("window") or {}
CFG_WIN_X = int(_cfg_window.get("x", 100))
CFG_WIN_Y = int(_cfg_window.get("y", 100))
CFG_COMPACT = bool(cfg.get("compact_window", False))
CFG_SHOW_NOTIFS = bool(cfg.get("show_notifications", True))

# Accept ints or numeric strings for lead time; fallback to 0 on bad input
try:
    CFG_NOTIFY_LEAD = int(cfg.get("notify_seconds_before", 0))
    if CFG_NOTIFY_LEAD < 0:
        CFG_NOTIFY_LEAD = 0
except Exception:
    CFG_NOTIFY_LEAD = 0

class StickyToast:
    """
    Tiny always-on-top window anchored bottom-right.
    Stays until a button is pressed.
    """
    def __init__(self, root: tk.Tk, title: str, body: str, on_dismiss=None, on_snooze=None):
        self.root = root
        self.on_dismiss = on_dismiss
        self.on_snooze = on_snooze

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)        # no title bar
        self.win.attributes("-topmost", True)  # stay on top
        self.win.configure(bg="#0b1220")       # dark backdrop

        # Content frame
        frame = tk.Frame(self.win, bg="#0b1220")
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Title + body
        tk.Label(frame, text=title, fg="white", bg="#0b1220",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(frame, text=body, fg="#e5e7eb", bg="#0b1220",
                 font=("Segoe UI", 9), wraplength=260, justify="left").pack(anchor="w", pady=(4, 8))

        # Buttons
        btn_row = tk.Frame(frame, bg="#0b1220")
        btn_row.pack(fill="x", pady=(2, 0))

        snooze = tk.Button(btn_row, text="Snooze 5 min", relief="flat",
                           command=self._snooze, bg="#1f2937", fg="white", activebackground="#334155")
        snooze.pack(side="left", padx=(0, 6))

        dism = tk.Button(btn_row, text="Dismiss", relief="flat",
                         command=self._dismiss, bg="#1f2937", fg="white", activebackground="#334155")
        dism.pack(side="left")

        # Size and position (bottom-right of current screen)
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - w - 20
        y = sh - h - 50
        self.win.geometry(f"{w}x{h}+{x}+{y}")

        # Basic shadow-ish border
        self.win.configure(highlightthickness=1, highlightbackground="#334155")

        # Close with ESC or click-out (optional)
        self.win.bind("<Escape>", lambda e: self._dismiss())

    def _dismiss(self):
        try:
            if callable(self.on_dismiss):
                self.on_dismiss()
        finally:
            self.win.destroy()

    def _snooze(self):
        try:
            if callable(self.on_snooze):
                self.on_snooze()
        finally:
            self.win.destroy()

# ---------- App ----------
class CollapsibleSection(tk.Frame):
    """A simple accordion-like section with a clickable header."""
    def __init__(self, master, title: str, *, opened=False):
        super().__init__(master)
        self._opened = opened
        self.header = tk.Frame(self)
        self.header.pack(fill="x")

        self.btn = tk.Button(
            self.header, text=("▼ " if opened else "► ") + title,
            bd=0, anchor="w", command=self.toggle
        )
        self.btn.pack(side="left", fill="x", expand=True)

        self.body = tk.Frame(self)
        if opened:
            self.body.pack(fill="x")

    def toggle(self):
        self._opened = not self._opened
        self.btn.config(text=("▼ " if self._opened else "► ") + self.btn.cget("text")[2:])
        if self._opened:
            self.body.pack(fill="x")
        else:
            self.body.forget()

class App:
    def __init__(self):
        """
        Initialize core state:
        - timezone, schedule blocks, UI color, paused flag
        - system tray icon holder, drag offsets, reload lock
        - file mtime for hot-reload, and the background scheduler
        - Tkinter window + canvas bindings
        """
        # Unified app timezone (DST-safe, consistent everywhere)
        self.tz = APP_TZ

        # In-memory state for today's blocks and current UI color
        self.blocks = []
        self.active_color = DEFAULT_COLOR

        # Control flags and helpers
        self.paused = False
        self.icon = None
        self._drag_offset = (0, 0)
        self.reload_lock = threading.Lock()
        self.last_schedule_mtime = 0

        # Background job scheduler (runs alongside Tk mainloop).
        # IMPORTANT: pass the timezone NAME to avoid dateutil tzlocal bug paths.
        self.scheduler = BackgroundScheduler(timezone=TZ_NAME)
        self.scheduler.start()

        # Lightweight config object for window position + compact toggle
        self.config = type(
            "Config", (),
            {
                "win_x": CFG_WIN_X,
                "win_y": CFG_WIN_Y,
                "compact_window": CFG_COMPACT,
                "show_notifications": CFG_SHOW_NOTIFS,
                "notify_seconds_before": CFG_NOTIFY_LEAD,
            },
        )()

        # ----- Tkinter window setup -----
        self.tk_root = tk.Tk()
        self.tk_root.title("Task Switcher")

        # If compact at start, size accordingly (width x height)
        if self.config.compact_window:
            self.tk_root.geometry("180x80")

        # Canvas is the colored rectangle "status light"
        self.canvas = tk.Canvas(self.tk_root, width=WINDOW_W, height=WINDOW_H, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)  # <— key

        # Bind canvas resize → redraw
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.tk_root.bind("<Double-1>", self.toggle_compact)
        self.tk_root.bind("<B1-Motion>", self.drag)
        self.tk_root.bind("<Button-1>", self.start_drag)
        self.tk_root.bind("<Control-w>", self._open_schedule_panel)

        # Defer initial window placement slightly so Tk knows screen metrics
        self.tk_root.after(100, lambda: self.tk_root.geometry(f"+{self.config.win_x}+{self.config.win_y}"))

        # Start a UI "tick" loop that keeps the color in sync with the active block
        self.tk_root.after(500, self._tick_ui)

    # ---------- UI ----------
    def draw_window(self):
        """
        Redraw a compact 7-day week view inside the tile.
        - A thin top bar uses self.active_color (so your current-block color still shows).
        - 7 columns (Mon..Sun); today is outlined.
        - Each day's blocks render as vertical bars by time range.
        - In compact mode, omit labels; in full size, show tiny start labels.
        """
        self.canvas.delete("all")

        # Determine the current window size (fallback to defaults when first shown)
        try:
            w = max(120, self.tk_root.winfo_width())
            h = max(70,  self.tk_root.winfo_height())
        except Exception:
            w, h = WINDOW_W, WINDOW_H
            legend_h = 18 if not self.config.compact_window else 16

        # Background
        self.canvas.create_rectangle(0, 0, w, h, fill="#e5e7eb", outline="")  # slate-200

        # Top accent bar in the active color (preserves your current color logic)
        self.canvas.create_rectangle(0, 0, w, 6, fill=self.active_color, outline="")

        # Layout metrics
        top_pad   = 10  # room under the accent bar
        bottom_pad= 6
        left_pad  = 6
        right_pad = 6
        col_gap   = 2
        header_h  = 12  # day label row
        grid_top  = top_pad + header_h
        grid_bot  = h - bottom_pad
        grid_h    = max(1, grid_bot - grid_top)

        # Seven equal columns
        cols = 7
        total_gap = col_gap * (cols - 1)
        col_w = max(8, (w - left_pad - right_pad - total_gap) // cols)

        # Monday-start week like your panel
        today = datetime.now(self.tz).date()
        start_of_week = today - timedelta(days=today.weekday())  # Monday
        days = [start_of_week + timedelta(days=i) for i in range(cols)]
        today_idx = (today - start_of_week).days

        # Get blocks for the week (uses your existing logic)
        week_blocks = self._blocks_for_week(days)

        # Helpers
        def minutes_since_midnight(t):
            return t.hour * 60 + t.minute + t.second / 60.0

        def y_for_time(t):
            # Map 00:00..24:00 to grid_top..grid_bot
            m = minutes_since_midnight(t)
            return grid_top + (m / (24 * 60)) * grid_h

        # Draw columns
        for i, d in enumerate(days):
            x0 = left_pad + i * (col_w + col_gap)
            x1 = x0 + col_w

            # Column background
            self.canvas.create_rectangle(x0, grid_top, x1, grid_bot,
                                        fill="#f8fafc", outline="#e2e8f0")  # very light bg

            # Day header (Mon, Tue, ...)
            day_lbl = d.strftime("%a")  # Mon/Tue/...
            self.canvas.create_text((x0 + x1) // 2, top_pad + header_h // 2,
                                text=day_lbl, font=("Segoe UI", 8, "bold"),
                                anchor="c", fill="#334155")

            # Render blocks as vertical bars
            for blk in week_blocks[i]:
                y0 = y_for_time(blk["start_time"])
                y1 = y_for_time(blk["end_time"])
                # Minimum height for visibility
                if y1 - y0 < 2:
                    y1 = y0 + 2

                self.canvas.create_rectangle(x0 + 2, y0, x1 - 2, y1,
                                            fill=blk["color"], outline="")

                # Only show tiny start labels when not compact (more room)
                if not self.config.compact_window and (y1 - y0) >= 14:
                    # Format start time as 12-hour AM/PM
                    lbl = blk["start_time"].strftime("%I:%M %p").lstrip("0")
                    self.canvas.create_text(x0 + 4, y0 + 2,
                                        text=lbl,
                                        font=("Segoe UI", 7),
                                        anchor="nw", fill="#0f172a")

            # Outline today's column
            if i == today_idx:
                self.canvas.create_rectangle(x0, grid_top, x1, grid_bot,
                                            outline="#0ea5e9", width=2)  # cyan-500
    
    def toggle_compact(self, _evt=None):
        """
        Toggle between compact and full window sizes.
        Note: only dimensions change; the rectangle fills the canvas.
        """
        self.config.compact_window = not self.config.compact_window
        w = 180 if self.config.compact_window else WINDOW_W
        h = 80 if self.config.compact_window else WINDOW_H
        self.tk_root.geometry(f"{w}x{h}")

    def start_drag(self, event):
        """Remember the initial mouse position when left-button is pressed for dragging."""
        self._drag_offset = (event.x_root, event.y_root)

    def drag(self, event):
        """Move the window following the mouse while left-button is held."""
        dx = event.x_root - self._drag_offset[0]
        dy = event.y_root - self._drag_offset[1]
        x = self.tk_root.winfo_x() + dx
        y = self.tk_root.winfo_y() + dy
        self.tk_root.geometry(f"+{x}+{y}")
        self._drag_offset = (event.x_root, event.y_root)

    @staticmethod
    def _time_in_range(start_t, end_t, now_t):
        """True if now_t ∈ [start_t, end_t) with wrap-around support."""
        if start_t <= end_t:
            return start_t <= now_t < end_t
        # wraps midnight (e.g., 22:00 → 01:00)
        return now_t >= start_t or now_t < end_t

    def _tick_ui(self):
        """
        Every second:
        - Determine if the current time sits inside any block's [start, end).
        - Update the active_color accordingly.
        - Redraw the window.
        """
        now = datetime.now(self.tz).time()
        for b in self.blocks:
            if self._time_in_range(b["start_time"], b["end_time"], now):
                self.active_color = b.get("color") or DEFAULT_COLOR
                break
        else:
            # No active block -> default color
            self.active_color = DEFAULT_COLOR

        self.draw_window()
        # Schedule the next tick in ~1 second
        self.tk_root.after(1_000, self._tick_ui)

    def _on_canvas_resize(self, event):
        """Redraw the window when the canvas changes size (e.g. window resize)."""
        # debounce rapid resize events
        if hasattr(self, "_resize_job") and self._resize_job:
            self.tk_root.after_cancel(self._resize_job)
        self._resize_job = self.tk_root.after(50, self.draw_window)

    def _open_schedule_panel(self, _=None):
        """Open a toplevel window showing a collapsible 7-day view."""
        # If already open, bring to front
        if hasattr(self, "_sched_win") and self._sched_win and tk.Toplevel.winfo_exists(self._sched_win):
            self._sched_win.deiconify()
            self._sched_win.lift()
            return

        self._sched_win = tk.Toplevel(self.tk_root)
        self._sched_win.title("Task Switcher – Week View")
        self._sched_win.geometry("380x520")

        # Scrollable canvas
        container = tk.Frame(self._sched_win)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, highlightthickness=0)
        vsb = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas)
        canvas.create_window((0, 0), window=body, anchor="nw")

        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._sched_win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        # Build 7 sections (Mon..Sun), expand today's by default
        today = datetime.now(self.tz).date()
        start_of_week = today - timedelta(days=today.weekday())  # Monday
        days = [start_of_week + timedelta(days=i) for i in range(7)]
        today_idx = (today - start_of_week).days

        # Materialize per-day blocks (re-using your JSON + fallbacks)
        week_blocks = self._blocks_for_week(days)

        for i, d in enumerate(days):
            title = d.strftime("%A  %b %d")
            section = CollapsibleSection(body, title, opened=(i == today_idx))
            section.pack(fill="x", padx=8, pady=(8 if i == 0 else 4))

            # Render rows for that day
            for blk in week_blocks[i]:
                self._render_block_row(section.body, blk)

        # Footer hint
        hint = tk.Label(body, text="Tip: Right-click tray → Reload schedule to rescan JSON.", fg="#64748b")
        hint.pack(padx=8, pady=12, anchor="w")

    def _render_block_row(self, parent, blk):
        """Row with time range, color swatch, and title."""
        row = tk.Frame(parent)
        row.pack(fill="x", padx=6, pady=3)

        # Color swatch
        sw = tk.Canvas(row, width=14, height=14, highlightthickness=0)
        sw.create_rectangle(0, 0, 14, 14, outline="", fill=blk["color"])
        sw.pack(side="left", padx=(2, 8))

        # Time label
        t_lbl = tk.Label(row, width=13, anchor="w", text=f"{blk['start']}–{blk['end']}")
        t_lbl.pack(side="left")

        # Title
        ttl = tk.Label(row, anchor="w", text=blk["title"])
        ttl.pack(side="left", fill="x", expand=True)

        # Emphasize current block (today only)
        now = datetime.now(self.tz)
        if blk.get("_is_today") and self._time_in_range(blk["start_time"], blk["end_time"], now.time()):
            ttl.config(font=("Segoe UI", 10, "bold"))

    def _blocks_for_week(self, days):
        """
        Produce 7 lists of blocks (one per day).
        Uses the currently active profile semantics:
         - If your JSON only has 'weekday': apply it to Mon–Fri; weekend uses 'off' filler.
         - If your JSON also includes 'weekend' or explicit day names ('monday', 'tuesday', ...),
           those take precedence.
        """
        # Re-parse raw entries once
        try:
            data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {"active_schedule": "weekday", "schedules": {"weekday": []}}

        schedules = data.get("schedules", {})
        # Normalize keys to lowercase for convenience
        norm_keys = {k.lower(): k for k in schedules.keys()}

        def entries_for_date(d):
            wkday = d.strftime("%A").lower()   # 'monday'..'sunday'
            # precedence: explicit day -> weekend/weekday -> active
            if wkday in norm_keys:
                return schedules[norm_keys[wkday]]
            if d.weekday() >= 5 and "weekend" in norm_keys:
                return schedules[norm_keys["weekend"]]
            if d.weekday() < 5 and "weekday" in norm_keys:
                return schedules[norm_keys["weekday"]]
            # fallback to active profile list (what you already use)
            active = data.get("active_schedule")
            if isinstance(active, str) and active in schedules:
                return schedules[active]
            return []

        # Use your parse logic to build concrete blocks per day
        week_lists = []
        for d in days:
            raw_entries = entries_for_date(d)
            # — same steps you do in parse_blocks() —
            # sort by time
            items = []
            for e in raw_entries:
                t = e.get("time")
                if not (isinstance(t, str) and ":" in t):
                    continue
                items.append({
                    "time": t,
                    "label": e.get("label", "Untitled"),
                    "category": e.get("category"),
                    "color": e.get("color")
                })

            def _hm_key(s): 
                hh, mm = [int(x) for x in s.split(":")]
                return hh * 60 + mm

            items.sort(key=lambda x: _hm_key(x["time"]))
            # expand into ranges
            blocks = []
            base = datetime(d.year, d.month, d.day, tzinfo=self.tz)
            for i, cur in enumerate(items):
                nxt = items[i + 1]["time"] if i + 1 < len(items) else "23:59"
                sh, sm = [int(x) for x in cur["time"].split(":")]
                eh, em = [int(x) for x in nxt.split(":")]
                start_t = base.replace(hour=sh, minute=sm, second=0, microsecond=0).time()
                end_t   = base.replace(hour=eh, minute=em, second=0, microsecond=0).time()
                c = cur.get("color")
                if not c and cur.get("category"):
                    c = CAT_COLORS.get(cur["category"])
                c = c or DEFAULT_COLOR
                blocks.append({
                    "title": cur["label"],
                    "start": cur["time"],
                    "end": nxt,
                    "start_time": start_t,
                    "end_time": end_t,
                    "color": c,
                    "_is_today": (d == datetime.now(self.tz).date()),
                })
            week_lists.append(blocks)
        return week_lists
    
    # --- Legend helpers ---
    def _legend_pairs(self):
        """
        Map display labels → colors for the legend.
        Starts with CAT_COLORS and keeps the same label text you use in JSON.
        """
        # base: category color map
        pairs = [(name.capitalize(), col) for name, col in CAT_COLORS.items()]

        # If any current blocks use a color with no category, add a generic tag once.
        known = {c.lower() for c in CAT_COLORS.values()}
        extra_seen = set()
        for b in self.blocks:
            col = (b.get("color") or "").lower()
            if col and col not in known and col not in extra_seen and not b.get("category"):
                pairs.append(("Custom", b["color"]))
                extra_seen.add(col)

        return pairs

    def _draw_legend(self, y_top: int, w: int, left_pad: int, right_pad: int, row_h: int = 16, sw: int = 12):
        """
        Draw a single-row legend of evenly spaced items at vertical position y_top.
        Each item: [■] Label
        """
        items = self._legend_pairs()
        if not items:
            return

        n = len(items)
        seg_w = max(60, (w - left_pad - right_pad) // n)

        for i, (label, color) in enumerate(items):
            x_left = left_pad + i * seg_w
            # swatch
            self.canvas.create_rectangle(x_left, y_top + (row_h - sw)//2,
                                         x_left + sw, y_top + (row_h - sw)//2 + sw,
                                         outline="#cbd5e1", fill=color)
            # label
            self.canvas.create_text(x_left + sw + 6, y_top + row_h // 2,
                                    text=label, anchor="w",
                                    font=("Segoe UI", 8), fill="#334155")

    # ---------- Tray ----------
    def make_tray_icon(self):
        """
        Build a simple circular blue tray icon and menu.
        Run the tray icon on a daemon thread so it doesn't block Tk mainloop.
        """
        # Create a 64x64 transparent image and draw a blue circle on it
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(59, 130, 246, 255))

        # Tray menu: Pause/Resume, manual reload, and Quit
        menu = pystray.Menu(
            pystray.MenuItem("Open Schedule", self._open_schedule_panel),
            pystray.MenuItem("Pause/Resume", self.toggle_pause, default=False),
            pystray.MenuItem("Reload schedule", self.force_reload),
            pystray.MenuItem("Quit", self.quit)
        )

        # Create and launch the system tray icon in a background thread
        self.icon = pystray.Icon("TaskSwitcher", img, "Task Switcher", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def toggle_pause(self, _=None):
        """
        Toggle paused state.
        - When resuming: (re)schedule notifications.
        - When pausing: clear all scheduled jobs so nothing fires.
        """
        self.paused = not self.paused
        if not self.paused:
            self.schedule_notifications()
        else:
            self.scheduler.remove_all_jobs()

    # ---------- Schedule loading & parsing ----------
    def parse_blocks(self):
        """
        Reads schedule.json in the 'profile' format:
          {
            "active_schedule": "weekday",
            "schedules": {
              "weekday": [
                {"time":"08:30","label":"...","category":"..."},
                ...
              ]
            }
          }

        Expands it into a list of concrete blocks:
          {
            "title": str,
            "start": "HH:MM",
            "end":   "HH:MM",
            "start_time": time(),
            "end_time":   time(),
            "color": "#RRGGBB" or None
          }
        """
        try:
            raw = SCHEDULE_PATH.read_text(encoding="utf-8")
            root = json.loads(raw)

            # Validate top-level structure
            if not isinstance(root, dict):
                raise ValueError("Top-level JSON must be an object.")

            active_name = root.get("active_schedule")
            schedules = root.get("schedules")
            if not active_name or not isinstance(schedules, dict):
                raise ValueError("Expected keys: 'active_schedule' (str) and 'schedules' (object).")

            entries = schedules.get(active_name)
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"Schedule '{active_name}' must be a non-empty list.")

            # Normalize & sort by time
            norm = []
            for idx, e in enumerate(entries):
                if not isinstance(e, dict):
                    raise ValueError(f"Entry #{idx+1} is not an object.")
                t = e.get("time")
                if not t or not isinstance(t, str) or ":" not in t:
                    raise ValueError(f"Entry #{idx+1} missing/invalid 'time' (HH:MM).")
                label = e.get("label", "Untitled")
                category = e.get("category")
                color = e.get("color")  # optional per-entry color override
                norm.append({"time": t, "label": label, "category": category, "color": color})

            # Sort by HH:MM
            def _hm_key(s):
                hh, mm = [int(x) for x in s.split(":")]
                return hh * 60 + mm
            norm.sort(key=lambda x: _hm_key(x["time"]))

            # Expand to blocks: each start = this entry.time, end = next entry.time (last to 23:59)
            base_now = datetime.now(self.tz)
            blocks = []
            for i, cur in enumerate(norm):
                nxt = norm[i + 1] if i + 1 < len(norm) else None
                start = cur["time"]
                end = nxt["time"] if nxt else "23:59"

                # Parse to time() objects for fast comparisons
                sh, sm = [int(x) for x in start.split(":")]
                eh, em = [int(x) for x in end.split(":")]
                start_t = base_now.replace(hour=sh, minute=sm, second=0, microsecond=0).time()
                end_t   = base_now.replace(hour=eh, minute=em, second=0, microsecond=0).time()

                # Choose color: explicit > category map > default
                c = cur.get("color")
                if not c and cur.get("category"):
                    c = CAT_COLORS.get(cur["category"])
                c = c or DEFAULT_COLOR

                blocks.append({
                    "title": cur["label"],
                    "start": start,
                    "end": end,
                    "start_time": start_t,
                    "end_time": end_t,
                    "color": c
                })

            # Keep chronological (already sorted, but be explicit)
            blocks.sort(key=lambda b: b["start_time"])

            # --- Debug prints ---
            print(f"[schedule] Loaded {len(blocks)} blocks for '{active_name}'")
            for b in blocks:
                print(f"  {b['start']}–{b['end']} → {b['title']} ({b['color']})")

            return blocks

        except FileNotFoundError:
            print(f"[warn] No schedule file found at {SCHEDULE_PATH}. Using empty schedule.")
            return []
        except Exception as e:
            print(f"[error] Failed to parse schedule: {e}")
            return []



    # ---------- Notifications ----------
    def schedule_notifications(self):
        """
        Create one-shot jobs for each block's *next* start time.
        - If today's start is in the future, schedule it today.
        - If it already passed, schedule it for tomorrow.
        Also, schedule a self-refresh at 00:05 to seed the next day's jobs.
        """
        # --- Master switch: turn all notifications on/off from config ---
        if not getattr(self.config, "show_notifications", True):
            # If notifications are disabled, remove any existing jobs and exit
            self.scheduler.remove_all_jobs()
            return

        # --- Reset jobs to avoid duplicates ---
        # Always clear out old jobs before re-adding them fresh
        self.scheduler.remove_all_jobs()

        # --- Establish reference times ---
        now = datetime.now(self.tz)           # current time (timezone-aware)
        today = now.date()                    # today's calendar date
        tomorrow = today + timedelta(days=1)  # tomorrow's calendar date
        lead = max(0, int(getattr(self.config, "notify_seconds_before", 0)))  
        # lead time = seconds before start; enforce non-negative

        # --- Helper to create a datetime from a date + "HH:MM" string ---
        def _mk_dt(date_, hhmm: str):
            hh, mm = [int(x) for x in hhmm.split(":")]
            return datetime(date_.year, date_.month, date_.day, hh, mm, tzinfo=self.tz)

        # --- Schedule one job per block ---
        for b in self.blocks:
            # Scheduled start time if it were today
            dt_today = _mk_dt(today, b["start"])
            # If that start time has already passed, schedule it tomorrow
            dt_next  = dt_today if dt_today >= now else _mk_dt(tomorrow, b["start"])

            # Apply lead time (notify earlier if set)
            run_at   = dt_next - timedelta(seconds=lead)

            # Never schedule in the past — clamp to "now" if needed
            if run_at < now:
                run_at = now

            # Add the job to APScheduler
            self.scheduler.add_job(
                self._notify_block,                    # function to call
                "date",                                # run once at a specific time
                run_date=run_at,                       # when to trigger
                args=[b],                              # pass the block as an argument
                id=f"block:{b['title']}:{run_at.isoformat()}",  # unique ID per job
                misfire_grace_time=60                  # allow 60s late execution
            )

        # --- Daily re-seed ---
        # Schedule this same function to run again just after midnight
        # Ensures the next day's jobs get queued automatically
        midnight_plus = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=self.tz) + timedelta(minutes=5)
        self.scheduler.add_job(self.schedule_notifications, "date", run_date=midnight_plus)

    def _notify_block(self, block):
        """
        Show a sticky in-app toast at the start of a block.
        Runs from APScheduler thread; marshal UI to Tk thread.
        """
        if self.paused:
            return

        # Build strings here; UI work happens in _show_toast_ui
        title = "Task Switcher"
        body = f"Time to switch to: {block.get('title', 'Task')}"

        # Marshal to Tk thread
        self.tk_root.after(0, self._show_toast_ui, title, body, block)

    def _show_toast_ui(self, title: str, body: str, block: dict):
        """
        Create the sticky toast window. Buttons:
            - Dismiss: just closes
            - Snooze 5 min: schedules the same notification 5 minutes later
        """
        def on_dismiss():
            # no-op, but you could log or mark 'acknowledged' here
            pass

        def on_snooze():
            try:
                # Re-schedule a one-shot job 5 minutes later with same block
                run_at = datetime.now(self.tz) + timedelta(minutes=5)
                self.scheduler.add_job(
                    self._notify_block,
                    "date",
                    run_date=run_at,
                    args=[block],
                    id=f"snooze:{block.get('title','Task')}:{run_at.isoformat()}",
                    misfire_grace_time=60
                )
            except Exception as e:
                print(f"[warn] Snooze scheduling failed: {e}")

    StickyToast(self.tk_root, title, body, on_dismiss=on_dismiss, on_snooze=on_snooze)

    # ---------- Control ----------
    def force_reload(self, _=None):
        """
        Manually reload the schedule (tray menu item) and rebuild jobs.
        Protected by a lock so the file watcher doesn't collide.
        """
        with self.reload_lock:
            self.blocks = self.parse_blocks()
            if not self.paused:
                self.schedule_notifications()

    def quit(self, _=None):
        """
        Cleanly stop tray + scheduler and close the Tk window.
        Called from the tray menu.
        """
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        self.tk_root.destroy()

    # ---------- File watching ----------
    def watch_files(self):
        """
        Poll the schedule file every 2 seconds.
        If mtime changes (file edited), force a reload and reschedule jobs.
        """
        while True:
            try:
                mtime = SCHEDULE_PATH.stat().st_mtime
                if mtime != self.last_schedule_mtime:
                    self.last_schedule_mtime = mtime
                    self.force_reload()
            except FileNotFoundError:
                # If the file doesn't exist yet, that's fine; we'll catch it later
                pass
            except Exception as e:
                print(f"[watch] error: {e}")
            time.sleep(2)

    # ---------- Boot ----------
    def run(self):
        """
        App entrypoint:
        - Load schedule and seed today's jobs
        - Start the tray icon
        - Start the file watcher in the background
        - Enter Tk main loop
        """
        # Initial schedule load + job creation
        self.blocks = self.parse_blocks()
        self.schedule_notifications()

        # Build and run the system tray icon
        self.make_tray_icon()

        # Kick off hot-reload watcher in the background
        threading.Thread(target=self.watch_files, daemon=True).start()

        # Start Tk event loop (blocks until window is closed)
        self.tk_root.mainloop()


# Only run the app when this file is executed directly (not on import)
if __name__ == "__main__":
    # Optional: quick visibility of the effective timezone at startup
    print(f"[tz] Using timezone: {TZ_NAME}")
    App().run()

