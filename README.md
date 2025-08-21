# scheduleblockingapp
desktop task switcher with notifications

Repo Layout

├── app
│   ├── main.py
│   ├── schedule.json          # Your daily blocks
│   ├── config.json            # Category colors + options
│   └── providers
│       ├── __init__.py
│       ├── base.py            # Common provider interface
│       ├── google_calendar.py # stub
│       ├── slack.py           # stub
│       └── hubstaff.py        # stub
├── requirements.txt
└── README.md