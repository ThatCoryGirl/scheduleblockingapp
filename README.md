# scheduleblockingapp
desktop task switcher with notifications

Repo Layout

├── app

│     ├── main.py

│     ├── schedule.json          # Your daily blocks

│     ├── config.json            # Category colors + options

│     └── providers

│           ├── __init__.py

│           ├── base.py            # Common provider interface

│           ├── google_calendar.py # stub

│           ├── slack.py           # stub

│           └── hubstaff.py        # stub

├── requirements.txt

└── README.md

# Desktop Task Switcher (MVP)


Lightweight task‑switch reminders with a color cue on your desktop.


## Setup
1. `python -m venv .venv` and activate it
2. `pip install -r requirements.txt`
3. Edit `app/schedule.json` and `app/config.json`
4. `python app/main.py`


## Usage tips
- Edit `schedule.json` anytime; the app will reload within ~30 seconds or use tray → Reload.
- Double‑click the window to toggle compact mode. Drag anywhere to move.


## Dev notes
- Scheduler: APScheduler (date triggers per block)
- Notifications: plyer
- Tray: pystray; icon drawn with Pillow
- UI: Tkinter


## Roadmap
- [ ] Recurring weekday/weekend schedules per day of week
- [ ] Import from Google Calendar (read‑only)
- [ ] Slack DM reminders (webhook)
- [ ] Hubstaff sync (map activities → categories)
- [ ] Export day summary (CSV)
- [ ] Packaged .exe via PyInstaller
