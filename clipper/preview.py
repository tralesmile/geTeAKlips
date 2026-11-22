"""Render test clips with the real pipeline and put them on the 'preview' release
(not the posting queue). Used to check editing/captions before changes go live.

Input (env PREVIEW_CLIPS): lines of "<streamer> <video url> <start sec> <end sec> [peak sec]".
"""
import json
import os
import subprocess
from pathlib import Path

from . import edit
from .common import WORK, load_config, run
from .make import clip_words, download_section

TAG = "preview"


def main():
    cfg = load_config()
    streamers = {s["name"]: s for s in cfg["streamers"]}
    if subprocess.run(["gh", "release", "view", TAG], capture_output=True).returncode:
        run(["gh", "release", "create", TAG, "--title", "Preview clips",
             "--notes", "Test renders, not posted.", "--prerelease"])
    WORK.mkdir(exist_ok=True)
    for i, line in enumerate(os.environ["PREVIEW_CLIPS"].strip().splitlines()):
        parts = line.split()
        name, url, start, end = parts[0], parts[1], float(parts[2]), float(parts[3])
        peak = float(parts[4]) - start if len(parts) > 4 else None
        st = streamers.get(name, {"name": name, "credit": name})
        raw = download_section(url, start, end, WORK / f"prev{i}.mp4")
        words = clip_words(raw, cfg, st)
        out = WORK / f"preview_{i}_{name}.mp4"
        _, layout = edit.render(raw, out, words, f"{st.get('credit', name)} test clip",
                                st.get("credit", name), peak=peak, facecam=st.get("facecam"),
                                streamer=name)
        info = WORK / f"preview_{i}_{name}.json"
        info.write_text(json.dumps({"layout": layout, "words": [list(w) for w in words]}, indent=1))
        src = WORK / f"preview_{i}_{name}_source.mp4"
        raw.rename(src)
        run(["gh", "release", "upload", TAG, out, info, src, "--clobber"])


if __name__ == "__main__":
    main()
