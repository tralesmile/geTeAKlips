"""Pro vertical editor: auto layout, cold-open teaser, dead-air cuts,
punch-in zooms, word-highlight captions.

Layouts (picked automatically per clip):
  split    - streamer facecam on top, gameplay below (cam detected via faces)
  irl      - camera-only scenes: 9:16 crop that follows the people
  gameplay - no face found: square gameplay crop on a blurred background
"""
import subprocess
from pathlib import Path

import numpy as np

from .common import ROOT, run

W, H = 1080, 1920
CAM_H = 640                      # split layout: facecam panel height
FONT_DIR = ROOT / "fonts"
FONT = "Archivo Black"
YELLOW = "&H004DE1FF"            # ASS colours are &HAABBGGRR


# ── media helpers ──────────────────────────────────────────────────────

def probe(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height:format=duration", "-of", "json", path], capture=True)
    import json
    info = json.loads(out)
    s = info["streams"][0]
    return int(s["width"]), int(s["height"]), float(info["format"]["duration"])


def loudness_10hz(path):
    """Loudness in dB for every 0.1 s of audio."""
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                           "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
    n = len(pcm) // 800
    if n == 0:
        return np.zeros(1)
    rms = np.sqrt((pcm[: n * 800].reshape(n, 800) ** 2).mean(axis=1)) + 1e-6
    return 20 * np.log10(rms / 32768)


# ── layout detection ───────────────────────────────────────────────────

def _frames(path, n=16, width=960):
    import cv2
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    out = []
    for k in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (0.05 + 0.9 * k / max(n - 1, 1))))
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        frame = cv2.resize(frame, (width, int(h * width / w)))
        out.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    cap.release()
    return out


def _stable_faces(frames):
    """Faces that stay in the same place across frames (a facecam or a fixed camera).
    Returns clusters [(cx, cy, size, hits)] in 0..1 units, biggest support first."""
    import cv2
    cascade = cv2.CascadeClassifier(str(ROOT / "models" / "haarcascade_frontalface_default.xml"))
    dets = []
    for i, g in enumerate(frames):
        fh, fw = g.shape
        for (x, y, w, h) in cascade.detectMultiScale(g, 1.1, 4, minSize=(18, 18)):
            dets.append((i, (x + w / 2) / fw, (y + h / 2) / fh, h / fh))
    clusters = []
    for i, cx, cy, s in dets:
        for c in clusters:
            if abs(c["cx"] - cx) < 0.05 and abs(c["cy"] - cy) < 0.07 and 0.6 < s / c["s"] < 1.6:
                c["frames"].add(i)
                c["pts"].append((cx, cy, s))
                break
        else:
            clusters.append({"cx": cx, "cy": cy, "s": s, "frames": {i}, "pts": [(cx, cy, s)]})
    need = max(3, int(0.3 * len(frames)))
    good = []
    for c in clusters:
        if len(c["frames"]) >= need:
            p = np.array(c["pts"])
            good.append((float(np.median(p[:, 0])), float(np.median(p[:, 1])),
                         float(np.median(p[:, 2])), len(c["frames"])))
    return sorted(good, key=lambda c: -c[3])


def _edge(profile, lo, hi, fallback):
    """Strongest stable edge between lo and hi in a 1-D edge profile."""
    lo, hi = max(lo, 1), min(hi, len(profile) - 1)
    if hi <= lo:
        return fallback, 0.0
    seg = profile[lo:hi]
    k = int(np.argmax(seg))
    strength = float(seg[k] / (np.median(profile) + 1e-6))
    return lo + k, strength


def _find_cam_box(frames, face):
    """Grow a facecam rectangle around a small stable face and snap it to the
    strongest stable edges (cam borders don't move, gameplay does)."""
    import cv2
    fh, fw = frames[0].shape
    cx, cy, s = face[0] * fw, face[1] * fh, face[2] * fh
    cam_w = float(np.clip(s * 5.0, 0.16 * fw, 0.36 * fw))
    cam_h = cam_w * 9 / 16
    x0, y0 = cx - cam_w / 2, cy - cam_h * 0.45
    # stable edges: average edge maps over time
    gx = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in frames], axis=0)
    gy = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in frames], axis=0)
    r0, r1 = int(max(cy - s, 0)), int(min(cy + s, fh))
    c0, c1 = int(max(cx - s, 0)), int(min(cx + s, fw))
    col = gx[r0:r1].mean(axis=0)
    row = gy[:, c0:c1].mean(axis=1)
    left, sl = (0, 9.0) if x0 < 0.05 * fw else _edge(col, int(x0 - 0.35 * cam_w), int(cx - s), int(x0))
    right, sr = (fw, 9.0) if x0 + cam_w > 0.95 * fw else _edge(col, int(cx + s), int(x0 + 1.35 * cam_w), int(x0 + cam_w))
    top, st = (0, 9.0) if y0 < 0.05 * fh else _edge(row, int(y0 - 0.35 * cam_h), int(cy - s), int(y0))
    bottom, sb = (fh, 9.0) if y0 + cam_h > 0.95 * fh else _edge(row, int(cy + s), int(y0 + 1.4 * cam_h), int(y0 + cam_h))
    box = [left / fw, top / fh, (right - left) / fw, (bottom - top) / fh]
    edges_ok = sum(v > 2.2 for v in (sl, sr, st, sb)) >= 2
    sane = 0.1 < box[2] < 0.45 and 0.1 < box[3] < 0.55
    return box, edges_ok and sane


