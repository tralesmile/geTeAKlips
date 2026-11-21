"""Post the oldest queued clip to TikTok, Instagram and YouTube via Upload-Post.

Run by .github/workflows/post.yml a few times a day.
"""
import json
import os
import sys
import uuid

import requests

from .common import WORK, load_config, queue_assets, run, QUEUE_TAG


def build_fields(meta, cfg):
    tags = cfg.get("hashtags", "")
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


def main():
    cfg = load_config()
    assets = queue_assets()
    names = {a["name"] for a in assets}
    clips = [a["name"] for a in assets if a["name"].endswith(".mp4")]
    if not clips:
        print("queue is empty, nothing to post")
        return
    name = clips[0]
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

    for n in (name, meta_name):
        if n in names:
            run(["gh", "release", "delete-asset", QUEUE_TAG, n, "-y"])
    print(f"posted {name}; {len(clips) - 1} clips left in queue")


if __name__ == "__main__":
    main()
