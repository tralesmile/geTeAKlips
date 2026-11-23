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

{mode}

Najnoviji naslovi vesti (poslednjih 36h):
{news}

Ranije objave (NE ponavljaj teme ni fore):
{history}
Poslednji formati: {recent_formats}. Izaberi DRUGAČIJI format od poslednja dva.
{learn}
Piši srpskim jezikom, LATINICOM, kako mladi pišu na Instagramu (kratko, žargon
dozvoljen). Pravila:
- Satira i zezanje na račun onoga što su vesti objavile. Ne izmišljaj činjenice,
  ne pripisuj stvarnim ljudima izmišljene citate pod navodnicima.
- Bez vređanja izgleda, nacionalnosti, vere, seksualnosti; bez seksualnog sadržaja,
  psovki i optužbi za zločine. Blago peckanje, ne mržnja.
- Najbolje prolaze memeovi sa kojima se ljudi poistovete i koje šalju drugarima.
- Na slici (text/title/items/slides/chat/label) NE koristi emoji; emoji samo u
  caption i comment.

Formati:
- "tweet": kratka duhovita objava kao tvit (max 200 znakova) -> "text"
- "big": jedna jaka rečenica/POV/"Kad..." (max 110 znakova) -> "text"
- "list": naslov + 3 kratke stavke ("tipovi ljudi kad...", rangiranje) -> "title", "items"
- "carousel": 3-5 slajdova za prevlačenje; prvi slajd je naslov koji tera na
  prevlačenje, ostali po jedna fora (max 90 znakova); npr. "Zadruga ove nedelje u 4 slike"
  ili "Ko je bio u pravu?" (slajd A, slajd B, poslednji: "Glasaj u komentarima") -> "slides"
- "chat": izmišljeni grupni čet sa generičkim imenima (Mama, Ćale, Baba, Drugar, Ja,
  Komšinica...), NIKAD stvarna imena; 3-6 poruka -> "chat": [{{"from": "Mama", "text": "..."}}],
  "label" = ime grupe (npr. "PORODICA")
- "jokes": naslov + 3-5 viceva za prevlačenje -> "title", "jokes": ["vic 1", "vic 2", ...];
  u vicu svaku repliku dijaloga stavi u novi red ("\\n"), npr. "Pita učiteljica Pericu: ...\\nPerica: ..."
- "breaking": lažna "HITNO" vest kao šala o svakodnevnoj situaciji koja se vezuje
  za temu (npr. "HITNO: Srbija stala, Zadruga počinje za 5 minuta") -> "text"
  (očigledno šala, ne lažna vest o stvarnoj osobi)

