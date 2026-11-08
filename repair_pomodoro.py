#!/usr/bin/env python3
"""Repairs ~/.pomodoro/*.json day files affected by the duplicate-task bug.

The bug: when two tasks in a day file shared the same name, a completed
pomodoro could get credited to the wrong one, making a task's visible
"completed" count wrong even though the pomodoro itself was correctly
logged in completed_pomodoros.

This script trusts completed_pomodoros (every logged pomodoro, with task
name + timestamp) as the source of truth, recomputes each task's
completed count from it, and merges any duplicate-named tasks along the
way. Nothing about your logged history is changed — only the per-task
completed counters are corrected to match it.

Usage:
    python3 repair_pomodoro.py            # dry run: show what would change
    python3 repair_pomodoro.py --apply    # write the fixes (backs up first)
"""

import argparse
import copy
import json
import os
from collections import Counter

DATA_DIR = os.path.expanduser("~/.pomodoro")


def consolidate_tasks(tasks):
    """Merges tasks that share a name into one entry (planned summed),
    keeping first-seen order."""
    merged = []
    index_by_name = {}
    for t in tasks:
        if t["name"] in index_by_name:
            merged[index_by_name[t["name"]]]["planned"] += t["planned"]
        else:
            index_by_name[t["name"]] = len(merged)
            merged.append({"name": t["name"], "planned": t["planned"], "completed": 0})
    return merged


def repair_state(state):
    """Returns (new_state, changed, report_lines)."""
    log = state.get("completed_pomodoros", [])
    true_counts = Counter(p["task"] for p in log)

    tasks = consolidate_tasks(state.get("tasks", []))
    seen_names = {t["name"] for t in tasks}

    # A task removed via edit_tasks but with completed pomodoros still in
    # the log wouldn't appear in `tasks` at all — surface it rather than
    # silently dropping those pomodoros from the count.
    for name in true_counts:
        if name not in seen_names:
            tasks.append({"name": name, "planned": 0, "completed": 0})
            seen_names.add(name)

    report = []
    # Merging duplicate-named tasks is a change on its own, even when the
    # counts happen to come out the same.
    original_tasks = state.get("tasks", [])
    changed = len(tasks) != len(original_tasks)
    if len({t["name"] for t in original_tasks}) != len(original_tasks):
        report.append("    merged duplicate task names")
    old_by_name = {}
    for t in original_tasks:
        old_by_name[t["name"]] = old_by_name.get(t["name"], 0) + t["completed"]

    for t in tasks:
        correct = true_counts.get(t["name"], 0)
        old = old_by_name.get(t["name"])
        if old != correct:
            changed = True
            report.append(f"    {t['name']!r}: completed {old} -> {correct}")
        t["completed"] = correct

    new_state = dict(state)
    new_state["tasks"] = tasks

    total_logged = len(log)
    total_credited = sum(t["completed"] for t in tasks)
    if total_credited != total_logged:
        report.append(f"    WARNING: {total_credited} credited vs {total_logged} logged pomodoros — mismatch remains")

    return new_state, changed, report


def find_overrun_tasks(tasks):
    """Tasks with a real target (planned > 0) that ended up with more
    completed pomodoros than planned."""
    return [t for t in tasks if t["planned"] > 0 and t["completed"] > t["planned"]]


def entries_for_task(log, name):
    """(log_index, entry) pairs for a task name, oldest first."""
    return [(i, e) for i, e in enumerate(log) if e["task"] == name]


def choose_target_task(candidate_names):
    print("Move to which task?")
    for i, name in enumerate(candidate_names, 1):
        print(f"  {i}. {name}")
    print(f"  {len(candidate_names) + 1}. <type a new task name>")
    raw = input("Choice: ").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(candidate_names):
            return candidate_names[idx - 1]
    new_name = input("New task name: ").strip()
    return new_name or None


def reassign_interactive(fname, working):
    """Lets the user move individual logged pomodoros from an over-completed
    task to another task, by editing that entry's task field in the log.
    Mutates and returns `working`."""
    preview, _, _ = repair_state(copy.deepcopy(working))
    overruns = find_overrun_tasks(preview["tasks"])
    if not overruns:
        return working

    log = working["completed_pomodoros"]
    print(f"\n{fname}: tasks with more completed than planned:")
    for t in overruns:
        name = t["name"]
        excess = t["completed"] - t["planned"]
        entries = entries_for_task(log, name)
        print(f"\n  '{name}': {t['completed']}/{t['planned']} completed ({excess} over)")
        choice = input(f"  Move some of these to another task? [y/N] ").strip().lower()
        if choice != "y":
            continue

        print(f"  Logged pomodoros for '{name}':")
        for n, (_, e) in enumerate(entries, 1):
            print(f"    {n}. {e['end']}")
        raw = input("  Which ones to move? (comma-separated numbers, 'all', or Enter to skip): ").strip()
        if not raw:
            continue
        if raw.lower() == "all":
            chosen = [i for i, _ in entries]
        else:
            chosen = []
            for piece in raw.split(","):
                piece = piece.strip()
                if piece.isdigit() and 1 <= int(piece) <= len(entries):
                    chosen.append(entries[int(piece) - 1][0])
        if not chosen:
            continue

        other_names = [t["name"] for t in preview["tasks"] if t["name"] != name]
        target = choose_target_task(other_names)
        if not target:
            print("  No target given, skipping.")
            continue
        for i in chosen:
            log[i]["task"] = target
        print(f"  Moved {len(chosen)} pomodoro(s) from '{name}' to '{target}'.")

    return working


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                         help="write the fixes to disk (default is dry-run / report only)")
    parser.add_argument("--reassign", action="store_true",
                         help="interactively move individual logged pomodoros between tasks "
                              "for any task that ended up with more completed than planned")
    args = parser.parse_args()
    if args.reassign and not args.apply:
        parser.error("--reassign needs --apply, otherwise the moves you pick are thrown away")

    if not os.path.isdir(DATA_DIR):
        print(f"No {DATA_DIR} directory found — nothing to repair.")
        return

    files = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".json"))
    if not files:
        print(f"No day files found in {DATA_DIR}.")
        return

    any_changes = False
    for fname in files:
        path = os.path.join(DATA_DIR, fname)
        try:
            with open(path) as f:
                original = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"{fname}: skipped, couldn't read ({e})")
            continue

        working = copy.deepcopy(original)
        if args.reassign:
            working = reassign_interactive(fname, working)

        new_state, changed, report = repair_state(working)
        if not changed:
            continue

        any_changes = True
        print(f"{fname}:")
        for line in report:
            print(line)

        if args.apply:
            backup_path = path + ".bak"
            if not os.path.exists(backup_path):
                with open(backup_path, "w") as f:
                    json.dump(original, f, indent=2)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(new_state, f, indent=2)
            os.replace(tmp, path)
            print(f"    fixed (backup saved to {os.path.basename(backup_path)})")

    if not any_changes:
        print("Everything checks out — no drift found in any day file.")
    elif not args.apply:
        print("\nDry run only — nothing was written. Re-run with --apply to fix these.")


if __name__ == "__main__":
    main()
