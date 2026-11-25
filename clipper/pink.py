"""Pinkove Zvezde clips for the Yugoslovenko Instagram (profile "tips").

Runs inside make-clips.yml: finds new Pinkove Zvezde videos on YouTube, takes
their most re-watched moments (YouTube heatmap), lets Claude pick the funniest /
most dramatic 12-30 s with a Serbian meme hook, renders a captioned vertical
Reel and puts it in the queue as streamer "pink". post.py posts it from the
"pink" posting profile (upload_user tips).

Never uses Pinkove Zvezdice (the children's show).
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from . import analyze, edit
from .common import (WORK, load_config, queued_clip_count, run, ytdlp_base)

STATE = Path("state/pink.json")
NAME = "pink"


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"done": [], "videos": {}}


def save_state(s):
    s["done"] = s["done"][-400:]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1, ensure_ascii=False))


def _json(target, extra=()):
    out = run(ytdlp_base() + ["-J", *extra, target], capture=True)
    return json.loads(out)


def ok_title(title):
    t = (title or "").lower()
    if "zvezdic" in t or "звездиц" in t:         # children's show: never
        return False
    return ("pinkov" in t and "zvezd" in t) or "пинков" in t


def candidates(pcfg):
    """Recent Pinkove Zvezde videos, newest first, from YouTube search."""
    seen, out = set(), []
    for q in pcfg.get("searches", ["Pinkove zvezde"]):
        try:
            data = _json(f"ytsearchdate{pcfg.get('search_n', 25)}:{q}", ["--flat-playlist"])
        except Exception as e:
            print(f"search failed {q}: {str(e)[:200]}", file=sys.stderr)
            continue
        for e in data.get("entries") or []:
            vid, dur = e.get("id"), e.get("duration") or 0
            if not vid or vid in seen or not ok_title(e.get("title")):
                continue
            if not 40 <= dur <= pcfg.get("max_video_seconds", 4 * 3600):
                continue
            seen.add(vid)
            out.append({"id": vid, "title": e.get("title", ""), "duration": dur,
                        "url": f"https://www.youtube.com/watch?v={vid}"})
    return out


def moments(video, done, max_age_days):
    """Most re-watched spots of one video (falls back to the start of short clips)."""
    try:
        info = _json(video["url"], ["--no-playlist"])
    except Exception as e:
        print(f"video failed {video['id']}: {str(e)[:200]}", file=sys.stderr)
        return []
    if info.get("timestamp") and time.time() - info["timestamp"] > max_age_days * 86400:
        return []
    dur = float(info.get("duration") or video["duration"])
    heat = sorted(info.get("heatmap") or [], key=lambda h: -h.get("value", 0))
    mids = []
    for h in heat:
        mid = (h["start_time"] + h["end_time"]) / 2
        if h.get("value", 0) < 0.3 or len(mids) >= 6:
            break
        if all(abs(mid - m) > 90 for m in mids):
            mids.append(mid)
    if not mids and dur <= 240:      # short clip without heatmap: the clip itself
        mids = [min(dur / 2, 45)]
    views = info.get("view_count") or 0
    out = []
    for mid in mids:
        key = f"{info['id']}:{int(mid // 30)}"
        if key in done:
            continue
        out.append({"key": key, "url": video["url"], "id": info["id"],
                    "title": info.get("title", ""), "start": max(0.0, mid - 40),
                    "end": min(dur, mid + 30), "score": views, "peak": mid})
    return out


PROMPT = """Ti praviš Reels za srpsku meme stranicu {handle} (porodične fore, vicevi, \
smešni momenti sa TV-a). Ovo je transkript isečka iz emisije "Pinkove zvezde" \
(video: "{title}"). Vremena su u sekundama od početka isečka.

{transcript}

Izaberi JEDAN najsmešniji ili najdramatičniji deo (svađa žirija, smešan komentar, \
neočekivana reakcija, ludi nastup) dužine {min_s}-{max_s} sekundi koji se razume \
bez konteksta. Ne biraj samo pevanje bez razgovora. Ne ismevaj izgled ili telo \
takmičara; zezaj situaciju, žiri i reakcije.

Vrati SAMO JSON:
{{"start": sekunde, "end": sekunde, "peak": sekunde najjačeg trenutka,
 "hook": "meme tekst na vrhu videa, srpski latinica, max 55 karaktera, kao Balkan \
meme (npr. 'Kad tetka na slavi kaže da i ona zna da peva')",
 "caption": "opis za Instagram, srpski latinica, 80-150 karaktera, 1-2 emoji, \
završi pitanjem da ljudi komentarišu",
 "comment": "prvi komentar, kratko pitanje publici",
 "title": "kratak naslov"}}
