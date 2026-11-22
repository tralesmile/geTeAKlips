"""Find the moments viewers liked most.

Signals:
  * Twitch community clips: viewers clip what they love; each clip has a view
    count. We map each clip back to its spot in the full stream (VOD) and cut our
    own version with context. If the VOD isn't public, the clip itself is used.
  * YouTube "most replayed" heatmap peaks (e.g. Kai Cenat's YouTube streams).

Every moment is a dict:
  {"key", "url", "start", "end", "score", "hint", "source"}
where url/start/end is the stretch of video to search for the best cut.
"""
import json
import re
import sys
import time

from .common import run, ytdlp_base

PAD_BEFORE = 75   # clip creation time is just after the moment
PAD_AFTER = 15


def _json(url, extra=()):
    out = run(ytdlp_base() + list(extra) + ["-J", url], capture=True)
    return json.loads(out)


def twitch_login(url):
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url or "")
    return m.group(1).lower() if m and m.group(1).lower() != "videos" else None


def twitch_clips(login, ranges=("24hr", "7d"), n=25):
    """Top community clips (most viewed first). Falls back to the 7-day list."""
    for rng in ranges:
        try:
            data = _json(f"https://www.twitch.tv/{login}/clips?filter=clips&range={rng}",
                         ["--flat-playlist", "--playlist-end", str(n)])
        except Exception as e:
            print(f"twitch clips {login}/{rng} failed: {str(e)[:200]}", file=sys.stderr)
            continue
        clips = [e for e in data.get("entries") or [] if e.get("url") and e.get("timestamp")]
        if len(clips) >= 5:
            return sorted(clips, key=lambda e: -(e.get("view_count") or 0))
    return []


def twitch_vods(login, n=3):
    try:
        data = _json(f"https://www.twitch.tv/{login}/videos?filter=archive",
                     ["--flat-playlist", "--playlist-end", str(n)])
    except Exception:
        return []
    vods = []
    for e in data.get("entries") or []:
        link = e.get("url")
        if not link:
            continue
        try:
            info = _json(link, ["--no-playlist"])
        except Exception:
            continue
        if info.get("timestamp") and info.get("duration"):
            vods.append({"url": link, "id": str(info["id"]), "start": float(info["timestamp"]),
                         "duration": float(info["duration"])})
    return vods


def fresh_score(views, created_ts):
    """Views, discounted by age: a moment from 2 h ago beats an older one with a few
    more views (clip views are winner-take-most while a moment is fresh)."""
    age_h = max((time.time() - float(created_ts)) / 3600, 0)
    return float(views or 0) / (1 + age_h / 6)


def twitch_moments(login):
    clips = twitch_clips(login)
    if not clips:
        return []
    vods = twitch_vods(login)
    moments = []
    for c in clips:
        ts, dur = float(c["timestamp"]), float(c.get("duration") or 30)
        hint = f"viewers clipped this as \"{c.get('title') or ''}\" ({c.get('view_count') or 0} views)"
        vod = next((v for v in vods if v["start"] <= ts <= v["start"] + v["duration"] + 120), None)
        if vod:
            end_off = ts - vod["start"]
            moments.append({"key": f"tw:{c['id']}", "url": vod["url"], "vod": vod["id"],
                            "start": max(0.0, end_off - PAD_BEFORE - dur / 2),
                            "end": min(vod["duration"], end_off + PAD_AFTER),
                            "score": fresh_score(c.get("view_count"), ts), "hint": hint,
                            "created": ts, "source": "twitch-vod"})
        else:  # VOD not public: use the community clip itself
            moments.append({"key": f"tw:{c['id']}", "url": c["url"], "vod": f"clip{c['id']}",
                            "start": 0.0, "end": dur, "score": fresh_score(c.get("view_count"), ts),
                            "hint": hint, "created": ts, "source": "twitch-clip"})
    return moments


def youtube_moments(channel_url, n=3, max_age_days=4):
    """Most-replayed peaks of a YouTube channel's newest streams/videos."""
    try:
        data = _json(channel_url, ["--flat-playlist", "--playlist-end", str(n)])
    except Exception as e:
        print(f"youtube list failed: {str(e)[:200]}", file=sys.stderr)
        return []
    moments = []
    for e in data.get("entries") or []:
        link = e.get("url")
        if not link or not e.get("duration"):          # skip live/upcoming streams
            continue
        try:
            info = _json(link, ["--no-playlist"])
        except Exception as err:
            print(f"youtube video failed: {str(err)[:200]}", file=sys.stderr)
            continue
        if info.get("timestamp") and time.time() - info["timestamp"] > max_age_days * 86400:
            continue
        heat = sorted(info.get("heatmap") or [], key=lambda h: -h.get("value", 0))
        picked = []
        for h in heat:
            mid = (h["start_time"] + h["end_time"]) / 2
            if h.get("value", 0) < 0.35 or len(picked) >= 12:
                break
            if all(abs(mid - p) > 150 for p in picked):
                picked.append(mid)
                moments.append({"key": f"yt:{info['id']}:{int(mid)}", "url": link,
                                "vod": str(info["id"]), "start": max(0.0, mid - 50),
                                "end": min(float(info["duration"]), mid + 40),
                                "score": 1000 * h["value"],
                                "hint": "this is one of the most re-watched parts of the stream",
                                "source": "youtube-heatmap"})
    return moments


def popular_moments(streamer):
    """All popularity-ranked moments for a streamer, best first."""
    moments = []
    login = twitch_login(streamer.get("url")) or streamer.get("twitch")
    if login:
        moments += twitch_moments(login)
    for url in [streamer.get("url"), streamer.get("youtube")]:
        if url and "youtube.com" in url:
            moments += youtube_moments(url)
    return sorted(moments, key=lambda m: -m["score"])