def detect_layout(path, override=None):
    """Returns (layout, params). override = config facecam [x, y, w, h]."""
    if override:
        return "split", {"cam": list(override)}
    try:
        frames = _frames(path)
    except Exception as e:  # OpenCV missing or unreadable video
        print(f"layout detection failed ({e}); using gameplay layout")
        return "gameplay", {}
    if len(frames) < 3:
        return "gameplay", {}
    try:
        faces = _stable_faces(frames)
    except Exception as e:
        print(f"face detection failed ({e}); using gameplay layout")
        return "gameplay", {}
    if not faces:
        return "gameplay", {}
    main = faces[0]
    others = [f for f in faces[1:] if f[3] >= 0.6 * main[3]]
    if others:  # several people on screen: a camera scene, not a facecam
        xs = [f[0] for f in [main] + others]
        pad = max(f[2] for f in [main] + others) * 0.9
        return "irl", {"x0": max(min(xs) - pad, 0.0), "x1": min(max(xs) + pad, 1.0),
                       "cx": float(np.mean(xs))}
    if main[2] < 0.16:
        box, ok = _find_cam_box(frames, main)
        if ok:
            return "split", {"cam": box}
    big = [f for f in faces if f[2] >= 0.09]
    if big:
        xs = [f[0] for f in big]
        pad = max(f[2] for f in big) * 0.9
        return "irl", {"x0": max(min(xs) - pad, 0.0), "x1": min(max(xs) + pad, 1.0),
                       "cx": float(np.mean(xs))}
    box, _ = _find_cam_box(frames, main)  # small face, weak edges: still best guess
    return "split", {"cam": box}


def layout_graph(layout, p, sw, sh, src="[vc]", out="[lay]"):
    blur_bg = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
               f"boxblur=30:3,eq=brightness=-0.18:saturation=1.2")
    if layout == "split":
        x, y, w, h = p["cam"]
        cx, cy = int(x * sw) // 2 * 2, int(y * sh) // 2 * 2
        cw, ch = int(w * sw) // 2 * 2, int(h * sh) // 2 * 2
        game_h = H - CAM_H
        gw = int(sh * W / game_h) // 2 * 2
        gx = max(0, min(sw - gw, int(sw / 2 - gw / 2))) // 2 * 2
        return (f"{src}split=2[a][b];"
                f"[a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{CAM_H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{CAM_H}[cam];"
                f"[b]crop={gw}:{sh}:{gx}:0,scale={W}:{game_h}[game];"
                f"[cam][game]vstack=inputs=2,drawbox=x=0:y={CAM_H - 3}:w={W}:h=6:color=black@0.85:t=fill{out}")
    if layout == "irl":
        x0, x1 = p["x0"] * sw, p["x1"] * sw
        need = max(x1 - x0, sh * 9 / 16)
        if need <= sh * 9 / 16 * 1.05:          # one person: full-screen 9:16
            cw = int(sh * 9 / 16) // 2 * 2
            cx = int(np.clip(p["cx"] * sw - cw / 2, 0, sw - cw)) // 2 * 2
            return f"{src}crop={cw}:{sh}:{cx}:0,scale={W}:{H}{out}"
        cw = int(min(need, sh * 1.0)) // 2 * 2     # group: up to square, on blurred bg
        cx = int(np.clip((x0 + x1) / 2 - cw / 2, 0, sw - cw)) // 2 * 2
        return (f"{src}crop={cw}:{sh}:{cx}:0,split=2[a][b];[a]{blur_bg}[bg];"
                f"[b]scale={W}:-2[fg];[bg][fg]overlay=0:(H-h)/2{out}")
    gh_out = 1350                                  # gameplay: tall 4:5 centre crop
    cw = int(sh * W / gh_out) // 2 * 2
    gx = (sw - cw) // 2 // 2 * 2
    return (f"{src}split=2[a][b];[a]{blur_bg}[bg];"
            f"[b]crop={cw}:{sh}:{gx}:0,scale={W}:{gh_out}[fg];"
            f"[bg][fg]overlay=0:{(H - gh_out) // 2}{out}")


