"""Twice-daily health report sent to your phone (ntfy).

Run by .github/workflows/health-check.yml.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from .common import load_state, queue_assets, run

HOURS = 13


def recent_runs(repo, since):
    out = run(["gh", "api", f"repos/{repo}/actions/runs?per_page=100&created=>={since}"],
              capture=True, check=False)
    runs = json.loads(out).get("workflow_runs", []) if out else []
    return [r for r in runs if r["name"] != "Health check"]


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    since_dt = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    runs = recent_runs(repo, since)
    by_name = {}
    for r in runs:
        stats = by_name.setdefault(r["name"], {"ok": 0, "failed": [], "running": 0})
        if r["status"] != "completed":
            stats["running"] += 1
        elif r["conclusion"] == "success":
            stats["ok"] += 1
        else:
            stats["failed"].append(r["html_url"])

    queued = sum(1 for a in queue_assets() if a["name"].endswith(".mp4"))

    state = load_state()
    cutoff = since_dt.strftime("%Y-%m-%d %H:%M")
    recent = [p for p in state.get("processed", []) if p.get("at", "") >= cutoff]
    made = sum(int(p["result"].split(":")[1]) for p in recent
               if str(p.get("result", "")).startswith("ok:"))
    errors = [f"{p['streamer']}: {p['result'][6:90]}" for p in recent
              if str(p.get("result", "")).startswith("error")]

    problems, lines = [], []
    make, post = by_name.get("Make clips", {}), by_name.get("Post clip", {})
    lines.append(f"🎬 Clips made: {made}   📦 In queue: {queued}")
    lines.append(f"📤 Posts OK: {post.get('ok', 0)}   🛠 Clip runs OK: {make.get('ok', 0)}")
    for name, stats in by_name.items():
        for url in stats["failed"]:
            problems.append(f"❌ {name} failed: {url}")
    problems += [f"⚠️ {e}" for e in errors]
    if queued == 0:
        problems.append("⚠️ Queue is empty: nothing left to post")
    if not runs:
        problems.append("⚠️ No workflow ran in the last 13h (are Actions enabled?)")

    title = "✅ Clip bot healthy" if not problems else f"⚠️ Clip bot: {len(problems)} problem(s)"
    body = "\n".join(lines + problems[:8])
    if problems:
        body += "\n👉 Send this to Claude to fix."
    print(title)
    print(body)

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        requests.post("https://ntfy.sh/", json={
            "topic": topic, "title": title, "message": body, "tags": ["robot"],
            "priority": 4 if problems else 3}, timeout=20).raise_for_status()


if __name__ == "__main__":
    main()
