import argparse
import json
import os
import subprocess
import sys
import time

import psutil

CRASH_WINDOW = 15 * 60
MAX_CRASHES = 3


def say(msg):
    try:
        print(f"[durable] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def has_unfinished_jobs(root):
    jobs = os.path.join(root, "jobs")
    if not os.path.isdir(jobs):
        return False
    for name in os.listdir(jobs):
        if name.endswith(".json") and read_json(os.path.join(jobs, name), {}).get("state") in ("queued", "running"):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent", type=int, required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--argv", required=True)
    a = ap.parse_args()

    try:
        parent = psutil.Process(a.parent)
        while parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE:
            time.sleep(1)
    except psutil.NoSuchProcess:
        pass
    time.sleep(1)

    if read_json(os.path.join(a.root, "clean_exit.json"), {}).get("pid") == a.parent:
        return
    if not has_unfinished_jobs(a.root):
        return

    crashes_path = os.path.join(a.root, "crashes.json")
    now = time.time()
    crashes = [t for t in read_json(crashes_path, []) if now - t < CRASH_WINDOW] + [now]
    with open(crashes_path, "w") as f:
        json.dump(crashes, f)
    if len(crashes) > MAX_CRASHES:
        say(f"ComfyUI crashed {len(crashes)} times in 15 minutes, not restarting it again. Your queue is saved and will resume on the next start.")
        return

    env = dict(os.environ, DURABLE_RELAUNCHED="1")
    cmd = [sys.executable] + json.loads(a.argv)
    log_path = os.path.join(a.root, "relaunch.log")
    if os.name == "nt":
        proc = subprocess.Popen(cmd, cwd=a.cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        where = "in a new window"
    else:
        log = open(log_path, "ab")
        proc = subprocess.Popen(cmd, cwd=a.cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        where = f"in the background, log: {log_path}"
    say(f"ComfyUI exited unexpectedly with jobs in progress. Restarted it {where} (pid {proc.pid})")


if __name__ == "__main__":
    main()