TEXT_Y = {  # (captions, hook, credit) y positions per layout
    "split": (1185, CAM_H, 1855),
    "irl": (1330, 300, 1855),
    "gameplay": (1160, 210, 1855),
}


# ── pacing: teaser + dead-air cuts ─────────────────────────────────────

def plan_segments(words, loud, dur, peak=None):
    """Return (segments, teaser_len). segments = [(src_start, src_end), ...]."""
    keep, cursor = [], 0.0
    floor = float(np.median(loud)) - 3 if len(loud) else -60
    marks = [(w[0], w[1]) for w in words] + [(dur, dur)]
    prev_end = 0.0
    for start, end in marks:
        gap = start - prev_end
        if gap > 1.0:
            a, b = int(prev_end * 10), int(start * 10)
            quiet = len(loud[a:b]) == 0 or float(np.mean(loud[a:b])) < floor
            if quiet:
                keep.append((cursor, prev_end + 0.15))
                cursor = max(start - 0.2, prev_end + 0.15)
        prev_end = max(prev_end, end)
    keep.append((cursor, dur))
    keep = [(a, b) for a, b in keep if b - a > 0.2]
    if sum(b - a for a, b in keep) < 12:       # too short after trimming: keep original
        keep = [(0.0, dur)]
    teaser = 0.0
    if peak is not None and dur > 18 and 5 < peak < dur - 2:
        t0, t1 = max(peak - 0.4, 0), min(peak + 1.6, dur)
        keep = [(t0, t1)] + keep
        teaser = t1 - t0
    return keep, teaser


def map_time(t, segments, skip_first=0):
    """Source time -> output time (first matching segment after skip_first)."""
    acc = sum(b - a for a, b in segments[:skip_first])
    for a, b in segments[skip_first:]:
        if a <= t <= b:
            return acc + (t - a)
        acc += b - a
    return None


def punch_times(loud, segments, skip_first, out_dur, teaser):
    """Up to 2 loudest moments (in output time) for a quick zoom punch."""
    if len(loud) < 10:
        return []
    thr = np.percentile(loud, 92)
    picks = []
    for idx in np.argsort(-loud):
        if loud[idx] < thr or len(picks) >= 2:
            break
        t = map_time(idx / 10, segments, skip_first)
        if t is None or t < teaser + 1.5 or t > out_dur - 1:
            continue
        if all(abs(t - q) > 4 for q in picks):
            picks.append(t)
    return ([0.15] if teaser else []) + picks


# ── captions ───────────────────────────────────────────────────────────

