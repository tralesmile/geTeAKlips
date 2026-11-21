"""Turn a 16:9 stream clip into a captioned 1080x1920 vertical video."""
from pathlib import Path

from .common import run

W, H = 1080, 1920
CAM_H = 640  # facecam band height in the split layout


def _ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(path, words, hook, credit, duration, has_cam):
    """words: list of (start, end, text). Captions are shown 3 words at a time."""
    cap_y = CAM_H + 140 if has_cam else 1480
    hook_y = H - 260 if has_cam else 300
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Cap,DejaVu Sans,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1",
        "Style: Hook,DejaVu Sans,62,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,"
        "-1,0,0,0,100,100,0,0,3,18,0,5,80,80,0,1",
        "Style: Credit,DejaVu Sans,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,3,0,5,40,40,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(min(4.5, duration))},Hook,,0,0,0,,"
                     f"{{\\pos(540,{hook_y})}}{_esc(hook.upper())}")
    if credit:
        cy = CAM_H - 40 if has_cam else H - 120
        lines.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration)},Credit,,0,0,0,,"
                     f"{{\\pos(540,{cy})}}{_esc(credit)}")
    for i in range(0, len(words), 3):
        chunk = words[i:i + 3]
        start = chunk[0][0]
        end = words[i + 3][0] if i + 3 < len(words) else chunk[-1][1]
        end = min(end, chunk[-1][1] + 0.6)
        text = _esc(" ".join(w[2].strip() for w in chunk).upper())
        if text:
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,"
                         f"{{\\pos(540,{cap_y})}}{text}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def filter_graph(facecam, zoom, ass_path):
    subs = f"subtitles=filename='{ass_path}'"
    if facecam:
        x, y, w, h = facecam
        game_h = H - CAM_H
        return (
            f"[0:v]split=2[a][b];"
            f"[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
            f"scale={W}:{CAM_H}:force_original_aspect_ratio=increase,crop={W}:{CAM_H}[cam];"
            f"[b]scale=-2:{game_h},crop={W}:{game_h}[game];"
            f"[cam][game]vstack=inputs=2,setsar=1,fps=30,{subs}[v]"
        )
    fg_w = int(W * zoom) // 2 * 2
    return (
        f"[0:v]split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"boxblur=24:2,eq=brightness=-0.12[bg];"
        f"[b]scale={fg_w}:-2,crop={W}:ih[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,{subs}[v]"
    )


def render(src, dst, words, hook, credit, duration, facecam=None, zoom=1.45):
    dst = Path(dst)
    ass = dst.with_suffix(".ass")
    write_ass(ass, words, hook, credit, duration, bool(facecam))
    run(["ffmpeg", "-y", "-v", "error", "-i", src,
         "-filter_complex", filter_graph(facecam, zoom, ass.resolve()),
         "-map", "[v]", "-map", "0:a?", "-t", f"{duration:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst])
    ass.unlink(missing_ok=True)
    return dst
