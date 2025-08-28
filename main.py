import json
self.canvas = tk.Canvas(root, width=180, height=80, highlightthickness=0)
self.canvas.pack(padx=10, pady=6)
root.bind("<Double-1>", self.toggle_compact)
root.bind("<B1-Motion>", self.drag)
root.bind("<Button-1>", self.start_drag)
root.after(100, lambda: root.geometry(f"+{self.config.win_x}+{self.config.win_y}"))
return root


def draw_window(self):
self.canvas.delete("all")
self.canvas.create_rectangle(0, 0, 180, 80, fill=self.active_color, outline="")


def toggle_compact(self, _evt=None):
w = 120 if self.config.compact_window else 200
h = 120 if self.config.compact_window else 120
self.config.compact_window = not self.config.compact_window
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


def make_tray_icon(self):
img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse((8, 8, 56, 56), fill=(59,130,246,255))
menu = pystray.Menu(
pystray.MenuItem("Pause", self.toggle_pause, default=False),
pystray.MenuItem("Reload schedule", self.force_reload),
pystray.MenuItem("Quit", self.quit)
)
self.icon = pystray.Icon("TaskSwitcher", img, "Task Switcher", menu)
threading.Thread(target=self.icon.run, daemon=True).start()


def toggle_pause(self, _=None):
self.paused = not self.paused
if not self.paused:
self.schedule_notifications()


def force_reload(self, _=None):
with self.reload_lock:
self.blocks = self.parse_blocks()
self.schedule_notifications()


def quit(self, _=None):
if self.icon:
self.icon.stop()
self.scheduler.shutdown()
self.tk_root.destroy()


def watch_files(self):
# Simple polling watcher; watchdog is installed if you prefer more advanced watching
while True:
try:
mtime = SCHEDULE_PATH.stat().st_mtime
if mtime != self.last_schedule_mtime:
self.last_schedule_mtime = mtime
self.force_reload()
except Exception:
pass
time.sleep(30)


def run(self):
self.blocks = self.parse_blocks()
self.schedule_notifications()
self.make_tray_icon()
threading.Thread(target=self.watch_files, daemon=True).start()
self.tk_root.mainloop()


if __name__ == "__main__":
App().run()