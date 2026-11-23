"""Original Serbian meme posts about trending reality-TV news (Zadruga, Pinkove
zvezde...). No TV footage: Claude writes the joke about what the news reports,
and we design the image ourselves. Posted to Instagram via Upload-Post.

Called by clipper/post.py for profiles with `kind: memes`.
"""
import json
import os
import random
import re
import time
import uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont

from .common import ROOT, WORK

HISTORY = ROOT / "state" / "memes.json"
FONT_HEAVY = str(ROOT / "fonts" / "ArchivoBlack-Regular.ttf")
FONT_BODY = str(ROOT / "fonts" / "Montserrat.ttf")
W, H = 1080, 1350          # 4:5, the tallest feed format on Instagram

PALETTES = [  # (top colour, bottom colour, text colour, accent)
    ((255, 94, 58), (255, 42, 104), (255, 255, 255), (255, 230, 80)),
    ((20, 20, 30), (55, 20, 80), (255, 255, 255), (255, 200, 0)),
    ((0, 120, 255), (0, 200, 170), (255, 255, 255), (255, 240, 90)),
    ((255, 200, 0), (255, 120, 0), (20, 20, 20), (255, 255, 255)),
    ((130, 0, 255), (255, 0, 140), (255, 255, 255), (120, 255, 200)),
    ((10, 60, 40), (20, 140, 80), (255, 255, 255), (255, 220, 70)),
]


# ── 1. what's trending ────────────────────────────────────────────────

def headlines(queries, max_age_h=36):
    """Recent Serbian news headlines from Google News RSS."""
    seen, out = set(), []
    for q in queries:
        for lang in ("sr-Latn", "sr"):
            url = (f"https://news.google.com/rss/search?q={quote(q)}+when:2d"
                   f"&hl={lang}&gl=RS&ceid=RS:{lang}")
            try:
                root = ET.fromstring(requests.get(url, timeout=30).content)
            except Exception as e:
                print(f"news {q}/{lang} failed: {e}")
                continue
            for item in root.iter("item"):
                title = (item.findtext("title") or "").rsplit(" - ", 1)[0].strip()
                try:
                    age = (time.time() - parsedate_to_datetime(item.findtext("pubDate")).timestamp()) / 3600
                except Exception:
                    age = 0
                key = title.lower()[:60]
                if title and age <= max_age_h and key not in seen:
                    seen.add(key)
                    out.append((age, title))
            if out:
                break
    return [t for _, t in sorted(out)][:25]


# ── 2. the joke ────────────────────────────────────────────────────────

PROMPT = """Ti si admin popularne srpske Instagram meme stranice "{name}" ({handle}).
Pratioci vole rijaliti (Zadruga, Pinkove zvezde), balkanski humor i jugo-nostalgiju.

Najnoviji naslovi vesti (poslednjih 36h):
{news}

Ranije objave (NE ponavljaj teme ni fore):
{history}
{learn}
Napravi JEDAN originalan, stvarno smešan meme o nečemu iz ovih vesti (izaberi temu
o kojoj ljudi najviše pričaju). Piši srpskim jezikom, LATINICOM, kako mladi pišu
na Instagramu (kratko, žargon dozvoljen). Pravila:
- Satira i zezanje na račun onoga što su vesti objavile. Ne izmišljaj činjenice,
  ne pripisuj stvarnim ljudima izmišljene citate pod navodnicima.
- Bez vređanja izgleda, nacionalnosti, vere, seksualnosti; bez seksualnog sadržaja,
  psovki i optužbi za zločine. Blago peckanje, ne mržnja.
- Najbolje prolaze memeovi sa kojima se ljudi poistovete i koje šalju drugarima.

Izaberi format:
- "tweet": kratka duhovita objava kao tvit (max 200 znakova)
- "big": jedna jaka rečenica/POV/"Kad..." (max 110 znakova), plus kratka oznaka teme
- "list": naslov + 3 kratke stavke (rangiranje, "tipovi ljudi kad...", itd.)

Vrati SAMO JSON:
{{"format": "tweet|big|list", "topic": "tema u 3-5 reči",
  "label": "OZNAKA TEME do 2 reči, npr. ZADRUGA",
  "text": "tekst za tweet/big format",
  "title": "naslov za list format", "items": ["...", "...", "..."],
  "caption": "opis objave 1-2 rečenice + pitanje za komentare",
  "comment": "prvi komentar: kratko pitanje ili anketa (npr. 'Ko je u pravu? 🅰️ ili 🅱️')"}}"""


