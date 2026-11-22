"""Post the oldest queued clip to TikTok, Instagram and YouTube via Upload-Post.

Run by .github/workflows/post.yml a few times a day.
"""
import json
import os
import re
import sys
import time
import uuid

import requests

from .common import (ROOT, WORK, QUEUE_TAG, clip_streamer, load_config,
                     queue_assets, run)

POSTING_STATE = ROOT / "state" / "posting.json"


def build_fields(meta, cfg, platforms=None):
    streamer = next((x for x in cfg["streamers"] if x["name"] == meta.get("streamer")), {})
    tags = " ".join(t for t in (streamer.get("hashtags", ""), cfg.get("hashtags", "")) if t)
    credit = meta.get("credit", "")
    caption = f"{meta['caption']} 🎥 {credit}\n{tags}".strip()
    title = meta["title"] or meta["hook"] or "GTA 6 moment"
    posting = cfg["posting"]
    fields = [("user", posting["upload_post_user"]),
              # generic title = the X (Twitter) post text, max 280 characters
              ("title", f"{meta['caption'] or title} {tags}"[:280]),
              ("tiktok_title", caption[:2200]),
              ("instagram_title", caption[:2200]),
              ("youtube_title", (title[:90] + " #shorts")[:100]),
              ("youtube_description", f"{meta['caption']}\n\nCredit: {credit}\n{tags}"),
              ("privacyStatus", "public"),
              ("categoryId", "20")]  # 20 = Gaming
    fields += [("platform[]", p) for p in (platforms or posting["platforms"])]
    return fields


PLATFORM_NAMES = [("tiktok.com", "TikTok"), ("instagram.com", "Instagram"),
                  ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
                  ("x.com", "X"), ("twitter.com", "X")]


def post_links(response_text):
    """Real post links (one per platform) from Upload-Post's response."""
    found = {}
    for url in re.findall(r"https://[^\s\"'\\,]+", response_text):
        if "upload-post.com" in url:
            continue
        for domain, label in PLATFORM_NAMES:
            if domain in url and label not in found:
                found[label] = url
    return found


def notify(meta, response_text, cfg=None):
    """Phone alert (ntfy app): one link per line + a button that opens the
    streamer's campaign page, ready to paste."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    streamer = next((x for x in (cfg or {}).get("streamers", [])
                     if x["name"] == meta.get("streamer")), {})
    who = meta.get("credit") or meta.get("streamer")
    links = post_links(response_text)
    lines = [f"Submit to the {who} campaign within 30 min.",
             "Long-press a link to copy it, then tap Submit on Whop:", ""]
    lines += [f"{label}: {url}" for label, url in links.items()] or [
        "Links not ready yet: copy them from your profiles."]
    actions = []
    if streamer.get("campaign"):
        actions.append({"action": "view", "label": f"Submit on Whop ({who})",
                        "url": streamer["campaign"], "clear": False})
    for label, url in list(links.items())[:2]:
        actions.append({"action": "view", "label": f"Open {label}", "url": url})
    msg = {"topic": topic, "title": f"New {who} clip: {meta.get('title', '')[:50]}",
           "message": "\n".join(lines), "priority": 4, "tags": ["movie_camera"]}
    if actions:
        msg["actions"] = actions[:3]
    if streamer.get("campaign"):
        msg["click"] = streamer["campaign"]
    try:
        requests.post("https://ntfy.sh/", json=msg, timeout=20).raise_for_status()
    except requests.RequestException as e:
        print(f"notification failed: {e}")


def newest_first(name):
    stamp, index = name.split("_")[:2]
    return (-int(stamp), int(index))  # newest batch first, best clip of the batch first


def pick_clip(clips, cfg):
    """Take turns between streamers; post each streamer's newest clip."""
    order = [s["name"] for s in cfg["streamers"]]
    last = None
    if POSTING_STATE.exists():
        last = json.loads(POSTING_STATE.read_text()).get("last")
    start = order.index(last) + 1 if last in order else 0
    for k in range(len(order)):
        who = order[(start + k) % len(order)]
        mine = [c for c in clips if clip_streamer(c) == who]
        if mine:
            choice = sorted(mine, key=newest_first)[0]
            break
    else:  # clips from streamers no longer in config
        choice = sorted(clips, key=newest_first)[0]
    POSTING_STATE.parent.mkdir(parents=True, exist_ok=True)
    POSTING_STATE.write_text(json.dumps({"last": clip_streamer(choice), "at": int(time.time())}))
    return choice


