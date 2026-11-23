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
POSTS_LOG = ROOT / "state" / "posts.json"


def clip_age_hours(name):
    """Clip names start with the UTC time they were made: YYYYmmddHHMMSS."""
    from datetime import datetime, timezone
    try:
        made = datetime.strptime(name[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return (datetime.now(timezone.utc) - made).total_seconds() / 3600


def drop_stale(assets, cfg):
    """Delete queued clips older than max_clip_age_hours (fresh moments win)."""
    limit = cfg.get("max_clip_age_hours", 36)
    kept = []
    for a in assets:
        if a["name"].endswith((".mp4", ".json")) and clip_age_hours(a["name"]) > limit:
            run(["gh", "release", "delete-asset", QUEUE_TAG, a["name"], "-y"], check=False)
            print(f"dropped stale clip file {a['name']}")
        else:
            kept.append(a)
    return kept


def log_post(name, meta, resp, platforms):
    """Remember each post (for the daily stats job)."""
    try:
        request_id = resp.json().get("request_id")
    except ValueError:
        request_id = None
    try:
        log = json.loads(POSTS_LOG.read_text())
    except (OSError, ValueError):
        log = []
    log.append({"name": name, "request_id": request_id, "posted_at": int(time.time()),
                "streamer": meta.get("streamer"), "hook": meta.get("hook"),
                "title": meta.get("title"), "layout": meta.get("layout"),
                "source": meta.get("source"), "platforms": platforms,
                "links": post_links(resp.text)})
    POSTS_LOG.write_text(json.dumps(log[-400:], indent=1))


def build_fields(meta, cfg, platforms=None, profile=None):
    streamer = next((x for x in cfg["streamers"] if x["name"] == meta.get("streamer")), {})
    tags = " ".join(t for t in (streamer.get("hashtags", ""), cfg.get("hashtags", "")) if t)
    credit = meta.get("credit", "")
    paid = bool(streamer.get("paid_partnership"))
    disclosure = "Paid partnership. " if paid else ""
    caption = f"{disclosure}{meta['caption']} 🎥 {credit}\n{tags}".strip()
    title = meta["title"] or meta["hook"] or "GTA 6 moment"
    posting = cfg["posting"]
    fields = [("user", profile or streamer.get("profile") or posting.get("upload_post_user", "default")),
              # generic title = the X (Twitter) post text, max 280 characters
              ("title", f"{meta['caption'] or title} {tags}"[:280]),
              ("tiktok_title", caption[:2200]),
              ("instagram_title", caption[:2200]),
              ("youtube_title", (title[:90] + " #shorts")[:100]),
              ("youtube_description", f"{meta['caption']}\n\nCredit: {credit}\n{tags}"),
              ("privacyStatus", "public"),
              ("categoryId", "20")]  # 20 = Gaming
    cover = int(meta.get("cover_ms") or 500)          # peak frame as the cover
    fields += [("cover_timestamp", str(cover)), ("thumb_offset", str(cover))]
    comment = meta.get("comment") or f"Best {credit or 'GTA 6'} moment so far? 👀"
    fields.append(("first_comment", comment))
    if paid:  # platform "paid partnership" labels (campaign requires disclosure)
        fields += [("brand_content_toggle", "true"), ("hasPaidProductPlacement", "true")]
    fields += [("platform[]", p) for p in (platforms or posting.get("platforms", []))]
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
    camps = streamer.get("campaigns") or (
        [{"label": who, "url": streamer["campaign"]}] if streamer.get("campaign") else [])
    for c in camps[:3]:
        actions.append({"action": "view", "label": f"Submit: {c['label']}", "url": c["url"],
                        "clear": False})
    if any(c.get("note") for c in camps):
        lines += [""] + [f"• {c['label']}: {c['note']}" for c in camps if c.get("note")]
    for label, url in list(links.items())[:max(0, 3 - len(actions))]:
        actions.append({"action": "view", "label": f"Open {label}", "url": url})
    msg = {"topic": topic, "title": f"New {who} clip: {meta.get('title', '')[:50]}",
           "message": "\n".join(lines), "priority": 4, "tags": ["movie_camera"]}
    if actions:
        msg["actions"] = actions[:3]
    if camps:
        msg["click"] = camps[0]["url"]
    try:
        requests.post("https://ntfy.sh/", json=msg, timeout=20).raise_for_status()
    except requests.RequestException as e:
        print(f"notification failed: {e}")


def newest_first(name):
    stamp, index = name.split("_")[:2]
    return (-int(stamp), int(index))  # newest batch first, best clip of the batch first


def load_turns():
    try:
        data = json.loads(POSTING_STATE.read_text())
    except (OSError, ValueError):
        data = {}
    return data.get("profiles", {})


def save_turns(turns):
    POSTING_STATE.parent.mkdir(parents=True, exist_ok=True)
    POSTING_STATE.write_text(json.dumps({"profiles": turns}, indent=1))


def pick_clip(clips, names, last=None):
    """Take turns between this account's streamers; post each one's newest clip."""
    start = names.index(last) + 1 if last in names else 0
    for k in range(len(names)):
        who = names[(start + k) % len(names)]
        mine = [c for c in clips if clip_streamer(c) == who]
        if mine:
            return sorted(mine, key=newest_first)[0]
    return None


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


def profiles(cfg):
    """Posting accounts: {profile: {"platforms": [...], "times": [...], "streamers": [...]}}"""
    posting = cfg["posting"]
    out = {}
    for name, p in (posting.get("profiles") or {}).items():
        out[name] = {"platforms": p.get("platforms", posting.get("platforms", [])),
                     "times": p.get("times", posting.get("times", [])), "streamers": [],
                     "mirror": p.get("mirror"), "kind": p.get("kind", "clips")}
    if not out:  # old single-account config
        out[posting.get("upload_post_user", "default")] = {
            "platforms": posting.get("platforms", []), "times": posting.get("times", []),
            "streamers": []}
    first = next(k for k, v in out.items() if v["kind"] == "clips")
    for st in cfg["streamers"]:
        out.get(st.get("profile", first), out[first])["streamers"].append(st["name"])
    return out


def in_window(times, cfg, now=None):
    """True if now is within window_minutes after one of these times."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    if not times:
        return True
    tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
    now = now or datetime.now(tz)
    window = timedelta(minutes=cfg["posting"].get("window_minutes", 45))
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        for day in (-1, 0):
            slot = (now + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot <= now <= slot + window:
                return True
    return False


MIN_GAP_MINUTES = 75  # the backup trigger fires right after GitHub's: never post twice


def mirror(name, meta, cfg, spec):
    """Also post the clip to another profile's accounts (e.g. one shared Instagram
    and X). Not for campaigns, so no alert; a failure here never blocks anything."""
    if not spec or not spec.get("profile"):
        return
    plats = list(spec.get("platforms", []))
    for attempt in (plats, [p for p in plats if p != "x"]):
        if not attempt:
            return
        try:
            with open(WORK / name, "rb") as video:
                r = requests.post(
                    "https://api.upload-post.com/api/upload",
                    headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}",
                             "Idempotency-Key": str(uuid.uuid5(
                                 uuid.NAMESPACE_URL, "mirror" + name + ",".join(attempt)))},
                    data=build_fields(meta, cfg, attempt, spec["profile"]),
                    files={"video": (name, video, "video/mp4")}, timeout=600)
        except requests.RequestException as e:
            print(f"mirror to {spec['profile']} failed: {e}")
            return
        print(f"mirror to {spec['profile']} {attempt}:", r.status_code, r.text[:300])
        if r.status_code < 300 or "x" not in attempt:
            return


def all_failed(resp):
    """A 200 can still mean every platform failed (e.g. an account banned from posting)."""
    try:
        body = resp.json()
    except ValueError:
        return False
    results = body.get("results") or {}
    if body.get("status") == "failed":
        return True
    return bool(results) and all(isinstance(r, dict) and r.get("success") is False
                                 for r in results.values())


def post_one(profile, acct, cfg, assets, turns):
    """Post this account's next clip. Returns True if something was posted."""
    if acct.get("kind") == "memes":  # image meme account: make a fresh meme and post it
        from . import memes
        ok = memes.make_and_post(profile, acct, cfg)
        if ok:
            turns[profile] = {"last": "meme", "at": int(time.time())}
            save_turns(turns)
        return False  # nothing taken from the clip queue
    names = {a["name"] for a in assets}
    clips = [a["name"] for a in assets if a["name"].endswith(".mp4")]
    name = pick_clip(clips, acct["streamers"], turns.get(profile, {}).get("last"))
    if not name:
        print(f"[{profile}] no clips waiting for {', '.join(acct['streamers'])}")
        return False
    meta_name = name[:-4] + ".json"
    WORK.mkdir(exist_ok=True)
    run(["gh", "release", "download", QUEUE_TAG, "-p", name, "-D", WORK, "--clobber"])
    meta = {"caption": "", "title": "", "hook": "", "credit": ""}
    if meta_name in names:
        run(["gh", "release", "download", QUEUE_TAG, "-p", meta_name, "-D", WORK, "--clobber"])
        meta.update(json.loads((WORK / meta_name).read_text()))

    platforms = list(acct["platforms"])

    def upload(plats):
        with open(WORK / name, "rb") as video:
            r = requests.post(
                "https://api.upload-post.com/api/upload",
                headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}",
                         "Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                           name + ",".join(plats)))},
                data=build_fields(meta, cfg, plats, profile),
                files={"video": (name, video, "video/mp4")},
                timeout=600)
        print(f"[{profile}]", r.status_code, r.text[:2000])
        return r

    resp = upload(platforms)
    if resp.status_code >= 300 and "x" in platforms:
        platforms = [p for p in platforms if p != "x"]   # never let X block a post
        print("retrying without X")
        resp = upload(platforms)
    turns[profile] = {"last": clip_streamer(name), "at": int(time.time())}
    save_turns(turns)
    if resp.status_code >= 300 or all_failed(resp):
        print(f"[{profile}] upload failed; clip stays in the queue for the next slot")
        return False
    final = wait_for_links(resp, len(platforms))
    st = next((x for x in cfg["streamers"] if x["name"] == meta.get("streamer")), {})
    if st.get("notify", True):
        notify(meta, final, cfg)
    log_post(name, meta, resp, platforms)
    mirror(name, meta, cfg, acct.get("mirror"))
    for n in (name, meta_name):
        if n in names:
            run(["gh", "release", "delete-asset", QUEUE_TAG, n, "-y"])
    print(f"[{profile}] posted {name}")
    return True


def main():
    cfg = load_config()
    force = os.environ.get("FORCE_POST") == "1"
    only = os.environ.get("POST_PROFILE", "").strip()
    turns = load_turns()
    assets = drop_stale(queue_assets(), cfg)
    failed = False
    for profile, acct in profiles(cfg).items():
        if only and profile != only:
            continue
        if not force and not in_window(acct["times"], cfg):
            print(f"[{profile}] not this account's posting time; skipping")
            continue
        last_at = turns.get(profile, {}).get("at", 0)
        if not force and time.time() - last_at < MIN_GAP_MINUTES * 60:
            print(f"[{profile}] posted less than {MIN_GAP_MINUTES} min ago; skipping")
            continue
        try:
            if post_one(profile, acct, cfg, assets, turns):
                assets = queue_assets()
        except Exception as e:  # one account's problem shouldn't block the others
            print(f"[{profile}] error: {e}", file=sys.stderr)
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
