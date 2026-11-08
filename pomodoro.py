#!/usr/bin/env python3
"""Pomodoro CLI tracker with task allocation, pause/resume, and day-to-day carryover."""

import sys
import os
import json
import contextlib
import math
import re
import shutil
import signal
import time
import select
import termios
import tty
import platform
import subprocess
import argparse
import tempfile
from datetime import date, datetime, timedelta

WORK_SECONDS = 25 * 60
BREAK_SECONDS = 5 * 60
SAVE_EVERY_SECONDS = 30   # max progress lost if the process dies mid-block
ALARM_VOLUME = 70         # alarms raise system volume to at least this (0-100)
KEEP_DAYS = 7             # day logs older than this are deleted on startup
ALARM_REPEAT = 3          # seconds between alarm re-sounds...
ALARM_BACKOFF_AFTER = 300 # ...for the first 5 minutes of ringing,
ALARM_REPEAT_SLOW = 30    # then this often, to stay light if left unattended
DATA_DIR = os.path.expanduser("~/.pomodoro")
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


# ---------- storage ----------

def data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def day_file(d):
    return os.path.join(data_dir(), f"{d.isoformat()}.json")


def load_day(d):
    path = day_file(d)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_day(state):
    path = day_file(date.fromisoformat(state["date"]))
    fd, tmp = tempfile.mkstemp(dir=data_dir())
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def most_recent_previous_day(before):
    files = [f for f in os.listdir(data_dir()) if f.endswith(".json")]
    days = []
    for f in files:
        try:
            d = date.fromisoformat(f[:-5])
        except ValueError:
            continue
        if d < before:
            days.append(d)
    if not days:
        return None
    return max(days)


def prune_old_logs(today):
    """Deletes day logs (and repair-script .bak copies) older than KEEP_DAYS.
    The most recent previous day is always kept, even if it's older, since
    it's where the next session's carryover comes from."""
    cutoff = today - timedelta(days=KEEP_DAYS)
    keep = most_recent_previous_day(today)
    removed = 0
    for f in os.listdir(data_dir()):
        stem = f[:-len(".json.bak")] if f.endswith(".json.bak") else (
            f[:-len(".json")] if f.endswith(".json") else None)
        if stem is None:
            continue
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue
        if d < cutoff and d != keep:
            try:
                os.remove(os.path.join(data_dir(), f))
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"Removed {removed} log file(s) older than {KEEP_DAYS} days.")


# ---------- notifications ----------

def _run(cmd):
    """Runs a command quietly. Returns the CompletedProcess, or None if the
    tool is missing or fails to run."""
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def _linux_mixer():
    """'pactl' (PulseAudio / PipeWire) if present, else 'amixer' (ALSA), else None."""
    if shutil.which("pactl"):
        return "pactl"
    if shutil.which("amixer"):
        return "amixer"
    return None


def get_system_volume():
    """Returns the current output volume (0-100), or None if unavailable.
    Mac: osascript. Linux: pactl, falling back to amixer."""
    if IS_MAC:
        r = _run(["osascript", "-e", "output volume of (get volume settings)"])
        try:
            return int(r.stdout.strip()) if r and r.returncode == 0 else None
        except ValueError:
            return None
    if IS_LINUX:
        mixer = _linux_mixer()
        if mixer == "pactl":
            r = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
            m = re.search(r"(\d+)%", r.stdout) if r and r.returncode == 0 else None
        elif mixer == "amixer":
            r = _run(["amixer", "sget", "Master"])
            m = re.search(r"\[(\d+)%\]", r.stdout) if r and r.returncode == 0 else None
        else:
            m = None
        return int(m.group(1)) if m else None
    return None


def set_system_volume(level):
    """Sets the output volume (0-100). No-op if unavailable."""
    level = int(level)
    if IS_MAC:
        _run(["osascript", "-e", f"set volume output volume {level}"])
    elif IS_LINUX:
        mixer = _linux_mixer()
        if mixer == "pactl":
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
        elif mixer == "amixer":
            _run(["amixer", "-q", "sset", "Master", f"{level}%"])


