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
from plyer import notification           # Cross-platform notifications (Windows toast on Win10+)
from apscheduler.schedulers.background import BackgroundScheduler  # Scheduler for timed jobs

# ---------- Config ----------
# Single source of truth for the app’s timezone (override with env var if needed).
TZ_NAME = os.environ.get("TASKSWITCHER_TZ", get_localzone_name())
APP_TZ = ZoneInfo(TZ_NAME)

# Path to the daily schedule file. Lives next to main.py (e.g., ./schedule.json).
SCHEDULE_PATH = Path(__file__).with_name("schedule.json")
# Default color if no current block matches, or a block has no color set.
DEFAULT_COLOR = "#3b82f6"  # Tailwind blue-500
# Base window dimensions (can be toggled to compact).
WINDOW_W = 200
WINDOW_H = 120

# Category colors (fallbacks if a block has no explicit "color" in JSON)
CAT_COLORS = {
    "focus":  "#3b82f6",  # blue
    "admin":  "#22c55e",  # green
    "break":  "#06b6d4",  # cyan
    "collab": "#f59e0b",  # amber
    "off":    "#64748b",  # slate
}

# ---------- App ----------
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
            "Config", (), {"win_x": 100, "win_y": 100, "compact_window": False}
        )()

        # ----- Tkinter window setup -----
        self.tk_root = tk.Tk()
        self.tk_root.title("Task Switcher")

        # Canvas is the colored rectangle "status light"
        self.canvas = tk.Canvas(self.tk_root, width=WINDOW_W, height=WINDOW_H, highlightthickness=0)
        self.canvas.pack(padx=10, pady=6)

        # Bindings:
        # - Double click toggles compact/normal size
        # - Click + drag moves the window around
        self.tk_root.bind("<Double-1>", self.toggle_compact)
        self.tk_root.bind("<B1-Motion>", self.drag)
        self.tk_root.bind("<Button-1>", self.start_drag)

        # Defer initial window placement slightly so Tk knows screen metrics
        self.tk_root.after(100, lambda: self.tk_root.geometry(f"+{self.config.win_x}+{self.config.win_y}"))

        # Start a UI "tick" loop that keeps the color in sync with the active block
        self.tk_root.after(500, self._tick_ui)

    # ---------- UI ----------
    def draw_window(self):
        """Redraw the canvas as a solid rectangle of the current active_color."""
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, WINDOW_W, WINDOW_H, fill=self.active_color, outline="")

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
        # Remove any previous jobs so we don't duplicate notifications
        self.scheduler.remove_all_jobs()

        now = datetime.now(self.tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        # Helper: build a timezone-aware datetime (today or tomorrow) for a given "HH:MM"
        def _mk_dt(date_, hhmm: str):
            hh, mm = [int(x) for x in hhmm.split(":")]
            return datetime(date_.year, date_.month, date_.day, hh, mm, tzinfo=self.tz)

        # For each block, figure out whether to schedule the toast today or tomorrow
        for b in self.blocks:
            dt_today = _mk_dt(today, b["start"])
            dt_next = dt_today if dt_today >= now else _mk_dt(tomorrow, b["start"])

            # One run per block start time
            self.scheduler.add_job(
                self._notify_block,                # function to call
                "date",                            # run once at a specific time
                run_date=dt_next,                  # when to run
                args=[b],                          # pass the block to the notifier
                id=f"block:{b['title']}:{dt_next.isoformat()}",  # unique id for safety
                misfire_grace_time=60              # allow up to 60s late if computer wakes
            )

        # At ~midnight, re-seed the following day's jobs (handles rolling schedule)
        midnight_plus = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=self.tz) + timedelta(minutes=5)
        self.scheduler.add_job(self.schedule_notifications, "date", run_date=midnight_plus)

    def _notify_block(self, block):
        """
        Show a system notification at the start of a block.
        If paused, do nothing.
        """
        if self.paused:
            return

        title = block.get("title", "Task Switch")
        body = f"Time to switch to: {title}"

        try:
            notification.notify(
                title="Task Switcher",
                message=body,
                timeout=8,         # seconds to display (platform-dependent)
                app_name="Task Switcher"
            )
        except Exception as e:
            # Notifications can fail on some environments; don't crash the app
            print(f"[warn] Notification failed: {e}")

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

