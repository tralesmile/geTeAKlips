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


def vod_duration(url):  # kept for compatibility
    out = run(ytdlp_base() + ["-J", "--no-playlist", url], capture=True)
    info = json.loads(out)
    return float(info.get("duration") or 0)


def download_audio(url, start, end, folder):
    run(ytdlp_base() + ["-f", "bestaudio/worst",
                        "--download-sections", f"*{start:.0f}-{end:.0f}",
                        "-o", str(folder / "audio.%(ext)s"), url])
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
    segments, _ = model.transcribe(analyze.load_audio_16k(path), language=cfg.get("language"),
                                   word_timestamps=True, beam_size=1)
    return [(w.start, w.end, w.word) for s in segments for w in (s.words or [])]


def media_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", path], capture=True)
    return float(out.strip())


def process_window(streamer, vod, start, end, cfg):
    """Clip the part of a VOD between start and end (seconds)."""
    folder = WORK / vod["id"]
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    credit = streamer.get("credit") or "@" + streamer["name"]

    audio = download_audio(vod["url"], start, end, folder)
    transcript = analyze.build_transcript(audio, cfg, offset=start)
    (folder / "transcript.txt").write_text(transcript)
    clips = analyze.pick_clips(transcript, cfg, credit, end, window_start=start)
    print(f"picked {len(clips)} clips")
    audio.unlink(missing_ok=True)

    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    made = 0
    for i, c in enumerate(clips):
        try:
            raw = download_section(vod["url"], c["start"], c["end"], folder / f"raw{i}.mp4")
            dur = media_duration(raw)
            words = clip_words(raw, cfg)
            base = f"{stamp}_{i:02d}_{streamer['name']}_{vod['id']}_{int(start)}"
            out = render.render(raw, folder / f"{base}.mp4", words, c["hook"], credit,
                                dur, streamer.get("facecam"),
                                streamer.get("zoom", cfg.get("zoom", 1.45)))
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


def next_window(vod, duration, progress, cfg):
    """Newest unclipped stretch of a VOD, at most chunk_hours long."""
    chunk = cfg.get("chunk_hours", 3) * 3600
    done_until = progress.get(vod["id"], 0.0)
    start = max(done_until, duration - chunk)
    if duration - start < cfg.get("min_new_minutes", 20) * 60:
        return None
    return start, duration


def video_info(url):
    out = run(ytdlp_base() + ["-J", "--no-playlist", url], capture=True)
    info = json.loads(out)
    return {"id": str(info.get("id")), "url": url, "title": info.get("title") or "",
            "duration": float(info.get("duration") or 0)}


def is_gta(title):
    t = (title or "").lower()
    return "gta" in t or "grand theft auto" in t


def candidate_videos(streamer, cfg):
    """Videos you added by hand (newest added first), then the channel's newest
    VODs. GTA videos go before the rest; otherwise order is kept."""
    vids = [{"url": u, "id": None, "title": ""} for u in reversed(streamer.get("videos") or [])]
    if streamer.get("url"):
        try:
            listed = list_vods(streamer["url"], cfg.get("look_back_vods", 2))
            if not listed:
                print(f"{streamer['name']}: no public videos found at {streamer['url']}")
            vids += listed
        except Exception as e:
            print(f"could not list {streamer['name']}: {e}", file=sys.stderr)
    manual = [v for v in vids if v["id"] is None]
    listed = sorted([v for v in vids if v["id"] is not None], key=lambda v: not is_gta(v["title"]))
    return manual + listed


def main():
    cfg = load_config()
    state = load_state()
    progress = state.setdefault("progress", {})
    ensure_queue_release()
    target = cfg.get("clips_per_streamer", 8)

    for streamer in cfg["streamers"]:
        have = queued_clip_count(streamer["name"])
        if have >= target:
            print(f"{streamer['name']}: {have} clips waiting, enough for now")
            continue
        for vod in candidate_videos(streamer, cfg):
            try:
                info = video_info(vod["url"])
            except Exception as e:
                print(f"could not read {vod['url']}: {str(e)[:300]}", file=sys.stderr)
                continue
            vod = dict(vod, id=info["id"], title=vod["title"] or info["title"])
            window = next_window(vod, info["duration"], progress, cfg) if info["duration"] else None
            if not window:
                continue
            start, end = window
            print(f"== {streamer['name']}: {vod['title'][:80]} "
                  f"({vod['id']}) {start / 3600:.1f}h-{end / 3600:.1f}h")
            try:
                result = process_window(streamer, vod, start, end, cfg)
            except Exception as e:
                print(f"window failed: {e}", file=sys.stderr)
                result = f"error:{str(e)[:120]}"
            progress[vod["id"]] = end
            state["processed"].append({"id": vod["id"], "streamer": streamer["name"],
                                       "window": [int(start), int(end)], "result": result,
                                       "at": time.strftime("%Y-%m-%d %H:%M")})
            state["processed"] = state["processed"][-200:]
            save_state(state)
            break  # one window per streamer per run keeps runs short


if __name__ == "__main__":
    main()