def write_meme(cfg, news, history):
    m = cfg.get("memes", {})
    learn = ""
    best = [h for h in history if h.get("likes")]
    if best:
        best = sorted(best, key=lambda h: -h["likes"])[:4]
        learn = "\nNajbolje su prošli ovi memeovi (piši u sličnom duhu):\n" + "\n".join(
            f"- {h.get('text') or h.get('title')} ({h['likes']} lajkova)" for h in best) + "\n"
    prompt = PROMPT.format(
        name=m.get("name", "Yugoslovenko"), handle=m.get("handle", "@yugoslovenko"),
        news="\n".join(f"- {t}" for t in news) or "- (nema novih vesti: napravi opšti rijaliti/balkan meme)",
        history="\n".join(f"- {h.get('topic')}: {h.get('text') or h.get('title')}"
                          for h in history[-15:]) or "- (nema)",
        learn=learn)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": cfg.get("llm_model", "claude-sonnet-5-5"), "max_tokens": 1200,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120)
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json()["content"])
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


# ── 3. the image ───────────────────────────────────────────────────────

def _font(path, size, weight=None):
    f = ImageFont.truetype(path, size)
    if weight:
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
    return f


def _wrap(draw, text, font, width):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            test = f"{cur} {word}".strip()
            if draw.textlength(test, font=font) <= width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _fit(draw, text, path, width, height, start, minimum=34, weight=None, spacing=1.18):
    """Largest font size at which text fits the box. Returns (font, lines, line_h)."""
    size = start
    while size >= minimum:
        font = _font(path, size, weight)
        lines = _wrap(draw, text, font, width)
        line_h = int(size * spacing)
        if len(lines) * line_h <= height and all(draw.textlength(l, font=font) <= width for l in lines):
            return font, lines, line_h
        size -= 4
    font = _font(path, minimum, weight)
    return font, _wrap(draw, text, font, width), int(minimum * spacing)


def _gradient(top, bottom):
    img = Image.new("RGB", (W, H), top)
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(W):
            px[x, y] = c
    return img


def _pill(draw, text, xy, fill, color):
    font = _font(FONT_HEAVY, 34)
    tw = draw.textlength(text, font=font)
    x, y = xy
    draw.rounded_rectangle([x - tw / 2 - 26, y - 30, x + tw / 2 + 26, y + 30], 30, fill=fill)
    draw.text((x, y), text, font=font, fill=color, anchor="mm")


