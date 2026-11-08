# pomodoro

A CLI pomodoro tracker for planning a day of focused work against real tasks,
not just running a 25-minute countdown.

You tell it how many pomodoros you're aiming for today (e.g. 16 = 8 hours of
work + breaks) and how to split them across tasks. It runs each 25/5 cycle,
alarms you when a block ends, and logs what you actually worked on — so at
the end of the day you can see planned vs. actual per task, and any shortfall
automatically carries into tomorrow's plan.

No dependencies — just Python 3 and the standard library. Works on macOS and
Linux.

## Usage

```
python3 pomodoro.py
```

On first run each day it asks how many pomodoros you're aiming for and lets
you allocate them to named tasks (yesterday's shortfall is pre-filled).
After that, running the same command lets you continue today's session
(including an in-progress pomodoro, if the CLI or computer got interrupted
mid-block), start over, add tasks, or edit/remove tasks.

Check progress without starting a timer:

```
python3 pomodoro.py --status
```

## During a pomodoro

- `p` — pause / resume the countdown (only active time counts toward the 25
  minutes, so stepping away doesn't inflate your log)
- `q` — stop and save progress; resumes automatically next run
- `Ctrl+C` — exit cleanly at any prompt; nothing is lost

When a work block or break ends, it notifies you (desktop notification +
sound) and keeps re-sounding every few seconds until you press a key to
dismiss it — meant to be hard to miss, not a toast you can ignore. While it
rings, system volume is raised to 70% if it was lower (and unmuted), then
restored.

## Data

Session state is stored per day in `~/.pomodoro/YYYY-MM-DD.json`, saved
every 30 seconds, on every completion, and on exit — a hard crash or reboot
loses at most ~30 seconds of progress.

Logs older than 7 days are deleted automatically on startup (the most
recent previous day is always kept, for carryover). Change `KEEP_DAYS` at
the top of `pomodoro.py` to keep more.

`repair_pomodoro.py` fixes day files affected by the old duplicate-task bug
(dry run by default; `--apply` to write, with a `.bak` backup).