Ako nema ničeg dovoljno dobrog, vrati {{"skip": true}}."""


def ask_claude(cfg, prompt):
    for _ in range(3):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": cfg.get("llm_model", "claude-sonnet-5-5"), "max_tokens": 1500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
        print("claude reply not JSON:", text[:300], file=sys.stderr)
    return None


def transcribe(path, model, offset=0.0, words=False):
    m = analyze.whisper_model(model)
    segs, _ = m.transcribe(analyze.load_audio_16k(path), language="sr", beam_size=5,
                           vad_filter=True, word_timestamps=words,
                           condition_on_previous_text=False,
                           initial_prompt="Pinkove zvezde, žiri, Karleuša, Desingerica, "
                                          "Viki, Brunclik. Srpski, latinica.")
    segs = list(segs)
    if words:
        return [(w.start, w.end, w.word) for s in segs for w in (s.words or [])
                if getattr(w, "probability", 1.0) >= 0.35]
    return "\n".join(f"[{s.start + offset:.0f}-{s.end + offset:.0f}] {s.text.strip()}"
                     for s in segs)


def make_one(m, cfg, pcfg, index):
    folder = WORK / "pink"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    base = f"{stamp}_{index}_{NAME}_{m['id']}_{int(m['start'])}"
    raw = folder / f"{base}_raw.mp4"
    run(ytdlp_base() + ["-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
                        "--download-sections", f"*{m['start']:.1f}-{m['end']:.1f}",
                        "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
                        "-o", str(raw), m["url"]])
    model = pcfg.get("whisper_model", "small")
    transcript = transcribe(raw, model)
    if len(transcript) < 40:
        print(f"[pink] {m['key']}: no speech; skipping")
        return False
    dur = edit.probe(raw)[2]
    pick = ask_claude(cfg, PROMPT.format(
        handle=cfg.get("memes", {}).get("handle", ""), title=m["title"],
        transcript=transcript, min_s=pcfg.get("clip_min", 12), max_s=pcfg.get("clip_max", 30)))
    if not pick or pick.get("skip"):
        print(f"[pink] {m['key']}: Claude found nothing good")
        return False
    a = max(0.0, float(pick["start"]) - 0.3)
    b = min(dur, float(pick["end"]) + 0.4, a + pcfg.get("clip_max", 30) + 2)
    if b - a < 6:
        return False
    cut = folder / f"{base}_cut.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.2f}", "-i", str(raw), "-t", f"{b - a:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(cut)])
    words = transcribe(cut, model, words=True)
    peak = float(pick.get("peak", a)) - a
    out, layout = edit.render(cut, folder / f"{base}.mp4", words, pick["hook"],
                              "Pinkove Zvezde", peak=peak if 0 < peak < b - a else None,
                              streamer=NAME, tag="")
    meta = {"streamer": NAME, "credit": "Pinkove Zvezde", "hook": pick["hook"],
            "title": pick.get("title", pick["hook"]), "caption": pick["caption"],
            "comment": pick.get("comment", ""), "tags": pcfg.get("hashtags", ""),
            "notify": False, "media_type": "REELS", "vod": m["url"], "layout": layout,
            "cover_ms": getattr(edit.render, "cover_ms", 500), "made_at": int(time.time())}
    (folder / f"{base}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    run(["gh", "release", "upload", "queue", out, folder / f"{base}.json", "--clobber"])
    for f in (raw, cut):
        f.unlink(missing_ok=True)
    print(f"[pink] queued {base}: {pick['hook']}")
    return True


def main():
    cfg = load_config()
    pcfg = cfg.get("pink") or {}
    if not pcfg.get("enabled", True):
        return
    have = queued_clip_count(NAME, cfg.get("max_clip_age_hours", 36))
    need = pcfg.get("queue_target", 4) - have
    print(f"[pink] {have} queued, making {max(need, 0)}")
    if need <= 0:
        return
    state = load_state()
    done = set(state["done"])
    found = []
    for v in candidates(pcfg)[: pcfg.get("videos_to_check", 12)]:
        found += moments(v, done, pcfg.get("max_age_days", 10))
        if len(found) >= need * 3:
            break
    found.sort(key=lambda m: -m["score"])
    made = 0
    for i, m in enumerate(found):
        if made >= need:
            break
        state["done"].append(m["key"])   # never retry the same spot
        try:
            made += make_one(m, cfg, pcfg, i)
        except Exception as e:
            print(f"[pink] {m['key']} failed: {str(e)[:400]}", file=sys.stderr)
        save_state(state)
    print(f"[pink] made {made}")


if __name__ == "__main__":
    main()