def get_system_muted():
    """Returns True/False for the output mute state, or None if unavailable."""
    if IS_MAC:
        r = _run(["osascript", "-e", "output muted of (get volume settings)"])
        if r and r.returncode == 0 and r.stdout.strip() in ("true", "false"):
            return r.stdout.strip() == "true"
        return None
    if IS_LINUX:
        mixer = _linux_mixer()
        if mixer == "pactl":
            r = _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
            if r and r.returncode == 0:
                return "yes" in r.stdout.lower()
        elif mixer == "amixer":
            r = _run(["amixer", "sget", "Master"])
            if r and r.returncode == 0 and "[off]" in r.stdout:
                return True
            if r and r.returncode == 0 and "[on]" in r.stdout:
                return False
    return None


def set_system_muted(muted):
    """Mutes/unmutes the output. No-op if unavailable."""
    if IS_MAC:
        _run(["osascript", "-e", f"set volume output muted {'true' if muted else 'false'}"])
    elif IS_LINUX:
        mixer = _linux_mixer()
        if mixer == "pactl":
            _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"])
        elif mixer == "amixer":
            _run(["amixer", "-q", "sset", "Master", "mute" if muted else "unmute"])


def notify(title, message, sound=True):
    print(f"\n>>> {title}: {message}")
    # Via _run: a missing tool or a hung notification daemon can't stall the
    # alarm for more than its 5s timeout.
    if IS_MAC:
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
        _run(["osascript", "-e", script])
    elif IS_LINUX:
        _run(["notify-send", title, message])
    if sound:
        play_sound()


LINUX_SOUND_DIR = "/usr/share/sounds/freedesktop/stereo"


def _linux_sound_file(loud):
    """First freedesktop theme sound that exists on this machine, or None.
    The loud alarm prefers the more attention-grabbing one."""
    names = (["alarm-clock-elapsed", "complete", "bell", "message"] if loud
             else ["complete", "message", "bell"])
    for name in names:
        path = os.path.join(LINUX_SOUND_DIR, name + ".oga")
        if os.path.exists(path):
            return path, name
    return None, ("alarm-clock-elapsed" if loud else "complete")


def _sound_commands(loud):
    """Candidate player commands for this platform, best first.
    Mac: afplay. Linux: paplay, pw-play, ffplay or canberra-gtk-play (aplay
    is skipped: it can't decode the .oga theme sounds). Loudness comes from
    boosted_volume(), not from amplifying the sound itself."""
    if IS_MAC:
        return [["afplay", "/System/Library/Sounds/Sosumi.aiff"]]
    if IS_LINUX:
        path, name = _linux_sound_file(loud)
        cmds = []
        if path:
            cmds += [["paplay", path],
                     ["pw-play", path],
                     ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]]
        cmds.append(["canberra-gtk-play", "-i", name])
        return cmds
    return []


_working_sound_cmd = {}  # loud -> player command that worked last time


def _try_play(cmd):
    """Starts `cmd`; returns the process, or None if it failed right away."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    try:
        # A player that dies right away (no sound server, bad file)
        # should fall through to the next one instead of staying silent.
        if proc.wait(timeout=0.15) != 0:
            return None
    except subprocess.TimeoutExpired:
        pass  # still playing: good
    return proc


def start_sound(loud=False):
    """Starts the alert sound WITHOUT waiting for it to finish and returns
    the running process, so the caller can cut it short (see stop_sound).
    The player that worked is remembered, so re-sounds skip the search.
    Returns None if no player worked and the terminal bell was used instead."""
    cached = _working_sound_cmd.get(loud)
    if cached:
        proc = _try_play(cached)
        if proc is not None:
            return proc
    for cmd in _sound_commands(loud):
        if cmd == cached or not shutil.which(cmd[0]):
            continue
        proc = _try_play(cmd)
        if proc is not None:
            _working_sound_cmd[loud] = cmd
            return proc
    sys.stdout.write("\a")
    sys.stdout.flush()
    return None


def stop_sound(proc):
    """Cuts a sound started by start_sound short. Safe on None / finished."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def play_sound(loud=False):
    """Plays the alert sound and waits for it to finish (terminal bell if
    no player works)."""
    proc = start_sound(loud)
    if proc is not None:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            stop_sound(proc)


