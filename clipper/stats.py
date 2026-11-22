"""Daily stats: views/likes per posted clip (via Upload-Post), a phone report,
and state/winners.json (best hooks) that the clip picker learns from.

Run by .github/workflows/stats.yml.
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .common import ROOT, load_config

POSTS_LOG = ROOT / "state" / "posts.json"
STATS = ROOT / "state" / "stats.json"
WINNERS = ROOT / "state" / "winners.json"
PLATFORMS = ("tiktok", "instagram", "youtube", "x", "twitter")
METRICS = {"views": ("views", "view_count", "video_views", "plays", "play_count",
                     "impressions"),
           "likes": ("likes", "like_count"),
           "comments": ("comments", "comment_count"),
           "shares": ("shares", "share_count", "reposts", "retweets")}


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _find_metric(node, names):
    """First numeric value under any of `names`, searching nested dicts/lists."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in names and _num(v) is not None:
                return _num(v)
        for v in node.values():
            r = _find_metric(v, names)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_metric(v, names)
            if r is not None:
                return r
    return None


def _platform_nodes(node, out=None):
    """Map platform name -> the part of the response about that platform."""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            key = str(k).lower()
            if key in PLATFORMS and isinstance(v, (dict, list)):
                out.setdefault("x" if key == "twitter" else key, v)
            elif isinstance(v, (dict, list)):
                _platform_nodes(v, out)
            if key == "platform" and isinstance(v, str) and v.lower() in PLATFORMS:
                out.setdefault(v.lower(), node)
    elif isinstance(node, list):
        for v in node:
            _platform_nodes(v, out)
    return out


def fetch(request_id):
    r = requests.get(f"https://api.upload-post.com/api/uploadposts/post-analytics/{request_id}",
                     headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}"},
                     timeout=60)
    if r.status_code >= 300:
        print(f"{request_id}: {r.status_code} {r.text[:200]}")
        return None
    data = r.json()
    per = {}
    for plat, node in _platform_nodes(data).items():
        per[plat] = {m: _find_metric(node, names) or 0 for m, names in METRICS.items()}
    if not per:  # unknown shape: keep totals so nothing is lost
        per["all"] = {m: _find_metric(data, names) or 0 for m, names in METRICS.items()}
        print(f"{request_id}: unrecognised analytics shape: {json.dumps(data)[:400]}")
    return per


def main():
    cfg = load_config()
    try:
        posts = json.loads(POSTS_LOG.read_text())
    except (OSError, ValueError):
        posts = []
    week_ago = time.time() - 7 * 86400
    rows = []
    for p in posts:
        if not p.get("request_id") or p.get("posted_at", 0) < week_ago:
            continue
        per = fetch(p["request_id"])
        if per is None:
            continue
        total = {m: sum(v.get(m, 0) for v in per.values()) for m in METRICS}
        rows.append(dict(p, per_platform=per, **total))
        time.sleep(0.5)
    STATS.write_text(json.dumps({"updated": int(time.time()), "posts": rows}, indent=1))

    # winners: hooks that got the most views (the picker copies their style)
    ranked = sorted(rows, key=lambda r: -r["views"])
    WINNERS.write_text(json.dumps([{"hook": r.get("hook"), "title": r.get("title"),
                                    "streamer": r.get("streamer"), "views": r["views"]}
                                   for r in ranked if r["views"] > 0][:8], indent=1))

    # phone report
    tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
    day_ago = time.time() - 86400
    today = [r for r in rows if r["posted_at"] >= day_ago]
    by_streamer, by_hour, by_platform = defaultdict(list), defaultdict(list), defaultdict(int)
    for r in rows:
        by_streamer[r.get("streamer")].append(r["views"])
        by_hour[datetime.fromtimestamp(r["posted_at"], tz).strftime("%H:%M")].append(r["views"])
        for plat, v in r["per_platform"].items():
            by_platform[plat] += v.get("views", 0)
    avg = lambda xs: int(sum(xs) / len(xs)) if xs else 0  # noqa: E731
    lines = [f"Last 24h: {len(today)} posts, {sum(r['views'] for r in today):,} views, "
             f"{sum(r['likes'] for r in today):,} likes",
             f"Last 7 days: {len(rows)} posts, {sum(r['views'] for r in rows):,} views", ""]
    if ranked:
        lines.append("🏆 Top clips this week:")
        lines += [f"{i + 1}. {r.get('hook') or r.get('title')} — {r['views']:,} views"
                  for i, r in enumerate(ranked[:3])]
        lines.append("")
    if by_platform:
        lines.append("By platform: " + ", ".join(f"{k} {v:,}" for k, v in
                                                 sorted(by_platform.items(), key=lambda x: -x[1])))
    if by_streamer:
        lines.append("Avg views by streamer: " + ", ".join(
            f"{k} {avg(v):,}" for k, v in sorted(by_streamer.items(), key=lambda x: -avg(x[1]))))
    if len(by_hour) > 1:
        best = max(by_hour.items(), key=lambda x: avg(x[1]))
        lines.append(f"Best posting time so far: {best[0]} ({avg(best[1]):,} avg views)")
    body = "\n".join(lines)
    print(body)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        requests.post("https://ntfy.sh/", json={"topic": topic, "title": "📊 Daily clip stats",
                                                "message": body, "tags": ["chart_with_upwards_trend"]},
                      timeout=20)


if __name__ == "__main__":
    main()
