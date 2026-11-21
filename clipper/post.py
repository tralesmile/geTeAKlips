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


def build_fields(meta, cfg):
    streamer = next((x for x in cfg["streamers"] if x["name"] == meta.get("streamer")), {})
    tags = " ".join(t for t in (streamer.get("hashtags", ""), cfg.get("hashtags", "")) if t)
    credit = meta.get("credit", "")
    caption = f"{meta['caption']} 🎥 {credit}\n{tags}".strip()
    title = meta["title"] or meta["hook"] or "GTA 6 moment"
    posting = cfg["posting"]
    fields = [("user", posting["upload_post_user"]),
              ("title", title[:90]),
              ("tiktok_title", caption[:2200]),
              ("instagram_title", caption[:2200]),
              ("youtube_title", (title[:90] + " #shorts")[:100]),
              ("youtube_description", f"{meta['caption']}\n\nCredit: {credit}\n{tags}"),
              ("privacyStatus", "public"),
              ("categoryId", "20")]  # 20 = Gaming
    fields += [("platform[]", p) for p in posting["platforms"]]
    return fields


def notify(meta, response_text):
    """Push a phone notification (ntfy app) with the post links to submit."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    links = sorted(set(re.findall(r"https://[^\s\"'\\]+", response_text)))
    body = (f"Submit to the {meta.get('credit') or meta.get('streamer')} campaign "
            f"within 30 min:\n" + ("\n".join(links) or "check your profiles for the new post"))
    try:
        requests.post("https://ntfy.sh/", json={
            "topic": topic, "title": f"New clip posted: {meta.get('title', '')[:60]}",
            "message": body, "priority": 4, "tags": ["movie_camera"]},
            timeout=20).raise_for_status()
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
        links = re.findall(r"https://[^\s\"']*(?:instagram\.com|youtube\.com|youtu\.be|tiktok\.com)[^\s\"']*", r.text)
        if len(set(links)) >= platforms or '"completed"' in r.text or '"failed"' in r.text:
            return r.text
    return text


MIN_GAP_MINUTES = 75  # the backup trigger fires right after GitHub's: never post twice


def posted_recently():
    if not POSTING_STATE.exists():
        return False
    at = json.loads(POSTING_STATE.read_text()).get("at", 0)
    return time.time() - at < MIN_GAP_MINUTES * 60


def main():
    cfg = load_config()
    if posted_recently() and os.environ.get("FORCE_POST") != "1":
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

    with open(WORK / name, "rb") as video:
        resp = requests.post(
            "https://api.upload-post.com/api/upload",
            headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}",
                     "Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL, name))},
            data=build_fields(meta, cfg),
            files={"video": (name, video, "video/mp4")},
            timeout=600)
    print(resp.status_code, resp.text[:2000])
    if resp.status_code >= 300:
        sys.exit("upload failed; clip stays in the queue for the next run")

    notify(meta, wait_for_links(resp, len(cfg["posting"]["platforms"])))
    for n in (name, meta_name):
        if n in names:
            run(["gh", "release", "delete-asset", QUEUE_TAG, n, "-y"])
    print(f"posted {name}; {len(clips) - 1} clips left in queue")


if __name__ == "__main__":
    main()