def _ts(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _clean(text):
    return (text.replace("\\", "").replace("{", "(").replace("}", ")")
            .replace("\n", " ").strip())


FILLERS = {"uh", "um", "uhh", "umm", "hmm", "mm", "ah", "er", "erm"}
MASK = {  # mild masking keeps clips safe for TikTok/Instagram reach
    "fuck": "F*CK", "fucking": "F*CKING", "fucked": "F*CKED", "motherfucker": "MOTHERF*CKER",
    "shit": "SH*T", "bitch": "B*TCH", "ass": "A*S", "dick": "D*CK", "pussy": "P*SSY",
    "nigga": "N*GGA", "niggas": "N*GGAS", "damn": "DAMN",
}


def clean_words(words):
    """Tidy whisper words for on-screen captions: no fillers, no stray
    punctuation, profanity masked, every word visible for at least 0.2 s."""
    out = []
    for start, end, text in words:
        raw = text.strip()
        core = raw.strip(".,;:!?\"'()[]…-").lower()
        if not core or core in FILLERS:
            continue
        shown = MASK.get(core, raw.strip(",.;:\"()[]…").upper())
        if raw.endswith(("?", "!")) and not shown.endswith(("?", "!")):
            shown += raw[-1]
        out.append([max(start - 0.06, 0), max(end, start + 0.2), shown,
                    raw.endswith((".", "?", "!"))])
    for a, b in zip(out, out[1:]):      # no overlaps between consecutive words
        a[1] = min(a[1], b[0]) if b[0] > a[0] else a[1]
    return [tuple(w) for w in out]


def _chunks(words):
    """Group words into 1-3 word caption chunks (max ~16 characters)."""
    chunks, cur = [], []
    for w in words:
        nxt = cur + [w]
        text = " ".join(x[2] for x in nxt)
        gap = cur and w[0] - cur[-1][1] > 0.6
        sentence_end = cur and len(cur[-1]) > 3 and cur[-1][3]
        if cur and (len(nxt) > 4 or len(text) > 18 or gap or sentence_end):
            chunks.append(cur)
            cur = [w]
        else:
            cur = nxt
    if cur:
        chunks.append(cur)
    return chunks


def write_ass(path, words, hook, credit, out_dur, layout, hook_until):
    cap_y, hook_y, credit_y = TEXT_Y[layout]
    style = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
             "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
             "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]", style,
        f"Style: Cap,{FONT},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "0,0,0,0,100,100,1,0,1,8,4,5,60,60,0,1",
        f"Style: Hook,{FONT},58,&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,"
        "0,0,0,0,100,100,0,0,3,20,0,5,70,70,0,1",
        f"Style: Credit,{FONT},34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,2,0,1,3,1,5,40,40,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(min(hook_until, out_dur))},Hook,,0,0,0,,"
                     f"{{\\pos(540,{hook_y})\\fad(0,150)}}{_clean(hook.upper())}")
    if credit:
        lines.append(f"Dialogue: 0,{_ts(0)},{_ts(out_dur)},Credit,,0,0,0,,"
                     f"{{\\pos(540,{credit_y})\\alpha&H30&}}{_clean(credit.upper())}")
    chunks = _chunks(words)
    for ci, chunk in enumerate(chunks):
        texts = [_clean(w[2]) for w in chunk]
        nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else out_dur
        end_of_chunk = max(min(chunk[-1][1] + 0.35, nxt, out_dur), chunk[-1][0] + 0.1)
        for k, w in enumerate(chunk):
            start = w[0]
            end = min(chunk[k + 1][0], end_of_chunk) if k + 1 < len(chunk) else end_of_chunk
            if end - start < 0.05:
                continue
            parts = [(f"{{\\c{YELLOW}}}{t}{{\\c&H00FFFFFF&}}" if j == k else t)
                     for j, t in enumerate(texts)]
            pop = "\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)" if k == 0 else ""
            lines.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Cap,,0,0,0,,"
                         f"{{\\pos(540,{cap_y}){pop}}}{' '.join(parts)}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── render ─────────────────────────────────────────────────────────────

def render(src, dst, words, hook, credit, peak=None, facecam=None):
    """src: 16:9 clip. words: [(start, end, text)] in src time. peak: src time of
    the best moment (for the cold-open teaser). Returns (dst, layout)."""
    dst = Path(dst)
    sw, sh, dur = probe(src)
    loud = loudness_10hz(src)
    words = clean_words([w for w in words if w[2].strip() and w[1] <= dur + 0.5])
    segments, teaser = plan_segments(words, loud, dur, peak)
    skip = 1 if teaser else 0
    out_dur = sum(b - a for a, b in segments)

    out_words = []
    if teaser:  # captions inside the teaser too
        a, b = segments[0]
        out_words += [(w[0] - a, w[1] - a, w[2], w[3]) for w in words if a <= w[0] < b]
    for w in words:
        t0 = map_time(w[0], segments, skip)
        if t0 is not None:
            t1 = map_time(min(w[1], dur), segments, skip) or t0 + 0.3
            out_words.append((t0, max(t1, t0 + 0.08), w[2], w[3]))

    layout, params = detect_layout(src, facecam)
    print(f"layout: {layout} {params}")
    ass = dst.with_suffix(".ass")
    write_ass(ass, out_words, hook, credit, out_dur, layout, hook_until=teaser + 2.6)

    parts, labels = [], []
    for i, (a, b) in enumerate(segments):
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS,fps=30,"
                     f"scale={sw}:{sh},setsar=1[v{i}];"
                     f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}];")
        labels.append(f"[v{i}][a{i}]")
    graph = "".join(parts) + f"{''.join(labels)}concat=n={len(segments)}:v=1:a=1[vc][ac];"
    graph += layout_graph(layout, params, sw, sh) + ";"

    bumps = punch_times(loud, segments, skip, out_dur, teaser)
    zoom = "+".join(f"0.07*max(0,1-abs(it-{t:.2f})/0.35)" for t in bumps) or "0"
    flash = (f",drawbox=x=0:y=0:w={W}:h={H}:color=white@0.55:t=fill:"
             f"enable='between(t,{teaser:.2f},{teaser + 0.07:.2f})'") if teaser else ""
    graph += (f"[lay]zoompan=z='1+{zoom}':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
              f"s={W}x{H}:fps=30{flash},"
              f"subtitles=filename='{ass.resolve()}':fontsdir='{FONT_DIR}'[v];"
              f"[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex", graph,
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-profile:v", "high", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", dst])
    ass.unlink(missing_ok=True)
    return dst, layout