@contextlib.contextmanager
def boosted_volume():
    """On Mac and Linux, temporarily raises system output volume to
    ALARM_VOLUME if it's currently lower (and unmutes) so an alert is
    audible, then restores both on exit. Never goes to max; only lowers the
    level when unmuting a muted output that was set above ALARM_VOLUME."""
    prior_volume = get_system_volume()
    prior_muted = get_system_muted()
    # Raise a quiet output. If muted, also bring a high level down to
    # ALARM_VOLUME before unmuting, so muted-at-100% doesn't blast headphones.
    change_volume = prior_volume is not None and (
        prior_volume < ALARM_VOLUME or (prior_muted and prior_volume != ALARM_VOLUME))
    try:
        if change_volume:
            set_system_volume(ALARM_VOLUME)
        if prior_muted:
            set_system_muted(False)
        yield
    finally:
        if change_volume:
            set_system_volume(prior_volume)
        if prior_muted:
            set_system_muted(True)


def alert_once(title, message):
    """One loud notification + sound, then volume goes back to what it was."""
    with boosted_volume():
        notify(title, message, sound=False)
        play_sound(loud=True)


def alarm_until_dismissed(title, message):
    """Fires the notification once, then keeps re-sounding it every few
    seconds until the user presses any key — like an alarm clock, not a
    toast you can miss. The sound plays in the background, so a keypress
    dismisses it immediately instead of waiting for the sound to finish.
    Volume is boosted (and unmuted) while it rings, then restored."""
    with boosted_volume():
        notify(title, message, sound=False)
        proc = start_sound(loud=True)
        try:
            if not sys.stdin.isatty():
                if proc is not None:
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pass
                return
            print(">>> Press any key to dismiss the alarm...")
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                # Drop keys typed before the alarm started, so stray
                # typeahead can't dismiss it before it's heard.
                termios.tcflush(fd, termios.TCIFLUSH)
                started = last_sound = time.time()
                while True:
                    # Sleep until the next re-sound is due (no polling);
                    # after a while unattended, re-sound less often.
                    ringing = time.time() - started
                    interval = ALARM_REPEAT if ringing < ALARM_BACKOFF_AFTER else ALARM_REPEAT_SLOW
                    wait = max(0.0, last_sound + interval - time.time())
                    if wait == 0.0 and proc is not None and proc.poll() is None:
                        wait = 0.5  # due, but the last sound is still playing
                    rlist, _, _ = select.select([sys.stdin], [], [], wait)
                    if rlist:
                        sys.stdin.read(1)
                        break
                    if time.time() - last_sound >= interval and (proc is None or proc.poll() is not None):
                        proc = start_sound(loud=True)
                        last_sound = time.time()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            print("Dismissed.")
        finally:
            stop_sound(proc)


# ---------- keyboard ----------

BAR_WIDTH = 24


def render_progress(label, active, total, paused):
    frac = min(active / total, 1) if total else 1
    filled = int(BAR_WIDTH * frac)
    bar = "#" * filled + "-" * (BAR_WIDTH - filled)
    remaining = int(math.ceil(max(total - active, 0)))
    mm, ss = divmod(remaining, 60)
    icon = "||" if paused else ">>"
    sys.stdout.write(f"\r{icon} {label:<10} [{bar}] {mm:02d}:{ss:02d}   ")
    sys.stdout.flush()


MAX_STEP = 2.0  # seconds; a loop pass normally takes ~1s


