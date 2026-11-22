"""Re-render queued clips with the current editor, keeping the same cut, text and
queue position (files are replaced in place).

Input env REMAKE: JSON list of {"name": "<queued clip name without .mp4>"}.
The clip's .json in the queue supplies the cut (vod, start, end, peak, hook...).
"""
import json
import os

from .common import QUEUE_TAG, WORK, load_config, run
from .make import render_clip


def main():
    cfg = load_config()
    streamers = {s["name"]: s for s in cfg["streamers"]}
    WORK.mkdir(exist_ok=True)
    for item in json.loads(os.environ["REMAKE"]):
        name = item["name"]
        run(["gh", "release", "download", QUEUE_TAG, "-p", f"{name}.json", "-D", WORK,
             "--clobber"])
        meta = json.loads((WORK / f"{name}.json").read_text())
        st = streamers[meta["streamer"]]
        cut = {k: meta[k] for k in ("start", "end", "peak", "hook", "title", "caption")
               if k in meta}
        cut.update({k: meta[k] for k in ("source", "popularity") if k in meta})
        ok = render_clip(st, meta["vod"], cut, WORK, name, cfg)
        print(f"{name}: {'remade' if ok else 'FAILED (old version kept)'}")


if __name__ == "__main__":
    main()
