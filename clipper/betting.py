"""Serbian football tips page: 3 tips a day with a short analysis, built from
today's real fixtures that Claude looks up with web search. Every post carries
an 18+ / play-responsibly note, and the first post of the day shows yesterday's
honest record. Posted to Instagram via Upload-Post.

Called by clipper/post.py for profiles with `kind: betting`.
"""
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw

from .common import ROOT, WORK
from .memes import FONT_BODY, FONT_HEAVY, H, W, _fit, _font, _gradient, _pill, story_version

STATE = ROOT / "state" / "betting.json"
DISCLAIMER = "18+ · Igraj odgovorno · Tip nije garancija"


def _claude(cfg, prompt, searches=8):
    """Claude with live web search; returns the parsed JSON object it answers with."""
    for attempt in range(3):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": cfg.get("llm_model", "claude-sonnet-5-5"), "max_tokens": 4000,
                  "tools": [{"type": "web_search_20250305", "name": "web_search",
                             "max_uses": searches}],
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=300)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
        print(f"betting reply not usable (attempt {attempt + 1}): {text[-500:]}")
    raise RuntimeError("no usable reply")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"tips": []}


TIP_PROMPT = """Danas je {today} ({weekday}), sada je {now} po srednjoevropskom vremenu.
Ti si analitičar srpske Instagram stranice sa fudbalskim tipovima.

Koristi web pretragu da nađeš STVARNE utakmice koje se igraju DANAS i počinju posle
{earliest} (top lige: Engleska, Španija, Italija, Nemačka, Francuska, Liga šampiona,
Liga Evrope, Superliga Srbije...). Ne izmišljaj utakmice, rezultate ni povrede:
koristi samo ono što si našao. Već objavljeni tipovi danas (NE ponavljaj utakmicu):
{used}

Izaberi JEDNU utakmicu gde imaš najjači argument i JEDAN tip, npr. 1, X, 2, 1X, X2,
12, GG, NG, 2+, 3+, 0-2, "1 & 3+", "domaćin daje gol"... Biraj realno (ne preteruj
sa rizikom). Analiza na srpskom, LATINICOM: 3 kratke činjenice (forma, međusobni
dueli, povrede/izostanci, motivacija), svaka max 90 znakova.

Vrati SAMO JSON:
{{"league": "liga", "home": "domaćin", "away": "gost", "kickoff": "HH:MM",
  "tip": "kratak tip, npr. 1 ili 3+ ili GG", "tip_name": "objašnjenje tipa, npr. Pobeda domaćina",
  "confidence": 1-5, "reasons": ["...", "...", "..."],
  "caption": "2-3 rečenice analize za opis objave + pitanje pratiocima (npr. 'Igrate li i vi ovaj tip?')",
  "sources": ["url", "url"]}}"""

RESULT_PROMPT = """Pronađi KONAČNE rezultate ovih fudbalskih utakmica (igrane {day}) i za svaki
tip reci da li je prošao. Koristi web pretragu. Ako ne nađeš rezultat, stavi "unknown".
{tips}
Vrati SAMO JSON: {{"results": [{{"id": "...", "score": "2:1", "won": true|false|"unknown"}}]}}"""


def settle_yesterday(cfg, state, tz):
    """Check yesterday's tips; returns (wins, total) or None."""
    yday = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
    open_tips = [t for t in state["tips"] if t["date"] == yday and "won" not in t]
    if open_tips:
        listing = "\n".join(f'- id {t["id"]}: {t["home"]} - {t["away"]} ({t["league"]}), tip {t["tip"]}'
                            for t in open_tips)
        try:
            res = _claude(cfg, RESULT_PROMPT.format(day=yday, tips=listing), searches=6)
            for r in res.get("results", []):
                for t in open_tips:
                    if t["id"] == str(r.get("id")) and r.get("won") in (True, False):
                        t["won"], t["score"] = r["won"], r.get("score", "")
        except Exception as e:
            print(f"settling failed: {e}")
    done = [t for t in state["tips"] if t["date"] == yday and t.get("won") in (True, False)]
    return (sum(t["won"] for t in done), len(done)) if done else None