Vrati SAMO JSON:
{{"format": "tweet|big|list|carousel|chat|breaking|jokes", "topic": "tema u 3-5 reči",
  "label": "OZNAKA do 2 reči, npr. ZADRUGA",
  "text": "...", "title": "...", "items": [], "slides": [], "chat": [], "jokes": [],
  "caption": "opis objave 1-2 rečenice + pitanje za komentare",
  "comment": "prvi komentar: kratko pitanje ili anketa (npr. 'Ko je u pravu? 🅰️ ili 🅱️')"}}"""

MODES = {
    "relatable": "Napravi JEDAN originalan meme o svakodnevnom balkanskom životu sa kojim "
                 "se svi poistovete: porodica (mama, ćale, baba, deda, tetke), slava, svadbe, "
                 "komšije, plata, škola/posao, prevoz, Jugo-nostalgija. Pratiocima su "
                 "NAJBOLJI porodični čet memeovi (format \"chat\"), pa koristi \"chat\" "
                 "u većini slučajeva. NE pravi memeove o rijalitiju (Zadruga, Elita) ni o "
                 "poznatim ličnostima.",
    "vicevi": "Napravi \"jokes\" objavu: 3 do 5 kratkih, stvarno smešnih ORIGINALNIH viceva "
              "u balkanskom duhu (Perica, baba i deda, ćale i sin, komšija, učiteljica, "
              "policajac, doktor, taksista, svekrva...), svaki max 260 znakova, sa jasnom poentom "
              "na kraju. Bez vređanja nacionalnosti, vere i seksualnog sadržaja. Plus naslov "
              "koji tera na prevlačenje (npr. \"5 viceva za danas, treći me ubio\").",
    "reality": "Napravi JEDAN originalan, smešan meme o nečemu iz ovih vesti.",
}


def write_meme(cfg, news, history, mode="reality", force_format=None):
    m = cfg.get("memes", {})
    learn = ""
    best = [h for h in history if h.get("likes")]
    if best:
        best = sorted(best, key=lambda h: -h["likes"])[:4]
        learn = "\nNajbolje su prošli ovi memeovi (piši u sličnom duhu):\n" + "\n".join(
            f"- [{h.get('format')}] {h.get('text') or h.get('title') or (h.get('slides') or [''])[0]}"
            f" ({h['likes']} lajkova)" for h in best) + "\n"
    prompt = PROMPT.format(
        name=m.get("name", "Yugoslovenko"), handle=m.get("handle", "@yugoslovenko"),
        mode=MODES.get(mode, MODES["reality"]),
        news="\n".join(f"- {t}" for t in news) or "- (nema novih vesti)",
        history="\n".join(f"- {h.get('topic')}" for h in history[-20:]) or "- (nema)",
        recent_formats=", ".join(h.get("format", "?") for h in history[-3:]) or "nema",
        learn=learn)
    force_format = force_format or ("jokes" if mode == "vicevi" else None)
    if force_format:
        prompt += f'\n\nOVAJ PUT format MORA biti "{force_format}".'
    for attempt in range(3):
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": cfg.get("llm_model", "claude-sonnet-5-5"), "max_tokens": 3000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180)
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json()["content"])
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                pass
        print(f"meme reply not usable (attempt {attempt + 1}): {text[:600]} | "
              f"raw: {json.dumps(resp.json(), ensure_ascii=False)[:800]}")
    raise RuntimeError("Claude did not return a usable meme")


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


EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]+")


def _plain(text):
    """Image fonts have no emoji glyphs: drop them from on-image text (they stay in
    the caption) and tidy the spaces left behind."""
    return re.sub(r"[ \t]+", " ", EMOJI.sub("", text or "")).strip()


def _footer(img, handle, color=(255, 255, 255)):
    ImageDraw.Draw(img).text((W / 2, H - 70), handle, font=_font(FONT_BODY, 34, 700),
                             fill=color, anchor="mm")


def _big_slide(text, label, pal, handle, counter=None, swipe=False):
    top, bottom, ink, accent = pal
    img = _gradient(top, bottom)
    d = ImageDraw.Draw(img)
    if label:
        _pill(d, label.upper()[:20], (W / 2, 170), accent, (20, 20, 20))
    if counter:
        d.text((W - 90, 170), counter, font=_font(FONT_BODY, 34, 700), fill=ink, anchor="rm")
    font, lines, lh = _fit(d, text.upper(), FONT_HEAVY, W - 160, 780, 120, 52, spacing=1.12)
    y = H / 2 - len(lines) * lh / 2 + 30
    for line in lines:
        d.text((W / 2 + 4, y + 5), line, font=font, fill=(0, 0, 0), anchor="ma")
        d.text((W / 2, y), line, font=font, fill=ink, anchor="ma")
        y += lh
    if swipe:
        d.text((W / 2, H - 150), "PREVUCI  >>>", font=_font(FONT_HEAVY, 44), fill=accent, anchor="mm")
    _footer(img, handle, ink)
    return img


def render(meme, cfg, base):
    """Render the meme; returns a list of JPEG paths (several for a carousel)."""
    meme = dict(meme, **{k: _plain(meme.get(k, "")) for k in ("text", "title", "label")},
                items=[_plain(i) for i in meme.get("items") or []],
                slides=[_plain(x) for x in meme.get("slides") or []],
                chat=[{"from": _plain(c.get("from", "")), "text": _plain(c.get("text", ""))}
                      for c in meme.get("chat") or [] if isinstance(c, dict)],
                jokes=[_plain(j) for j in meme.get("jokes") or [] if isinstance(j, str)])
    m = cfg.get("memes", {})
    handle, name = m.get("handle", "@yugoslovenko"), m.get("name", "Yugoslovenko")
    fmt = meme.get("format", "big")
    pal = random.choice(PALETTES)
    top, bottom, ink, accent = pal
    imgs = []

    if fmt == "jokes" and meme["jokes"]:
        jokes = meme["jokes"][:5]
        n = len(jokes) + 1
        imgs.append(_big_slide(meme.get("title") or f"{len(jokes)} viceva za danas",
                               "VICEVI", pal, handle, counter=f"1/{n}", swipe=True))
        for i, joke in enumerate(jokes, 2):
            img = _gradient(top, bottom)
            d = ImageDraw.Draw(img)
            _pill(d, f"VIC #{i - 1}", (W / 2, 170), accent, (20, 20, 20))
            d.text((W - 90, 170), f"{i}/{n}", font=_font(FONT_BODY, 34, 700), fill=ink, anchor="rm")
            d.rounded_rectangle([70, 270, W - 70, H - 170], 40, fill=(255, 255, 255))
            font, lines, lh = _fit(d, joke, FONT_BODY, W - 240, H - 560, 64, 34, weight=650, spacing=1.32)
            y = (270 + H - 170) / 2 - len(lines) * lh / 2
            for line in lines:
                d.text((120, y), line, font=font, fill=(20, 20, 25))
                y += lh
            _footer(img, handle, ink)
            imgs.append(img)
    elif fmt == "carousel" and len(meme["slides"]) >= 2:
        n = len(meme["slides"][:5])
        for i, text in enumerate(meme["slides"][:5]):
            imgs.append(_big_slide(text, meme.get("label") if i == 0 else "", pal, handle,
                                   counter=f"{i + 1}/{n}", swipe=(i == 0)))
    elif fmt == "chat" and meme["chat"]:
        bg = (17, 27, 33)
        img = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 170], fill=(32, 44, 51))
        d.ellipse([50, 45, 130, 125], fill=top)
        d.text((90, 87), (meme.get("label") or "G")[:1].upper(), font=_font(FONT_HEAVY, 40),
               fill=(255, 255, 255), anchor="mm")
        d.text((160, 58), (meme.get("label") or "Grupa").title()[:26],
               font=_font(FONT_BODY, 44, 700), fill=(233, 237, 239))
        d.text((160, 112), "online", font=_font(FONT_BODY, 30, 500), fill=(134, 150, 160))
        y = 230
        body = _font(FONT_BODY, 40, 600)
        for msg in meme["chat"][:6]:
            mine = msg["from"].lower() in ("ja", "me", "mi")
            lines = _wrap(d, msg["text"], body, 640)
            h = 60 + len(lines) * 52 + (0 if mine else 44)
            x0 = W - 80 - 700 if mine else 80
            fill = (0, 92, 75) if mine else (32, 44, 51)
            d.rounded_rectangle([x0, y, x0 + 700, y + h], 28, fill=fill)
            ty = y + 26
            if not mine:
                d.text((x0 + 30, ty), msg["from"], font=_font(FONT_BODY, 32, 800), fill=accent)
                ty += 44
            for line in lines:
                d.text((x0 + 30, ty), line, font=body, fill=(233, 237, 239))
                ty += 52
            y += h + 26
            if y > H - 200:
                break
        _footer(img, handle, (134, 150, 160))
        imgs.append(img)
    elif fmt == "breaking":
        img = Image.new("RGB", (W, H), (12, 12, 16))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 300, W, 420], fill=(220, 20, 40))
        d.text((80, 360), "HITNO", font=_font(FONT_HEAVY, 80), fill=(255, 255, 255), anchor="lm")
        d.text((W - 80, 360), "UŽIVO", font=_font(FONT_BODY, 40, 800), fill=(255, 255, 255), anchor="rm")
        font, lines, lh = _fit(d, meme.get("text", "").upper(), FONT_HEAVY, W - 160, 560, 96, 48)
        y = 480
        for line in lines:
            d.text((80, y), line, font=font, fill=(255, 255, 255))
            y += lh
        d.rectangle([0, H - 150, W, H - 100], fill=(255, 205, 0))
        d.text((80, H - 125), f"{(meme.get('label') or 'VESTI').upper()}  ·  {handle}",
               font=_font(FONT_BODY, 32, 800), fill=(15, 15, 15), anchor="lm")
        imgs.append(img)
    elif fmt == "tweet":
        dark = random.random() < 0.5
        bg, fg, sub = ((21, 32, 43), (255, 255, 255), (139, 152, 165)) if dark else \
                      ((255, 255, 255), (15, 20, 25), (83, 100, 113))
        img = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(img)
        x0, y0 = 90, 300
        d.ellipse([x0, y0, x0 + 120, y0 + 120], fill=top)
        d.text((x0 + 60, y0 + 62), name[:1].upper(), font=_font(FONT_HEAVY, 64), fill=(255, 255, 255), anchor="mm")
        d.text((x0 + 150, y0 + 22), name, font=_font(FONT_BODY, 46, 800), fill=fg)
        d.text((x0 + 150, y0 + 78), handle, font=_font(FONT_BODY, 38, 500), fill=sub)
        font, lines, lh = _fit(d, meme.get("text", ""), FONT_BODY, W - 180, 620, 72, weight=600, spacing=1.3)
        y = y0 + 190
        for line in lines:
            d.text((x0, y), line, font=font, fill=fg)
            y += lh
        d.text((x0, y + 40), f"{meme.get('label', '').title()} · danas", font=_font(FONT_BODY, 34, 500), fill=sub)
        _footer(img, handle, sub)
        imgs.append(img)
    elif fmt == "list" and meme["items"]:
        img = _gradient(top, bottom)
        d = ImageDraw.Draw(img)
        _pill(d, meme.get("label", "ZADRUGA").upper()[:20], (W / 2, 130), accent, (20, 20, 20))
        font, lines, lh = _fit(d, meme.get("title", ""), FONT_HEAVY, W - 160, 300, 84, 44)
        y = 230
        for line in lines:
            d.text((W / 2, y), line.upper(), font=font, fill=ink, anchor="ma")
            y += lh
        y += 50
        for i, item in enumerate(meme["items"][:3], 1):
            d.rounded_rectangle([80, y, W - 80, y + 230], 34, fill=(255, 255, 255))
            d.text((150, y + 115), str(i), font=_font(FONT_HEAVY, 96), fill=top, anchor="mm")
            f2, l2, lh2 = _fit(d, item, FONT_BODY, W - 360, 190, 52, 30, weight=700)
            yy = y + 115 - len(l2) * lh2 / 2
            for line in l2:
                d.text((230, yy), line, font=f2, fill=(20, 20, 20))
                yy += lh2
            y += 260
        _footer(img, handle, ink)
        imgs.append(img)
    else:  # big (also fallback)
        text = meme.get("text") or meme.get("title") or " ".join(meme["slides"][:1])
        imgs.append(_big_slide(text, meme.get("label"), pal, handle))

    paths = []
    for i, img in enumerate(imgs):
        p = f"{base}_{i}.jpg"
        img.save(p, "JPEG", quality=93)
        paths.append(p)
    return paths


def story_version(path, out):
    """9:16 Story: the meme centred on a blurred, darkened copy of itself."""
    from PIL import ImageFilter, ImageEnhance
    src = Image.open(path).convert("RGB")
    bg = ImageEnhance.Brightness(src.resize((1080, 1920)).filter(ImageFilter.GaussianBlur(40))).enhance(0.55)
    bg.paste(src.resize((1000, 1250)), (40, 335))
    bg.save(out, "JPEG", quality=92)
    return out


# ── 4. post ────────────────────────────────────────────────────────────

HOT_WORDS = ("svađ", "tuč", "izbačen", "diskvalifik", "eliminac", "napusti", "raskid",
             "skandal", "haos", "incident", "fizičk", "pobedni", "finale", "šokira")


def load_history():
    try:
        return json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return []


def pick_mode(cfg):
    """Which kind of meme this slot gets (by local time, see memes.slots)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
    hour = datetime.now(tz).hour
    slots = cfg.get("memes", {}).get("slots", {})
    for t, mode in slots.items():
        h = int(t.split(":")[0])
        if hour == h or (h == 0 and hour == 0):
            return mode
    return "reality"


