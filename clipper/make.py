"""Find new VODs, cut the best moments into vertical clips, push them to the queue.

Run by .github/workflows/make-clips.yml.
"""
import json
import shutil
import sys
import time
from pathlib import Path

from . import analyze, render
from .common import (WORK, ensure_queue_release, load_config, load_state,
                     queued_clip_count, run, save_state, ytdlp_base)


def list_vods(url, n):
    out = run(ytdlp_base() + ["--flat-playlist", "-J", "--playlist-end", n, url],
              capture=True)
    entries = json.loads(out).get("entries") or []
    vods = []
    for e in entries:
        vid = e.get("id")
        link = e.get("url") or e.get("webpage_url")
        if vid and link:
            vods.append({"id": str(vid), "url": link, "title": e.get("title", "")})
    return vods


def vod_duration(url):
    out = run(ytdlp_base() + ["-J", "--no-playlist", url], capture=True)
    info = json.loads(out)
    if info.get("is_live"):
        return None
    return float(info.get("duration") or 0)


def download_audio(url, folder):
    run(ytdlp_base() + ["-f", "bestaudio/worst", "-o", str(folder / "audio.%(ext)s"), url])
    return next(folder.glob("audio.*"))


def download_section(url, start, end, dst):
    run(ytdlp_base() + [
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--download-sections", f"*{start:.1f}-{end:.1f}",
        "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
        "-o", str(dst), url])
    return dst


def clip_words(path, cfg):
    model = analyze.whisper_model(cfg.get("whisper_model", "base"))
    segments, _ = model.transcribe(str(path), language=cfg.get("language"),
                                   word_timestamps=True, beam_size=1)
    return [(w.start, w.end, w.word) for s in segments for w in (s.words or [])]


def media_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", path], capture=True)
    return float(out.strip())


def process_vod(streamer, vod, cfg):
    folder = WORK / vod["id"]
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    credit = streamer.get("credit") or "@" + streamer["name"]

    duration = vod_duration(vod["url"])
    if duration is None:
        print("still live, try later")
        return "live"
    if duration > cfg["max_vod_hours"] * 3600:
        print(f"skipping: {duration / 3600:.1f}h is too long")
        return "too_long"

    audio = download_audio(vod["url"], folder)
    transcript = analyze.build_transcript(audio, cfg)
    (folder / "transcript.txt").write_text(transcript)
    clips = analyze.pick_clips(transcript, cfg, credit, duration)
    print(f"picked {len(clips)} clips")
    audio.unlink(missing_ok=True)

    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    made = 0
    for i, c in enumerate(clips):
        try:
            raw = download_section(vod["url"], c["start"], c["end"], folder / f"raw{i}.mp4")
            dur = media_duration(raw)
            words = clip_words(raw, cfg)
            base = f"{stamp}_{i:02d}_{streamer['name']}_{vod['id']}"
            out = render.render(raw, folder / f"{base}.mp4", words, c["hook"], credit,
                                dur, streamer.get("facecam"), cfg.get("zoom", 1.45))
            meta = dict(c, credit=credit, streamer=streamer["name"], vod=vod["url"])
            (folder / f"{base}.json").write_text(json.dumps(meta, indent=2))
            run(["gh", "release", "upload", "queue", out, folder / f"{base}.json",
                 "--clobber"])
            made += 1
            raw.unlink(missing_ok=True)
        except Exception as e:  # one bad clip shouldn't kill the run
            print(f"clip {i} failed: {e}", file=sys.stderr)
    shutil.rmtree(folder, ignore_errors=True)
    return f"ok:{made}"


def main():
    cfg = load_config()
    state = load_state()
    done = {p["id"] for p in state["processed"]}
    ensure_queue_release()

    for streamer in cfg["streamers"]:
        queued = queued_clip_count()
        if queued >= cfg["queue_target"]:
            print(f"queue has {queued} clips, enough for now")
            break
        try:
            vods = list_vods(streamer["url"], cfg.get("look_back_vods", 3))
        except Exception as e:
            print(f"could not list {streamer['name']}: {e}", file=sys.stderr)
            continue
        for vod in vods:
            if vod["id"] in done:
                continue
            print(f"== {streamer['name']}: {vod['title']} ({vod['id']})")
            try:
                result = process_vod(streamer, vod, cfg)
            except Exception as e:
                print(f"VOD failed: {e}", file=sys.stderr)
                result = f"error:{str(e)[:120]}"
            if result != "live":
                state["processed"].append({"id": vod["id"], "streamer": streamer["name"],
                                           "result": result,
                                           "at": time.strftime("%Y-%m-%d %H:%M")})
                save_state(state)
            break  # one VOD per streamer per run keeps runs short


if __name__ == "__main__":
    main()