def render(tip, record, handle, base):
    pal = random.choice([((15, 23, 42), (30, 64, 120)), ((10, 40, 30), (20, 110, 70)),
                         ((40, 10, 60), (110, 30, 120))])
    img = _gradient(*pal)
    d = ImageDraw.Draw(img)
    gold = (255, 205, 60)
    _pill(d, "TIP DANA", (W / 2, 110), gold, (20, 20, 20))
    d.text((W / 2, 190), f"{tip['league'].upper()} · {tip['kickoff']}", font=_font(FONT_BODY, 36, 700),
           fill=(200, 210, 230), anchor="mm")
    font, lines, lh = _fit(d, f"{tip['home']}\nvs\n{tip['away']}".upper(), FONT_HEAVY, W - 140,
                           330, 84, 44, spacing=1.1)
    y = 250
    for line in lines:
        d.text((W / 2, y), line, font=font, fill=(255, 255, 255), anchor="ma")
        y += lh
    y += 30
    d.rounded_rectangle([W / 2 - 300, y, W / 2 + 300, y + 150], 36, fill=gold)
    tfont, tl, _ = _fit(d, tip["tip"].upper(), FONT_HEAVY, 540, 90, 90, 40)
    d.text((W / 2, y + 52), tl[0], font=tfont, fill=(20, 20, 20), anchor="mm")
    d.text((W / 2, y + 118), tip.get("tip_name", "")[:34], font=_font(FONT_BODY, 30, 700),
           fill=(40, 40, 40), anchor="mm")
    y += 185
    stars = int(tip.get("confidence") or 3)
    label_font = _font(FONT_BODY, 36, 700)
    lw = d.textlength("Sigurnost:", font=label_font)
    x = W / 2 - (lw + 20 + 5 * 44) / 2
    d.text((x, y), "Sigurnost:", font=label_font, fill=gold, anchor="lm")
    x += lw + 20
    for i in range(5):  # 5 dots, filled = confidence
        box = [x + i * 44, y - 15, x + i * 44 + 30, y + 15]
        d.ellipse(box, fill=gold if i < stars else None, outline=gold, width=4)
    y += 50
    for reason in (tip.get("reasons") or [])[:3]:
        f2, l2, lh2 = _fit(d, "• " + reason, FONT_BODY, W - 180, 130, 38, 28, weight=600, spacing=1.25)
        for line in l2:
            d.text((90, y), line, font=f2, fill=(235, 240, 250))
            y += lh2
        y += 14
    if record:
        d.text((W / 2, H - 165), f"Juče: {record[0]}/{record[1]} pogođeno",
               font=_font(FONT_BODY, 36, 800), fill=gold, anchor="mm")
    d.text((W / 2, H - 110), DISCLAIMER, font=_font(FONT_BODY, 30, 700), fill=(200, 210, 230), anchor="mm")
    d.text((W / 2, H - 60), handle, font=_font(FONT_BODY, 32, 700), fill=(255, 255, 255), anchor="mm")
    path = f"{base}.jpg"
    img.save(path, "JPEG", quality=93)
    return path


def make_and_post(profile, acct, cfg):
    b = cfg.get("betting", {})
    tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    used = [t for t in state["tips"] if t["date"] == today]
    record = settle_yesterday(cfg, state, tz) if not used else None
    days = ["ponedeljak", "utorak", "sreda", "četvrtak", "petak", "subota", "nedelja"]
    prompt = TIP_PROMPT.format(
        today=today, weekday=days[now.weekday()], now=now.strftime("%H:%M"),
        earliest=(now + timedelta(minutes=60)).strftime("%H:%M"),
        used="\n".join(f"- {t['home']} - {t['away']}" for t in used) or "- (nema)")
    tip = _claude(cfg, prompt)
    print(f"[{profile}] tip: {json.dumps(tip, ensure_ascii=False)[:500]}")
    WORK.mkdir(exist_ok=True)
    img = render(tip, record, b.get("handle", ""), str(WORK / f"tip_{int(time.time())}"))
    lines = [f"⚽ {tip['home']} - {tip['away']} ({tip['league']}, {tip['kickoff']})",
             f"🎯 Tip: {tip['tip']} ({tip.get('tip_name', '')})", "", tip.get("caption", "")]
    if record:
        lines += ["", f"📊 Juče: {record[0]}/{record[1]} pogođenih tipova"]
    lines += ["", f"🔞 {DISCLAIMER}. Kladi se samo onoliko koliko možeš da izgubiš.",
              "", b.get("hashtags", "")]
    caption = "\n".join(lines).strip()
    data = [("user", acct.get("upload_user", profile)), ("instagram_title", caption[:2200]),
            ("title", caption[:2200]),
            ("first_comment", "Koji je vaš tip za ovu utakmicu? 👇")]
    data += [("platform[]", p) for p in acct["platforms"]]
    with open(img, "rb") as f:
        r = requests.post("https://api.upload-post.com/api/upload_photos",
                          headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}",
                                   "Idempotency-Key": str(uuid.uuid4())},
                          data=data, files=[("photos[]", (os.path.basename(img), f, "image/jpeg"))],
                          timeout=300)
    print(f"[{profile}] post:", r.status_code, r.text[:400])
    if r.status_code >= 300:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        return False
    if b.get("story", True):
        try:
            story = story_version(img, img + "_story.jpg")
            with open(story, "rb") as f:
                requests.post("https://api.upload-post.com/api/upload_photos",
                              headers={"Authorization": f"Apikey {os.environ['UPLOAD_POST_API_KEY']}"},
                              data=[("user", acct.get("upload_user", profile)), ("media_type", "STORIES"),
                                    ("platform[]", "instagram")],
                              files=[("photos[]", ("story.jpg", f, "image/jpeg"))], timeout=300)
        except Exception as e:
            print(f"story failed: {e}")
    state["tips"].append(dict(tip, id=str(int(time.time())), date=today))
    state["tips"] = state["tips"][-300:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    return True


def main():
    from .common import load_config
    from .post import profiles
    cfg = load_config()
    name = next(k for k, v in profiles(cfg).items() if v["kind"] == "betting")
    if not make_and_post(name, profiles(cfg)[name], cfg):
        raise SystemExit("tip not posted")


if __name__ == "__main__":
    main()
