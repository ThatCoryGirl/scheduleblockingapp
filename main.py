# main.py
import json
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta

import tkinter as tk
from PIL import Image, ImageDraw
import pystray
from plyer import notification
from apscheduler.schedulers.background import BackgroundScheduler
from dateutil import tz

# ---------- Config ----------
SCHEDULE_PATH = Path(__file__).with_name("schedule.json")  # e.g., ./schedule.json
DEFAULT_COLOR = "#3b82f6"  # Tailwind blue-500
WINDOW_W = 200
WINDOW_H = 120

# ---------- App ----------
class App:
    def __init__(self):
        # Basic state
        self.tz = tz.tzlocal()
        self.blocks = []
        self.active_color = DEFAULT_COLOR
        self.paused = False
        self.icon = None
        self._drag_offset = (0, 0)
        self.reload_lock = threading.Lock()
        self.last_schedule_mtime = 0
        self.scheduler = BackgroundScheduler(timezone=self.tz)
        self.scheduler.start()

        # Simple config object
        self.config = type(
            "Config", (), {"win_x": 100, "win_y": 100, "compact_window": False}
        )()

        # Tk
        self.tk_root = tk.Tk()
        self.tk_root.title("Task Switcher")
        self.canvas = tk.Canvas(self.tk_root, width=WINDOW_W, height=WINDOW_H, highlightthickness=0)
        self.canvas.pack(padx=10, pady=6)

        self.tk_root.bind("<Double-1>", self.toggle_compact)
        self.tk_root.bind("<B1-Motion>", self.drag)
        self.tk_root.bind("<Button-1>", self.start_drag)
        self.tk_root.after(100, lambda: self.tk_root.geometry(f"+{self.config.win_x}+{self.config.win_y}"))

        # Render loop to keep color in sync with current block
        self.tk_root.after(500, self._tick_ui)

    # ---------- UI ----------
    def draw_window(self):
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, WINDOW_W, WINDOW_H, fill=self.active_color, outline="")

    def toggle_compact(self, _evt=None):
        self.config.compact_window = not self.config.compact_window
        w = 180 if self.config.compact_window else WINDOW_W
        h = 80 if self.config.compact_window else WINDOW_H
        self.tk_root.geometry(f"{w}x{h}")

    def start_drag(self, event):
        self._drag_offset = (event.x_root, event.y_root)

    def drag(self, event):
        dx = event.x_root - self._drag_offset[0]
        dy = event.y_root - self._drag_offset[1]
        x = self.tk_root.winfo_x() + dx
        y = self.tk_root.winfo_y() + dy
        self.tk_root.geometry(f"+{x}+{y}")
        self._drag_offset = (event.x_root, event.y_root)

    def _tick_ui(self):
        # Update active_color based on what block we're in right now
        now = datetime.now(self.tz).time()
        for b in self.blocks:
            if b["start_time"] <= now < b["end_time"]:
                self.active_color = b.get("color") or DEFAULT_COLOR
                break
        else:
            self.active_color = DEFAULT_COLOR

        self.draw_window()
        self.tk_root.after(1_000, self._tick_ui)

    # ---------- Tray ----------
    def make_tray_icon(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(59, 130, 246, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Pause/Resume", self.toggle_pause, default=False),
            pystray.MenuItem("Reload schedule", self.force_reload),
            pystray.MenuItem("Quit", self.quit)
        )
        self.icon = pystray.Icon("TaskSwitcher", img, "Task Switcher", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def toggle_pause(self, _=None):
        self.paused = not self.paused
        if not self.paused:
            self.schedule_notifications()
        else:
            self.scheduler.remove_all_jobs()

    # ---------- Schedule loading & parsing ----------
    def parse_blocks(self):
        """
        Expects schedule.json with a list of blocks like:
        [
          {"title": "Kount Queue/Vector Reviews", "start": "08:30", "end": "10:30", "color": "#22c55e"},
          {"title": "Break", "start": "10:30", "end": "10:45", "color": "#a3a3a3"}
        ]
        Times are local 24-hr HH:MM and repeat daily.
        """
        try:
            data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
            blocks = []
            for item in data:
                start = item["start"]
                end = item["end"]
                # Parse to time objects
                sh, sm = [int(x) for x in start.split(":")]
                eh, em = [int(x) for x in end.split(":")]
                start_t = datetime.now(self.tz).replace(hour=sh, minute=sm, second=0, microsecond=0).time()
                end_t = datetime.now(self.tz).replace(hour=eh, minute=em, second=0, microsecond=0).time()

                # Normalize if someone sets end before start (wrap past midnight)
                blocks.append({
                    "title": item.get("title", "Untitled"),
                    "start": start,
                    "end": end,
                    "start_time": start_t,
                    "end_time": end_t,
                    "color": item.get("color")
                })
            # Sort by start time
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
        """Schedule a toast at the start time of each block for today/tomorrow."""
        self.scheduler.remove_all_jobs()

        now = datetime.now(self.tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        def _mk_dt(date_, hhmm: str):
            hh, mm = [int(x) for x in hhmm.split(":")]
            return datetime(date_.year, date_.month, date_.day, hh, mm, tzinfo=self.tz)

        for b in self.blocks:
            dt_today = _mk_dt(today, b["start"])
            dt_next = dt_today if dt_today >= now else _mk_dt(tomorrow, b["start"])

            self.scheduler.add_job(
                self._notify_block,
                "date",
                run_date=dt_next,
                args=[b],
                id=f"block:{b['title']}:{dt_next.isoformat()}",
                misfire_grace_time=60
            )

        # Reschedule the next day’s notifications at 00:05
        midnight_plus = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=self.tz) + timedelta(minutes=5)
        self.scheduler.add_job(self.schedule_notifications, "date", run_date=midnight_plus)

    def _notify_block(self, block):
        if self.paused:
            return
        title = block.get("title", "Task Switch")
        body = f"Time to switch to: {title}"
        try:
            notification.notify(
                title="Task Switcher",
                message=body,
                timeout=8,  # seconds (Windows toast)
                app_name="Task Switcher"
            )
        except Exception as e:
            print(f"[warn] Notification failed: {e}")

    # ---------- Control ----------
    def force_reload(self, _=None):
        with self.reload_lock:
            self.blocks = self.parse_blocks()
            if not self.paused:
                self.schedule_notifications()

    def quit(self, _=None):
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
        while True:
            try:
                mtime = SCHEDULE_PATH.stat().st_mtime
                if mtime != self.last_schedule_mtime:
                    self.last_schedule_mtime = mtime
                    self.force_reload()
            except FileNotFoundError:
                # If file appears later, we’ll pick it up
                pass
            except Exception as e:
                print(f"[watch] error: {e}")
            time.sleep(2)

    # ---------- Boot ----------
    def run(self):
        # Initial load
        self.blocks = self.parse_blocks()
        self.schedule_notifications()
        self.make_tray_icon()

        # Watcher
        threading.Thread(target=self.watch_files, daemon=True).start()

        # Start UI
        self.tk_root.mainloop()


if __name__ == "__main__":
    App().run()

