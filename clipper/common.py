"""Shared helpers: config, state file, shell commands, GitHub release queue."""
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "processed.json"
WORK = ROOT / "work"
QUEUE_TAG = "queue"


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def run(cmd, capture=False, check=True):
    print("+", " ".join(str(c) for c in cmd)[:300], flush=True)
    res = subprocess.run([str(c) for c in cmd], text=True,
                         capture_output=capture, check=False)
    if check and res.returncode != 0:
        err = res.stderr[-2000:] if capture and res.stderr else ""
        raise RuntimeError(f"command failed ({res.returncode}): {cmd[0]}\n{err}")
    return res.stdout if capture else res.returncode


def ytdlp_base():
    """yt-dlp command with optional cookies (needed for YouTube from CI)."""
    cmd = ["yt-dlp", "--no-progress", "--no-warnings"]
    cookies = os.environ.get("YTDLP_COOKIES", "").strip()
    if cookies:
        path = WORK / "cookies.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cookies + "\n")
        cmd += ["--cookies", str(path)]
    return cmd


# ── Queue = assets on a GitHub release tagged "queue" ──────────────────

def ensure_queue_release():
    res =subprocess.run(["gh", "release", "view", QUEUE_TAG],
                         capture_output=True, text=True)
    if res.returncode != 0:
        run(["gh", "release", "create", QUEUE_TAG, "--title", "Clip queue",
             "--notes", "Clips waiting to be posted. Managed automatically.",
             "--prerelease"])


def queue_assets():
    """Return queue assets as a list of dicts with name/id, oldest first."""
    repo = os.environ["GITHUB_REPOSITORY"]
    out = run(["gh", "api", f"repos/{repo}/releases/tags/{QUEUE_TAG}"],
              capture=True, check=False)
    if not out:
        return []
    assets = json.loads(out).get("assets", [])
    return sorted(assets, key=lambda a: a["name"])


def clip_streamer(name):
    """Clip files are named <stamp>_<index>_<streamer>_<video id>_<start>.mp4"""
    parts = name.split("_")
    return parts[2] if len(parts) > 2 else ""


def clip_age_hours(name):
    """Clip names start with the UTC time they were made: YYYYmmddHHMMSS."""
    from datetime import datetime, timezone
    try:
        made = datetime.strptime(name[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return (datetime.now(timezone.utc) - made).total_seconds() / 3600


def queued_clip_count(streamer=None, max_age_hours=None):
    """Clips waiting for this streamer. With max_age_hours, clips that the
    poster will drop as stale don't count (so fresh ones get made in time)."""
    return sum(1 for a in queue_assets() if a["name"].endswith(".mp4")
               and (streamer is None or clip_streamer(a["name"]) == streamer)
               and (max_age_hours is None or clip_age_hours(a["name"]) <= max_age_hours))
