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
laughing or big reactions. Pick the {n} best self-contained moments while they are playing GTA 6: \
funny fails, crazy chases, big reactions, surprising game moments, memorable \
quotes. The streamer ({credit}) must be the main focus: their words and reactions \
drive the clip. Never pick moments that make them look bad. \
Each clip must be {min_s}-{max_s} seconds long, start right before the action \
(no slow intro) and end right after the payoff. Clips must not overlap.

Return ONLY a JSON array, best clip first, each item:
{{"start": "H:MM:SS", "end": "H:MM:SS",
  "hook": "on-screen hook text that names the streamer, max 7 words, no emojis",
  "title": "catchy title, max 70 chars",
  "caption": "1-2 sentence post caption in English that names the streamer"}}

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
        clips.append({"start": start, "end": end,
                      "hook": str(c.get("hook", ""))[:60],
                      "title": str(c.get("title", ""))[:90],
                      "caption": str(c.get("caption", ""))[:500]})
    return clips[: cfg["clips_per_vod"]]


def pick_clips(transcript, cfg, credit, duration, window_start=0.0):
    prompt = PROMPT.format(credit=credit, n=cfg["clips_per_vod"],
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
    return parse_clips(text, cfg, duration, window_start)
