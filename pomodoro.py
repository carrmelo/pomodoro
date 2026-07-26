#!/usr/bin/env python3
"""Pomodoro CLI tracker with task allocation, pause/resume, and day-to-day carryover."""

import sys
import os
import json
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


# ---------- notifications ----------

def notify(title, message):
    print(f"\n>>> {title}: {message}")
    try:
        if IS_MAC:
            script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
            subprocess.run(["osascript", "-e", script], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_LINUX:
            subprocess.run(["notify-send", title, message], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass
    play_sound()


def play_sound():
    try:
        if IS_MAC:
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        elif IS_LINUX:
            for cmd in (["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                        ["aplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"]):
                try:
                    subprocess.run(cmd, check=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
    except Exception:
        pass
    sys.stdout.write("\a")
    sys.stdout.flush()


def alarm_until_dismissed(title, message):
    """Fires the notification once, then keeps re-sounding it every few
    seconds until the user presses any key — like an alarm clock, not a
    toast you can miss."""
    notify(title, message)
    interactive = sys.stdin.isatty()
    if not interactive:
        return
    print(">>> Press any key to dismiss the alarm...")
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        last_sound = time.time()
        while True:
            rlist, _, _ = select.select([sys.stdin], [], [], 0.5)
            if rlist:
                sys.stdin.read(1)
                break
            if time.time() - last_sound >= 3:
                play_sound()
                last_sound = time.time()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    print("Dismissed.")


# ---------- keyboard ----------

BAR_WIDTH = 24


def render_progress(label, active, total, paused):
    frac = active / total if total else 1
    filled = int(BAR_WIDTH * frac)
    bar = "#" * filled + "-" * (BAR_WIDTH - filled)
    remaining = max(total - active, 0)
    mm, ss = divmod(remaining, 60)
    icon = "||" if paused else ">>"
    sys.stdout.write(f"\r{icon} {label:<10} [{bar}] {mm:02d}:{ss:02d}   ")
    sys.stdout.flush()


def countdown(total_seconds, label, already_active=0, on_tick=None):
    """Runs a countdown, supports 'p' pause/resume and 'q' to bail out early.
    Returns (active_seconds_reached, aborted_bool)."""
    active = already_active
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
        while active < total_seconds:
            render_progress(label, active, total_seconds, paused)

            key = None
            if interactive:
                rlist, _, _ = select.select([sys.stdin], [], [], 1.0)
                if rlist:
                    key = sys.stdin.read(1)
            else:
                time.sleep(1)

            if key:
                if key.lower() == "p":
                    paused = not paused
                elif key.lower() == "q":
                    aborted = True
                    break

            if not paused:
                active += 1
                if on_tick:
                    on_tick(active, paused)
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        render_progress(label, active, total_seconds, paused)
        sys.stdout.write("\n")
    return active, aborted


# ---------- task allocation ----------

def prompt_int(prompt, default=None):
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            return int(raw)
        except ValueError:
            print("Please enter a number.")


def allocate_tasks(total, carryover):
    tasks = []
    if carryover:
        print("\nShortfall carried over from your last session:")
        for name, amount in carryover.items():
            print(f"  {name}: {amount} short")
        for name, amount in carryover.items():
            tasks.append({"name": name, "planned": amount, "completed": 0})

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
        tasks.append({"name": name, "planned": count, "completed": 0})
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

def next_task_name(state):
    for t in state["tasks"]:
        if t["completed"] < t["planned"]:
            return t["name"]
    return None


def ask_task(state):
    suggestion = next_task_name(state)
    prompt = f"What task is this pomodoro for? [{suggestion}] " if suggestion else "What task is this pomodoro for? "
    name = input(prompt).strip()
    if not name:
        name = suggestion
    if not name:
        name = "unplanned"
    for t in state["tasks"]:
        if t["name"] == name:
            return name
    state["tasks"].append({"name": name, "planned": 0, "completed": 0})
    return name


def record_completion(state, task_name, active_seconds, took_break, aborted):
    for t in state["tasks"]:
        if t["name"] == task_name:
            t["completed"] += 1
            break
    state["completed_pomodoros"].append({
        "task": task_name,
        "end": datetime.now().isoformat(timespec="seconds"),
        "active_seconds": active_seconds,
        "took_break": took_break,
        "aborted": aborted,
    })
    state["current"] = None
    save_day(state)


# ---------- main run loop ----------

def run_pomodoro(state):
    cur = state["current"]
    if cur is None:
        task_name = ask_task(state)
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
        def tick(active, paused):
            state["current"]["active_seconds"] = active
            state["current"]["phase"] = "work"
            if int(active) % 5 == 0:
                save_day(state)

        active, aborted = countdown(WORK_SECONDS, f"[{task_name}] Work", cur["active_seconds"], tick)
        state["current"]["active_seconds"] = active
        save_day(state)
        if aborted:
            print("Stopped early. Progress saved — run again to resume this pomodoro.")
            return "quit"

        alarm_until_dismissed("Pomodoro complete", f"Finished a work block on '{task_name}'.")
        choice = input("Break now or continue working? [B/c] ").strip().lower()
        took_break = choice != "c"
        state["current"]["phase"] = "break" if took_break else "done"
        save_day(state)
    else:
        took_break = cur["phase"] == "break"

    if took_break:
        break_start = cur.get("break_seconds", 0) if state["current"] else 0
        active_b, aborted_b = countdown(BREAK_SECONDS, "Break", break_start)
        if aborted_b:
            print("Break skipped, moving on.")
        alarm_until_dismissed("Break over", "Time to get back to it.")

    record_completion(state, task_name, state["current"]["active_seconds"] if state["current"] else WORK_SECONDS,
                       took_break, False)
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
    if total_done < total_planned:
        print(f"  Shortfall will carry over to your next session.")


def main():
    parser = argparse.ArgumentParser(description="Pomodoro CLI tracker")
    parser.add_argument("--status", action="store_true", help="show today's progress and exit")
    args = parser.parse_args()

    today = date.today()
    state = load_day(today)

    if args.status:
        if not state:
            print("No pomodoro session started today yet.")
        else:
            print_summary(state)
        return

    try:
        if state is None:
            prev_day = most_recent_previous_day(today)
            carryover = compute_carryover(load_day(prev_day)) if prev_day else {}
            state = new_day_state(today, carryover)
            save_day(state)

        while True:
            done = sum(t["completed"] for t in state["tasks"])
            if state["current"] is None and done >= state["planned_total"]:
                print_summary(state)
                more = prompt_int(
                    f"You've hit today's target ({done}/{state['planned_total']}). "
                    "Add more pomodoros? [0 to stop] ", default=0)
                if more <= 0:
                    break
                state["planned_total"] += more
                for extra in allocate_tasks(more, {}):
                    existing = next((t for t in state["tasks"] if t["name"] == extra["name"]), None)
                    if existing:
                        existing["planned"] += extra["planned"]
                    else:
                        state["tasks"].append(extra)
                save_day(state)
                continue
            result = run_pomodoro(state)
            if result == "quit":
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
        if state is None:
            return

    print_summary(state)


if __name__ == "__main__":
    main()
