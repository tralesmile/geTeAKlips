"""Transcribe a VOD's audio, mark loud moments, and ask Claude for the best clips."""
import json
import os
import re
import subprocess

import numpy as np
import requests

_whisper = {}


def whisper_model(name):
    if name not in _whisper:
        from faster_whisper import WhisperModel
        _whisper[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _whisper[name]


def load_audio_16k(path):
    """Decode any media file to 16 kHz mono float32 with ffmpeg (avoids PyAV)."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"], capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def loudness_per_second(audio_path):
    """RMS loudness (dB) for each second of audio."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(audio_path), "-ac", "1", "-ar", "8000",
         "-f", "s16le", "-"], capture_output=True, check=True)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
    n = len(pcm) // 8000
    if n == 0:
        return np.array([])
    sec = pcm[: n * 8000].reshape(n, 8000)
    rms = np.sqrt((sec ** 2).mean(axis=1)) + 1e-6
    return 20 * np.log10(rms / 32768)


def fmt(t):
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def build_transcript(audio_path, cfg, offset=0.0):
    model = whisper_model(cfg.get("whisper_model", "base"))
    segments, _ = model.transcribe(load_audio_16k(audio_path), language=cfg.get("language"),
                                   vad_filter=True, beam_size=1)
    loud = loudness_per_second(audio_path)
    threshold = np.percentile(loud, 97) if len(loud) else 0
    lines = []
    for s in segments:
        window = loud[int(s.start): int(s.end) + 1]
        tag = " [LOUD]" if len(window) and window.max() >= threshold else ""
        lines.append(f"[{fmt(s.start + offset)}] {s.text.strip()}{tag}")
    return "\n".join(lines)


PROMPT = """You are an expert short-form video editor making viral GTA 6 clips \
for TikTok, Instagram Reels and YouTube Shorts from a stream by {credit}.

Below is the timestamped transcript of the stream. [LOUD] marks screaming, \
laughing or big reactions.{hint} Pick the {n} best self-contained moments while they are playing GTA 6: \
funny fails, crazy chases, big reactions, surprising game moments, memorable \
quotes. The streamer ({credit}) must be the main focus: their words and reactions \
drive the clip. Never pick moments that make them look bad. \
{learn}Viewers decide in the first 2 seconds, and clips that are watched to the end \
get shown to more people. So each clip must be {min_s}-{max_s} seconds (shorter \
is better when the moment allows), start the instant the action or line begins \
(no greetings, no setup, no dead air) and end right after the payoff, ideally \
on a beat that makes people rewatch. Clips must not overlap.

Return ONLY a JSON array, best clip first, each item:
{{"start": "H:MM:SS", "end": "H:MM:SS",
  "peak": "H:MM:SS of the single funniest/loudest second inside the clip",
  "hook": "on-screen hook that makes people stay, names the streamer, max 7 words, \
no emojis, e.g. 'Lacy did NOT expect this' or 'Clix finds the worst car in Vice City'",
  "title": "YouTube title, max 70 chars, includes the streamer name and 'GTA 6'",
  "comment": "a short, fun question to post as the first comment, max 12 words, \
e.g. 'Who's watching the whole marathon?'",
  "caption": "80-150 characters, natural English for TikTok/Instagram search: \
include the streamer name, 'GTA 6' and what happens (e.g. police chase, mission fail, \
Vice City). End with a short question to get comments."}}

TRANSCRIPT:
{transcript}"""


def to_seconds(ts):
    parts = [float(p) for p in str(ts).split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def parse_clips(text, cfg, duration, window_start=0.0):
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ValueError("model did not return a JSON array")
    raw = json.loads(match.group(0))
    clips, taken = [], []
    lo, hi = cfg["clip_min_seconds"], cfg["clip_max_seconds"]
    for c in raw:
        try:
            start, end = to_seconds(c["start"]), to_seconds(c["end"])
        except (KeyError, ValueError):
            continue
        if end - start < lo:
            end = start + lo
        if end - start > hi:
            end = start + hi
        if start < window_start or (duration and end > duration):
            continue
        if any(start < e and end > s for s, e in taken):
            continue
        taken.append((start, end))
        try:
            peak = to_seconds(c.get("peak", ""))
            peak = peak if start < peak < end else None
        except ValueError:
            peak = None
        clips.append({"start": start, "end": end, "peak": peak,
                      "hook": str(c.get("hook", ""))[:60],
                      "title": str(c.get("title", ""))[:90],
                      "comment": str(c.get("comment", ""))[:150],
                      "caption": str(c.get("caption", ""))[:220]})
    return clips[: cfg["clips_per_vod"]]


def winners_note():
    """Hooks/titles that got the most views on our own accounts (state/winners.json,
    written by the daily stats job), so new clips learn from what works."""
    try:
        from .common import ROOT
        best = json.loads((ROOT / "state" / "winners.json").read_text())[:6]
    except (OSError, ValueError):
        return ""
    if not best:
        return ""
    lines = "\n".join(f'- "{w["hook"]}" ({w["views"]} views)' for w in best if w.get("hook"))
    return (f"On our own accounts these hooks got the most views, so write hooks in a "
            f"similar style (don't copy them):\n{lines}\n\n") if lines else ""


def pick_clips(transcript, cfg, credit, duration, window_start=0.0, n=None, hint=""):
    n = n or cfg["clips_per_vod"]
    hint = (f"\nIMPORTANT: this part of the stream is popular: {hint}. Find exactly "
            "that moment and cut it so it works on its own.") if hint else ""
    prompt = PROMPT.format(credit=credit, n=n, hint=hint, learn=winners_note(),
                           min_s=cfg["clip_min_seconds"],
                           max_s=cfg["clip_max_seconds"], transcript=transcript)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg.get("llm_model", "claude-sonnet-5-5"),
              "max_tokens": 4000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=300)
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json()["content"])
    return parse_clips(text, dict(cfg, clips_per_vod=n), duration, window_start)