def wait_for_links(resp, platforms=3, max_wait=600):
    """Background uploads: poll Upload-Post until post URLs appear (max 10 min)."""
    text = resp.text
    try:
        request_id = resp.json().get("request_id")
    except ValueError:
        request_id = None
    if not request_id:
        return text
    headers = {"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}"}
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(30)
        try:
            r = requests.get("https://api.upload-post.com/api/uploadposts/status",
                             params={"request_id": request_id}, headers=headers, timeout=30)
        except requests.RequestException:
            continue
        print("status:", r.text[:800])
        links = re.findall(r"https://[^\s\"']*(?:instagram\.com|youtube\.com|youtu\.be|tiktok\.com|x\.com|twitter\.com)[^\s\"']*", r.text)
        try:
            job = r.json()
        except ValueError:
            job = {}
        # the job's own status, not a platform's: one platform "completed"
        # while Instagram is still processing must not stop the wait
        job_done = job.get("status") in ("completed", "failed", "partial", "error", "done")
        if len(set(links)) >= platforms or job_done:
            return r.text
    return text


def in_posting_window(cfg, now=None):
    """True if now is within window_minutes after one of the posting times."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    posting = cfg["posting"]
    times = posting.get("times")
    if not times:
        return True
    tz = ZoneInfo(posting.get("timezone", "UTC"))
    now = now or datetime.now(tz)
    window = timedelta(minutes=posting.get("window_minutes", 45))
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        for day in (-1, 0):
            slot = (now + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot <= now <= slot + window:
                return True
    return False


MIN_GAP_MINUTES = 75  # the backup trigger fires right after GitHub's: never post twice


def posted_recently():
    if not POSTING_STATE.exists():
        return False
    at = json.loads(POSTING_STATE.read_text()).get("at", 0)
    return time.time() - at < MIN_GAP_MINUTES * 60


def main():
    cfg = load_config()
    force = os.environ.get("FORCE_POST") == "1"
    if not force and not in_posting_window(cfg):
        print("outside the posting times (late or extra run); skipping")
        return
    if posted_recently() and not force:
        print("a clip was posted less than 75 minutes ago; skipping this run")
        return
    assets = queue_assets()
    names = {a["name"] for a in assets}
    clips = [a["name"] for a in assets if a["name"].endswith(".mp4")]
    if not clips:
        print("queue is empty, nothing to post")
        return
    name = pick_clip(clips, cfg)
    meta_name = name[:-4] + ".json"

    WORK.mkdir(exist_ok=True)
    run(["gh", "release", "download", QUEUE_TAG, "-p", name, "-D", WORK, "--clobber"])
    meta = {"caption": "", "title": "", "hook": "", "credit": ""}
    if meta_name in names:
        run(["gh", "release", "download", QUEUE_TAG, "-p", meta_name, "-D", WORK, "--clobber"])
        meta.update(json.loads((WORK / meta_name).read_text()))

    platforms = list(cfg["posting"]["platforms"])

    def upload(plats):
        with open(WORK / name, "rb") as video:
            r = requests.post(
                "https://api.upload-post.com/api/upload",
                headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}",
                         "Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                           name + ",".join(plats)))},
                data=build_fields(meta, cfg, plats),
                files={"video": (name, video, "video/mp4")},
                timeout=600)
        print(r.status_code, r.text[:2000])
        return r

    resp = upload(platforms)
    if resp.status_code >= 300 and "x" in platforms:
        # never let X block a post: retry on the other platforms
        platforms = [p for p in platforms if p != "x"]
        print("retrying without X")
        resp = upload(platforms)
    if resp.status_code >= 300:
        sys.exit("upload failed; clip stays in the queue for the next run")

    notify(meta, wait_for_links(resp, len(platforms)), cfg)
    for n in (name, meta_name):
        if n in names:
            run(["gh", "release", "delete-asset", QUEUE_TAG, n, "-y"])
    print(f"posted {name}; {len(clips) - 1} clips left in queue")


if __name__ == "__main__":
    main()