def countdown(total_seconds, label, already_active=0, on_tick=None):
    """Runs a countdown, supports 'p' pause/resume and 'q' to bail out early.
    Progress is measured in real elapsed time, not loop passes, so
    keypresses can't skip ahead. Each pass credits at most MAX_STEP seconds,
    so a suspended laptop doesn't wake up with the block "done": only time
    the process was actually running counts.
    Returns (active_seconds_reached, aborted_bool)."""
    active = float(already_active)
    paused = False
    interactive = sys.stdin.isatty()
    fd = sys.stdin.fileno() if interactive else None
    old_settings = termios.tcgetattr(fd) if interactive else None
    aborted = False
    if interactive:
        print("(p: pause/resume, q: stop and save progress)")
    try:
        if interactive:
            tty.setcbreak(fd)
        last = time.time()
        while active < total_seconds:
            render_progress(label, active, total_seconds, paused)

            key = None
            if interactive:
                # Wake once a second to redraw; while paused nothing changes
                # until a key is pressed, so block with no timeout at all.
                rlist, _, _ = select.select([sys.stdin], [], [], None if paused else 1.0)
                if rlist:
                    key = sys.stdin.read(1)
            else:
                time.sleep(1.0)

            # Credit the real time that passed since the last pass, clamped
            # to [0, MAX_STEP]: a backwards clock change can't un-count
            # progress, and a suspend/resume gap isn't counted as work.
            now = time.time()
            elapsed = min(max(0.0, now - last), MAX_STEP)
            last = now
            if not paused:
                active += elapsed

            if key:
                if key.lower() == "p":
                    paused = not paused
                elif key.lower() == "q":
                    aborted = True
                    break

            if not paused and on_tick:
                on_tick(active, paused)
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        render_progress(label, active, total_seconds, paused)
        sys.stdout.write("\n")
    return int(min(active, total_seconds)), aborted


# ---------- task allocation ----------

def prompt_int(prompt, default=None):
    """Asks until it gets a whole number >= 0 (or Enter, if there's a default)."""
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a number.")
            continue
        if value < 0:
            print("Please enter 0 or more.")
            continue
        return value


def add_task_amount(tasks, name, amount):
    """Adds `amount` pomodoros to a task named `name`, merging into an
    existing entry if one already exists instead of creating a duplicate."""
    existing = next((t for t in tasks if t["name"] == name), None)
    if existing:
        existing["planned"] += amount
    else:
        tasks.append({"name": name, "planned": amount, "completed": 0})


def consolidate_tasks(tasks):
    """Merges any tasks that share a name (e.g. from older buggy data)
    into a single entry, summing planned and completed counts. Keeps
    first-seen order."""
    merged = []
    index_by_name = {}
    for t in tasks:
        if t["name"] in index_by_name:
            m = merged[index_by_name[t["name"]]]
            m["planned"] += t["planned"]
            m["completed"] += t["completed"]
        else:
            index_by_name[t["name"]] = len(merged)
            merged.append(dict(t))
    return merged


def allocate_tasks(total, carryover):
    tasks = []
    if carryover:
        print("\nShortfall carried over from your last session:")
        for name, amount in carryover.items():
            print(f"  {name}: {amount} short")
        for name, amount in carryover.items():
            add_task_amount(tasks, name, amount)

    allocated = sum(t["planned"] for t in tasks)
    print(f"\nAllocating {total} pomodoros to tasks ({allocated} already carried over).")
    print("Enter task name and count. Press Enter on task name when done"
          " (remaining pomodoros will be assigned as you go).")
    while allocated < total:
        name = input(f"Task name ({total - allocated} left to allocate, Enter to stop): ").strip()
        if not name:
            break
        count = prompt_int(f"How many pomodoros for '{name}'? ")
        count = min(count, total - allocated)
        add_task_amount(tasks, name, count)
        allocated += count
    return tasks