def is_hot(news):
    """Big reality-TV moment right now? (several fresh headlines about fights etc.)"""
    hits = [t for t in news[:12] if any(w in t.lower() for w in HOT_WORDS)]
    return len(hits) >= 3


def _upload(profile, platforms, photos, caption, comment=None, story=False):
    data = [("user", profile), ("instagram_title", caption[:2200]), ("title", caption[:2200])]
    if comment and not story:
        data.append(("first_comment", comment))
    if story:
        data.append(("media_type", "STORIES"))
    data += [("platform[]", p) for p in platforms]
    handles = [open(p, "rb") for p in photos]
    try:
        r = requests.post("https://api.upload-post.com/api/upload_photos",
                          headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}",
                                   "Idempotency-Key": str(uuid.uuid4())},
                          data=data, timeout=300,
                          files=[("photos[]", (os.path.basename(p), h, "image/jpeg"))
                                 for p, h in zip(photos, handles)])
    finally:
        for h in handles:
            h.close()
    print(f"[{profile}] {'story' if story else 'post'}:", r.status_code, r.text[:400])
    return r


def make_and_post(profile, acct, cfg, mode=None, force_format=None):
    m = cfg.get("memes", {})
    history = load_history()
    mode = mode or pick_mode(cfg)
    news = headlines(m.get("queries", ["Zadruga", "Pinkove zvezde", "rijaliti Pink"]),
                     max_age_h=6 if mode == "bonus" else 36) if mode in ("reality", "bonus") else []
    print(f"[{profile}] mode {mode}, {len(news)} headlines")
    if mode == "bonus" and not is_hot(news):
        print(f"[{profile}] no big reality moment tonight; skipping the bonus meme")
        return False
    meme = write_meme(cfg, news, history, mode, force_format)
    print(f"[{profile}] meme: {json.dumps(meme, ensure_ascii=False)[:500]}")
    WORK.mkdir(exist_ok=True)
    photos = render(meme, cfg, str(WORK / f"meme_{int(time.time())}"))
    caption = f"{meme.get('caption', '')}\n\n{m.get('hashtags', '')}".strip()
    user = acct.get("upload_user", profile)
    r = _upload(user, acct["platforms"], photos, caption, meme.get("comment", "Šta mislite? 👇"))
    if r.status_code >= 300:
        return False
    try:
        request_id = r.json().get("request_id")
    except ValueError:
        request_id = None
    if m.get("story", True):  # same meme to the Story (never blocks the post)
        try:
            _upload(user, ["instagram"], [story_version(photos[0], photos[0] + "_story.jpg")],
                    "", story=True)
        except Exception as e:
            print(f"[{profile}] story failed: {e}")
    history.append(dict(meme, mode=mode, posted_at=int(time.time()), request_id=request_id))
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=1))
    return True


def main():
    """Post one meme now (manual run): env MEME_FORMAT, MEME_MODE, MEME_PROFILE."""
    from .common import load_config
    from .post import profiles
    cfg = load_config()
    name = os.environ.get("MEME_PROFILE") or next(
        k for k, v in profiles(cfg).items() if v["kind"] == "memes")
    acct = profiles(cfg)[name]
    ok = make_and_post(name, acct, cfg, mode=os.environ.get("MEME_MODE") or "reality",
                       force_format=os.environ.get("MEME_FORMAT") or None)
    if not ok:
        raise SystemExit("meme not posted")


if __name__ == "__main__":
    main()