def render(meme, cfg, path):
    m = cfg.get("memes", {})
    handle, name = m.get("handle", "@yugoslovenko"), m.get("name", "Yugoslovenko")
    fmt = meme.get("format", "big")
    top, bottom, ink, accent = random.choice(PALETTES)

    if fmt == "tweet":
        dark = random.random() < 0.5
        bg, fg, sub = ((21, 32, 43), (255, 255, 255), (139, 152, 165)) if dark else \
                      ((255, 255, 255), (15, 20, 25), (83, 100, 113))
        img = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(img)
        x0, y0 = 90, 330
        d.ellipse([x0, y0, x0 + 120, y0 + 120], fill=top)
        d.text((x0 + 60, y0 + 62), name[:1].upper(), font=_font(FONT_HEAVY, 64), fill=(255, 255, 255), anchor="mm")
        d.text((x0 + 150, y0 + 22), name, font=_font(FONT_BODY, 46, 800), fill=fg)
        d.text((x0 + 150, y0 + 78), handle, font=_font(FONT_BODY, 38, 500), fill=sub)
        font, lines, lh = _fit(d, meme.get("text", ""), FONT_BODY, W - 180, 560, 64, weight=600, spacing=1.3)
        y = y0 + 190
        for line in lines:
            d.text((x0, y), line, font=font, fill=fg)
            y += lh
        d.text((x0, y + 40), f"{meme.get('label', '').title()} · danas", font=_font(FONT_BODY, 34, 500), fill=sub)
    elif fmt == "list":
        img = _gradient(top, bottom)
        d = ImageDraw.Draw(img)
        _pill(d, meme.get("label", "ZADRUGA").upper()[:20], (W / 2, 130), accent, (20, 20, 20))
        font, lines, lh = _fit(d, meme.get("title", ""), FONT_HEAVY, W - 160, 300, 84, 44)
        y = 230
        for line in lines:
            d.text((W / 2, y), line.upper(), font=font, fill=ink, anchor="ma")
            y += lh
        y += 50
        for i, item in enumerate((meme.get("items") or [])[:3], 1):
            d.rounded_rectangle([80, y, W - 80, y + 230], 34, fill=(255, 255, 255))
            d.text((150, y + 115), str(i), font=_font(FONT_HEAVY, 96), fill=top, anchor="mm")
            f2, l2, lh2 = _fit(d, item, FONT_BODY, W - 360, 190, 52, 30, weight=700)
            yy = y + 115 - len(l2) * lh2 / 2
            for line in l2:
                d.text((230, yy), line, font=f2, fill=(20, 20, 20))
                yy += lh2
            y += 260
    else:  # big
        img = _gradient(top, bottom)
        d = ImageDraw.Draw(img)
        _pill(d, meme.get("label", "ZADRUGA").upper()[:20], (W / 2, 170), accent, (20, 20, 20))
        font, lines, lh = _fit(d, meme.get("text", "").upper(), FONT_HEAVY, W - 160, 800, 120, 52, spacing=1.12)
        y = H / 2 - len(lines) * lh / 2 + 40
        for line in lines:
            d.text((W / 2 + 4, y + 5), line, font=font, fill=(0, 0, 0, 90), anchor="ma")
            d.text((W / 2, y), line, font=font, fill=ink, anchor="ma")
            y += lh
    ImageDraw.Draw(img).text((W / 2, H - 70), handle, font=_font(FONT_BODY, 34, 700),
                             fill=(255, 255, 255) if fmt != "tweet" else (139, 152, 165), anchor="mm")
    img.save(path, "JPEG", quality=93)
    return path


# ── 4. post ────────────────────────────────────────────────────────────

def load_history():
    try:
        return json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return []


def make_and_post(profile, acct, cfg):
    m = cfg.get("memes", {})
    history = load_history()
    news = headlines(m.get("queries", ["Zadruga", "Pinkove zvezde", "rijaliti Pink"]))
    print(f"[{profile}] {len(news)} headlines")
    meme = write_meme(cfg, news, history)
    print(f"[{profile}] meme: {json.dumps(meme, ensure_ascii=False)[:400]}")
    WORK.mkdir(exist_ok=True)
    img = render(meme, cfg, WORK / f"meme_{int(time.time())}.jpg")
    caption = f"{meme.get('caption', '')}\n\n{m.get('hashtags', '')}".strip()
    data = [("user", profile), ("instagram_title", caption[:2200]), ("title", caption[:2200]),
            ("first_comment", meme.get("comment", "Šta mislite? 👇"))]
    data += [("platform[]", p) for p in acct["platforms"]]
    with open(img, "rb") as f:
        r = requests.post("https://api.upload-post.com/api/upload_photos",
                          headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}",
                                   "Idempotency-Key": str(uuid.uuid4())},
                          data=data, files=[("photos[]", (img.name, f, "image/jpeg"))], timeout=300)
    print(f"[{profile}]", r.status_code, r.text[:600])
    if r.status_code >= 300:
        return False
    try:
        request_id = r.json().get("request_id")
    except ValueError:
        request_id = None
    history.append(dict(meme, posted_at=int(time.time()), request_id=request_id))
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=1))
    return True