def new_day_state(d, carryover):
    print(f"=== Pomodoro tracker: {d.isoformat()} ===")
    default_total = 16
    total = prompt_int(f"How many pomodoros are you aiming for today? [{default_total}] ",
                        default=default_total)
    tasks = allocate_tasks(total, carryover)
    return {
        "date": d.isoformat(),
        "planned_total": total,
        "tasks": tasks,
        "completed_pomodoros": [],
        "current": None,
        "batch": None,
    }


def compute_carryover(prev_state):
    if not prev_state:
        return {}
    shortfall = {}
    for t in prev_state.get("tasks", []):
        diff = t["planned"] - t["completed"]
        if diff > 0:
            shortfall[t["name"]] = diff
    return shortfall


# ---------- task selection during the day ----------

def available_tasks(state):
    """Tasks that still have pomodoros left, in the order they were added."""
    return [t for t in state["tasks"] if t["completed"] < t["planned"]]


def ask_task(state):
    options = available_tasks(state)

    if options:
        print("\nAvailable tasks:")
        for i, t in enumerate(options, 1):
            remaining = t["planned"] - t["completed"]
            print(f"  {i}. {t['name']} ({remaining} left)")
        prompt = "Pick a number, type a new task name, or press Enter for #1: "
    else:
        prompt = "What task is this pomodoro for? "

    while True:
        raw = input(prompt).strip()
        if not raw:
            return options[0]["name"] if options else "unplanned"
        if not raw.isdigit():
            break
        idx = int(raw)
        if 1 <= idx <= len(options):
            return options[idx - 1]["name"]
        if options:
            print(f"No task #{idx} — pick 1-{len(options)}, or type a name.")
        else:
            print("Task names can't be just a number.")

    name = raw
    for t in state["tasks"]:
        if t["name"] == name:
            return name
    state["tasks"].append({"name": name, "planned": 0, "completed": 0})
    return name


def record_completion(state, task_name, active_seconds, took_break, aborted, rest_seconds=0):
    target = next((t for t in state["tasks"]
                    if t["name"] == task_name and t["completed"] < t["planned"]), None)
    if target is None:
        target = next((t for t in state["tasks"] if t["name"] == task_name), None)
    if target is not None:
        target["completed"] += 1
    else:
        state["tasks"].append({"name": task_name, "planned": 0, "completed": 1})
    state["completed_pomodoros"].append({
        "task": task_name,
        "end": datetime.now().isoformat(timespec="seconds"),
        "active_seconds": active_seconds,
        "took_break": took_break,
        "rest_seconds": rest_seconds,
        "aborted": aborted,
    })
    state["current"] = None
    save_day(state)


# ---------- break prompt ----------

def ask_break_or_continue():
    """Asks 'break or continue?' while the clock keeps running: time spent
    sitting at the prompt counts as rest. Once BREAK_SECONDS have passed the
    rest is already done, so the user is told so and the only way out is to
    continue (whatever they type).

    Returns (choice, rest_seconds), choice being 'break' or 'continue'."""
    prompt = "Break now or continue working? [B/c] "
    start = time.time()

    if not sys.stdin.isatty():
        raw = input(prompt).strip().lower()
        idle = time.time() - start
        if idle >= BREAK_SECONDS:
            return "continue", BREAK_SECONDS
        return ("continue" if raw == "c" else "break"), int(idle)

    sys.stdout.write(prompt)
    sys.stdout.flush()
    announced = False
    line = ""
    while True:
        rlist, _, _ = select.select([sys.stdin], [], [], 1.0)
        if not announced and time.time() - start >= BREAK_SECONDS:
            announced = True
            sys.stdout.write("\n>>> Rest time is up - press Enter to continue. ")
            sys.stdout.flush()
            alert_once("Break over", "Time to get back to it.")
        if rlist:
            line = sys.stdin.readline()
            break

    idle = time.time() - start
    if idle >= BREAK_SECONDS:
        return "continue", BREAK_SECONDS
    return ("continue" if line.strip().lower() == "c" else "break"), int(idle)


