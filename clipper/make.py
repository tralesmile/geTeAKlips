"""Find new VODs, cut the best moments into vertical clips, push them to the queue.

Run by .github/workflows/make-clips.yml.
"""
import json
import shutil
import sys
import time
from pathlib import Path

from . import analyze, edit, popular
from .common import (STATE_FILE, WORK, ensure_queue_release, load_config, load_state,
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
            vods.append({"id": str(vid), "url": link, "title": e.get("title", ""),
                         "duration": float(e.get("duration") or 0)})
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


def clip_words(path, cfg, streamer=None):
    """Word-level captions for one short clip. Uses a more accurate model than the
    long-stream transcript, and primes it with names and GTA words so they're
    spelled right."""
    model = analyze.whisper_model(cfg.get("caption_model", "small.en"))
    names = ", ".join(x.get("credit", x["name"]) for x in cfg["streamers"])
    prompt = (f"{streamer.get('credit', '') if streamer else ''} streaming GTA 6. "
              f"{names}. Vice City, Leonida, Jason, Lucia, chat, bro, cops, mission.")
    segments, _ = model.transcribe(
        analyze.load_audio_16k(path), language=cfg.get("language"), word_timestamps=True,
        beam_size=5, vad_filter=True, condition_on_previous_text=False,
        initial_prompt=prompt)
    # drop words whisper is unsure about (often game audio / music, not the streamer)
    return [(w.start, w.end, w.word) for s in segments for w in (s.words or [])
            if getattr(w, "probability", 1.0) >= 0.4]


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
    clips = analyze.pick_clips(transcript, cfg, credit, end, window_start=start,
                               focus=streamer.get("focus", "while they are playing GTA 6"))
    print(f"picked {len(clips)} clips")
    audio.unlink(missing_ok=True)

    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    made = 0
    for i, c in enumerate(clips):
        base = f"{stamp}_{i:02d}_{streamer['name']}_{vod['id']}_{int(start)}"
        made += render_clip(streamer, vod["url"], c, folder, base, cfg)
    shutil.rmtree(folder, ignore_errors=True)
    return f"ok:{made}"


def render_clip(streamer, url, c, folder, base, cfg):
    """Download, caption, edit and queue one clip. Returns 1 on success."""
    credit = streamer.get("credit") or "@" + streamer["name"]
    try:
        raw = download_section(url, c["start"], c["end"], folder / f"{base}_raw.mp4")
        words = clip_words(raw, cfg, streamer)
        peak = c["peak"] - c["start"] if c.get("peak") else None
        out, layout = edit.render(raw, folder / f"{base}.mp4", words, c["hook"], credit,
                                  peak=peak, facecam=streamer.get("facecam"),
                                  streamer=streamer.get("cam_key", streamer["name"]))
        meta = dict(c, credit=credit, streamer=streamer["name"], vod=url, layout=layout,
                    cover_ms=getattr(edit.render, "cover_ms", 500),
                    made_at=int(time.time()))
        (folder / f"{base}.json").write_text(json.dumps(meta, indent=2))
        run(["gh", "release", "upload", "queue", out, folder / f"{base}.json", "--clobber"])
        raw.unlink(missing_ok=True)
        return 1
    except Exception as e:  # one bad clip shouldn't kill the run
        print(f"clip {base} failed: {str(e)[:500]}", file=sys.stderr)
        return 0


def process_moment(streamer, m, cfg, index):
    """Cut the best 15-35 s from one popular stretch of a stream."""
    folder = WORK / f"m{index}"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    credit = streamer.get("credit") or "@" + streamer["name"]
    audio = download_audio(m["url"], m["start"], m["end"], folder)
    transcript = analyze.build_transcript(audio, cfg, offset=m["start"])
    clips = analyze.pick_clips(transcript, cfg, credit, m["end"], window_start=m["start"],
                               focus=streamer.get("focus", "while they are playing GTA 6"),
                               n=1, hint=m["hint"])
    if not clips:
        return 0
    c = dict(clips[0], source=m["source"], popularity=m["score"])
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    base = f"{stamp}_{index:02d}_{streamer['name']}_{m['vod']}_{int(c['start'])}"
    made = render_clip(streamer, m["url"], c, folder, base, cfg)
    shutil.rmtree(folder, ignore_errors=True)
    return made


def overlaps(m, done_windows):
    for a, b in done_windows.get(m["vod"], []):
        inter = min(b, m["end"]) - max(a, m["start"])
        if inter > 0.4 * (m["end"] - m["start"]):
            return True
    return False


def clip_popular(streamer, need, state, cfg):
    """Make up to `need` clips from the most popular moments. Returns clips made."""
    done = set(state.setdefault("moments_done", []))
    windows = state.setdefault("moment_windows", {})
    try:
        moments = popular.popular_moments(streamer)
    except Exception as e:
        print(f"{streamer['name']}: popularity lookup failed: {e}", file=sys.stderr)
        return 0
    fresh = [m for m in moments if m["key"] not in done and not overlaps(m, windows)]
    print(f"{streamer['name']}: {len(moments)} popular moments, {len(fresh)} new")
    made = 0
    for i, m in enumerate(fresh[: cfg.get("moments_per_run", 5)]):
        if made >= need:
            break
        if overlaps(m, windows):  # same moment as one clipped earlier in this run
            continue
        print(f"== {streamer['name']} popular ({m['source']}, score {m['score']:.0f}): "
              f"{m['hint'][:80]}")
        who = streamer
        if m.get("credit"):  # pooled account: credit the streamer in this moment
            who = dict(streamer, credit=m["credit"], cam_key=m.get("login", streamer["name"]))
        try:
            made += process_moment(who, m, cfg, i)
        except Exception as e:
            print(f"moment failed: {str(e)[:300]}", file=sys.stderr)
        state["moments_done"] = (state["moments_done"] + [m["key"]])[-500:]
        windows.setdefault(m["vod"], []).append([m["start"], m["end"]])
        windows[m["vod"]] = windows[m["vod"]][-100:]
        save_state(state)
    return made


def next_window(vod, duration, progress, cfg):
    """Newest unclipped stretch of a VOD, at most chunk_hours long."""
    chunk = cfg.get("chunk_hours", 3) * 3600
    done_until = progress.get(vod["id"], 0.0)
    start = max(done_until, duration - chunk)
    if duration - start < cfg.get("min_new_minutes", 20) * 60:
        return None
    return start, duration


def earlier_window(vod, duration, state, cfg):
    """Next older unclipped stretch of a finished VOD (before the earliest
    window already processed), at most chunk_hours long."""
    chunk = cfg.get("chunk_hours", 3) * 3600
    starts = [w["window"][0] for w in state.get("processed", [])
              if w.get("id") == vod["id"] and w.get("window")]
    if not starts:
        return None
    end = min(starts)
    if end < cfg.get("min_new_minutes", 20) * 60:
        return None
    return max(0.0, end - chunk), end


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
        have = queued_clip_count(streamer["name"], cfg.get("max_clip_age_hours", 36))
        if have >= target:
            print(f"{streamer['name']}: {have} clips waiting, enough for now")
            continue
        # 1) moments viewers liked most (Twitch clips, YouTube most-replayed)
        if clip_popular(streamer, target - have, state, cfg) > 0:
            continue
        # 2) backup: let Claude scan the newest stretch of the stream
        for vod in candidate_videos(streamer, cfg):
            try:
                info = video_info(vod["url"])
            except Exception as e:
                print(f"could not read {vod['url']}: {str(e)[:300]}", file=sys.stderr)
                continue
            manual = vod["id"] is None
            vod = dict(vod, id=info["id"], title=vod["title"] or info["title"])
            # some sites (Kick) give the length only in the channel listing
            duration = info["duration"] or vod.get("duration") or 0
            window = next_window(vod, duration, progress, cfg) if duration else None
            if not window and manual and duration:
                # hand-added VODs are finished streams: once the newest part is
                # done, keep going backwards chunk by chunk (older parts)
                window = earlier_window(vod, duration, state, cfg)
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
            progress[vod["id"]] = max(progress.get(vod["id"], 0.0), end)
            state["processed"].append({"id": vod["id"], "streamer": streamer["name"],
                                       "window": [int(start), int(end)], "result": result,
                                       "at": time.strftime("%Y-%m-%d %H:%M")})
            state["processed"] = state["processed"][-200:]
            save_state(state)
            break  # one window per streamer per run keeps runs short


if __name__ == "__main__":
    main()
