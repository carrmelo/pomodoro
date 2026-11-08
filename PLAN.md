# Design notes

Why this exists: a CLI pomodoro tracker for a flexible-hours job, where the
goal isn't just "run a timer" but "prove I covered my hours across the tasks
I planned, and carry over whatever I didn't get to."

## Decisions made

- **Task allocation up front, flexible in practice.** You set a daily total
  (e.g. 16 = 8h) and split it across named tasks, but any pomodoro can be
  logged against a task outside the plan — the log stays honest even when
  the day doesn't go as planned.
- **Active time, not wall-clock time.** `p` pauses the countdown; only
  unpaused seconds count toward the 25 minutes. This matters for flexible
  work where you sometimes step away mid-block — the log should reflect
  effort, not elapsed clock time. Time is measured by the real clock (so
  keypresses can't skip ahead), but each loop pass credits at most 2s, so a
  laptop suspended mid-block doesn't wake up with the block "done".
- **Skipping a break is allowed but recorded.** Continuing straight into the
  next block logs `took_break: false` instead of forcing a rest. The point
  is visibility (e.g. "3 of 15 breaks taken this week"), not enforcement.
  Time spent sitting at the "break or continue?" prompt counts as rest
  (`rest_seconds`); if it reaches 5 minutes, the break already happened.
- **Alarm, not a toast.** A single notification/sound is easy to miss.
  Instead the end-of-block signal re-fires every few seconds until you
  press a key to dismiss it. While it rings, system volume is raised to
  `ALARM_VOLUME` (70%) if lower and unmuted, then restored — never to max,
  since headphones. SIGHUP/SIGTERM still restore it.
- **Crash-safe by construction.** State is written to
  `~/.pomodoro/YYYY-MM-DD.json` every 30s during a block or break, on every
  phase change/completion, and on exit (Ctrl-C, terminal closed). A hard
  crash loses at most ~30s; the next run detects the in-progress block and
  resumes it.
- **Shortfall carries over automatically.** On a new day, if the most recent
  previous day has `completed < planned` for any task, that gap is offered
  as a starting allocation for today. Work is always logged under the day
  it happened — yesterday's file is never reopened.
- **Restart menu.** If today's file exists, startup offers continue /
  dismiss (asks for confirmation if anything is logged) / add tasks / edit
  or remove tasks.
- **Batches.** Picking a task with several pomodoros left asks how many to
  run back-to-back without being asked for the task again.
- **Logs are short-lived.** Day files older than 7 days (`KEEP_DAYS`) are
  deleted on startup, except the most recent previous day, which is kept
  for carryover however old it is.
- **Hitting your target isn't the end.** Reaching `planned_total` prompts
  "add more?" instead of just exiting, since some days run long.
- **Data stays out of the repo.** Task names can reveal work/client context,
  so `~/.pomodoro/` (actual logs) is never committed — only the script
  itself is public.

## Possible future work (not started)

- First-run setup: save defaults (daily total, e.g. 16; a base task
  template with pre-assigned pomodoros) so a new day only asks about what's
  left to decide. Skippable, and editable later.
- Weekly/rolling summary across the (up to 7) kept day files.
- Configurable work/break lengths (currently fixed 25/5).
- Optional short PR/commit-style tagging of what was actually produced per
  pomodoro, not just the task name.