# ---------- main run loop ----------

def run_pomodoro(state):
    cur = state["current"]
    if cur is None:
        batch = state.get("batch")
        if batch and batch.get("remaining", 0) > 0:
            task_name = batch["task"]
            print(f"Continuing '{task_name}' — {batch['remaining']} more queued in this batch.")
        else:
            task_name = ask_task(state)
            t = next((x for x in state["tasks"] if x["name"] == task_name), None)
            remaining = (t["planned"] - t["completed"]) if t else 0
            if remaining > 1:
                count = prompt_int(
                    f"'{task_name}' has {remaining} pomodoros left — how many would you "
                    f"like to run back-to-back without being asked again? "
                    f"[1-{remaining}, default {remaining}] ", default=remaining)
                count = max(1, min(count, remaining))
                state["batch"] = {"task": task_name, "remaining": count}
            else:
                state["batch"] = None
        state["current"] = {
            "task": task_name,
            "phase": "work",
            "active_seconds": 0,
        }
        save_day(state)
        cur = state["current"]
    else:
        task_name = cur["task"]
        print(f"Resuming pomodoro for '{task_name}' "
              f"({cur['active_seconds']}s of work already logged, phase={cur['phase']}).")

    if cur["phase"] == "work":
        last_saved = cur["active_seconds"]

        def tick(active, paused):
            nonlocal last_saved
            state["current"]["active_seconds"] = int(active)
            state["current"]["phase"] = "work"
            if active - last_saved >= SAVE_EVERY_SECONDS:
                save_day(state)
                last_saved = active

        active, aborted = countdown(WORK_SECONDS, f"[{task_name}] Work", cur["active_seconds"], tick)
        state["current"]["active_seconds"] = active
        save_day(state)
        if aborted:
            print("Stopped early. Progress saved — run again to resume this pomodoro.")
            return "quit"

        alarm_until_dismissed("Pomodoro complete", f"Finished a work block on '{task_name}'.")
        choice, rest_seconds = ask_break_or_continue()
        # Waiting 5+ minutes at the prompt already was the break.
        took_break = choice == "break" or rest_seconds >= BREAK_SECONDS
        if choice == "continue" and rest_seconds >= BREAK_SECONDS:
            print("You already rested through the prompt - continuing.")
        state["current"]["phase"] = "break" if choice == "break" else "done"
        state["current"]["break_seconds"] = rest_seconds
        state["current"]["took_break"] = took_break  # so a resume logs it right
        save_day(state)
    else:
        took_break = cur.get("took_break", cur["phase"] == "break")
        rest_seconds = cur.get("break_seconds", 0)

    if state["current"]["phase"] == "break":
        # Starts from whatever rest was already banked at the prompt.
        break_start = state["current"].get("break_seconds", 0)
        last_break_save = break_start

        def break_tick(active, paused):
            nonlocal last_break_save
            state["current"]["break_seconds"] = int(active)
            if active - last_break_save >= SAVE_EVERY_SECONDS:
                save_day(state)
                last_break_save = active

        active_b, aborted_b = countdown(BREAK_SECONDS, "Break", break_start, break_tick)
        rest_seconds = active_b
        if aborted_b:
            print("Break skipped, moving on.")
        else:
            alarm_until_dismissed("Break over", "Time to get back to it.")

    # Update the batch before record_completion, so its single save covers
    # both: a crash in between can't replay an extra batch pomodoro.
    batch = state.get("batch")
    if batch and batch.get("task") == task_name:
        batch["remaining"] -= 1
        if batch["remaining"] <= 0:
            state["batch"] = None

    record_completion(state, task_name, state["current"]["active_seconds"],
                       took_break, False, rest_seconds)

    print(f"Logged pomodoro #{len(state['completed_pomodoros'])} for '{task_name}'.\n")
    return "ok"


def print_summary(state):
    print(f"\n=== Summary for {state['date']} ===")
    total_planned = sum(t["planned"] for t in state["tasks"])
    total_done = sum(t["completed"] for t in state["tasks"])
    breaks_taken = sum(1 for p in state["completed_pomodoros"] if p["took_break"])
    breaks_total = len(state["completed_pomodoros"])
    active_minutes = sum(p["active_seconds"] for p in state["completed_pomodoros"]) / 60
    for t in state["tasks"]:
        mark = "OK" if t["completed"] >= t["planned"] else "SHORT"
        print(f"  {t['name']:<20} {t['completed']}/{t['planned']}  [{mark}]")
    print(f"  {'TOTAL':<20} {total_done}/{total_planned}")
    print(f"  Active work time: {active_minutes:.0f} min")
    print(f"  Breaks taken: {breaks_taken}/{breaks_total}")
    rest_minutes = sum(p.get("rest_seconds", 0) for p in state["completed_pomodoros"]) / 60
    print(f"  Rest time: {rest_minutes:.0f} min")
    if total_done < total_planned:
        print(f"  Shortfall will carry over to your next session.")


def confirm_dismiss(state):
    """Dismissing overwrites today's file, so ask before throwing away any
    pomodoros already logged today."""
    logged = len(state["completed_pomodoros"])
    in_progress = state.get("current") is not None
    if not logged and not in_progress:
        return True
    what = f"{logged} completed pomodoro(s)"
    if in_progress:
        what += " and the block in progress"
    choice = input(f"This deletes today's log ({what}). Really start over? [y/N] ").strip().lower()
    return choice == "y"


def ask_restart_action():
    """Called when today's session file already exists on script startup.
    Returns 'continue', 'dismiss', 'add', or 'edit'."""
    print("\nYou already have a session in progress today.")
    choice = input("Continue where you left off, dismiss it and start over, "
                    "add new tasks, or edit/remove existing tasks? [C/d/a/e] ").strip().lower()
    if choice.startswith("d"):
        return "dismiss"
    if choice.startswith("a"):
        return "add"
    if choice.startswith("e"):
        return "edit"
    return "continue"


def edit_tasks(state):
    """Interactive loop: change a task's planned pomodoro count, or remove
    it entirely. Keeps planned_total in sync with whatever changes."""
    while True:
        if not state["tasks"]:
            print("No tasks to edit.")
            return
        print("\nCurrent tasks:")
        for i, t in enumerate(state["tasks"], 1):
            print(f"  {i}. {t['name']}: {t['completed']}/{t['planned']}")
        raw = input("Enter a number to edit its count, 'r<number>' to remove it, "
                     "or Enter to finish: ").strip()
        if not raw:
            return

        if raw.lower().startswith("r"):
            idx_str = raw[1:].strip()
            if not idx_str.isdigit():
                print("Use 'r' followed by a task number, e.g. r2.")
                continue
            idx = int(idx_str)
            if not (1 <= idx <= len(state["tasks"])):
                print(f"No task #{idx}.")
                continue
            t = state["tasks"][idx - 1]
            if t["completed"] > 0:
                confirm = input(
                    f"'{t['name']}' already has {t['completed']} completed pomodoro(s) "
                    "logged — remove anyway? [y/N] ").strip().lower()
                if confirm != "y":
                    continue
            state["planned_total"] -= t["planned"]
            state["tasks"].pop(idx - 1)
            if state.get("batch") and state["batch"].get("task") == t["name"]:
                state["batch"] = None
            print(f"Removed '{t['name']}'.")
            continue

        if raw.isdigit():
            idx = int(raw)
            if not (1 <= idx <= len(state["tasks"])):
                print(f"No task #{idx}.")
                continue
            t = state["tasks"][idx - 1]
            new_count = prompt_int(
                f"New pomodoro count for '{t['name']}' "
                f"(currently {t['planned']}, {t['completed']} completed): ")
            if new_count < t["completed"]:
                print(f"Can't set below the {t['completed']} already completed.")
                continue
            state["planned_total"] += new_count - t["planned"]
            t["planned"] = new_count
            batch = state.get("batch")
            if batch and batch.get("task") == t["name"]:
                still_left = t["planned"] - t["completed"]
                if still_left <= 0:
                    state["batch"] = None
                else:
                    batch["remaining"] = min(batch["remaining"], still_left)
            continue

        print("Didn't understand that.")


def prompt_new_tasks():
    """Asks how many new tasks to add, then name + pomodoro count for each."""
    count = prompt_int("How many new tasks would you like to add? ")
    tasks = []
    for i in range(count):
        name = input(f"Task {i + 1} name: ").strip()
        if not name:
            name = f"task{i + 1}"
        amount = prompt_int(f"How many pomodoros for '{name}'? ")
        tasks.append({"name": name, "planned": amount, "completed": 0})
    return tasks


def main():
    parser = argparse.ArgumentParser(description="Pomodoro CLI tracker")
    parser.add_argument("--status", action="store_true", help="show today's progress and exit")
    args = parser.parse_args()

    today = date.today()
    state = load_day(today)
    if state:
        state["tasks"] = consolidate_tasks(state["tasks"])

    if args.status:
        if not state:
            print("No pomodoro session started today yet.")
        else:
            print_summary(state)
        return

    # Turn SIGHUP (terminal closed) / SIGTERM into a normal exit so finally
    # blocks run: progress gets saved and a raised alarm volume is restored.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(1))

    prune_old_logs(today)

    def carryover_from_previous_day(warn=False):
        prev_day = most_recent_previous_day(today)
        prev_state = load_day(prev_day) if prev_day else None
        if warn and prev_state and prev_state.get("current"):
            cur = prev_state["current"]
            print(f"Note: {prev_day.isoformat()} ended with an unfinished block on "
                  f"'{cur.get('task')}' ({cur.get('active_seconds', 0) // 60} min). "
                  "It isn't logged; that task's shortfall still carries over.")
        return compute_carryover(prev_state)

    try:
        if state is None:
            # Always a new file for today: work done today is logged under
            # today's date, with yesterday's shortfall offered as a start.
            state = new_day_state(today, carryover_from_previous_day(warn=True))
            save_day(state)
        else:
            action = ask_restart_action()
            if action == "dismiss" and not confirm_dismiss(state):
                print("Kept today's session.")
                action = "continue"
            if action == "dismiss":
                state = new_day_state(today, carryover_from_previous_day())
                save_day(state)
            elif action == "add":
                new_tasks = prompt_new_tasks()
                added_total = 0
                for nt in new_tasks:
                    added_total += nt["planned"]
                    add_task_amount(state["tasks"], nt["name"], nt["planned"])
                state["planned_total"] += added_total
                save_day(state)
            elif action == "edit":
                edit_tasks(state)
                save_day(state)
            # "continue" falls through unchanged

        while True:
            done = sum(t["completed"] for t in state["tasks"])
            batch_active = bool(state.get("batch") and state["batch"].get("remaining", 0) > 0)
            if state["current"] is None and not batch_active and done >= state["planned_total"]:
                print_summary(state)
                more = prompt_int(
                    f"You've hit today's target ({done}/{state['planned_total']}). "
                    "Add more pomodoros? [0 to stop] ", default=0)
                if more <= 0:
                    break
                # Raise the target only by what actually got allocated, after
                # allocating: stopping early or Ctrl-C can't leave it inflated.
                extras = allocate_tasks(more, {})
                for extra in extras:
                    add_task_amount(state["tasks"], extra["name"], extra["planned"])
                state["planned_total"] += sum(e["planned"] for e in extras)
                save_day(state)
                if not extras:
                    break
                continue
            result = run_pomodoro(state)
            if result == "quit":
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        # Catches progress since the last periodic save (Ctrl-C, SIGHUP, ...).
        if state is not None:
            save_day(state)

    if state is not None:
        print_summary(state)


if __name__ == "__main__":
    main()
