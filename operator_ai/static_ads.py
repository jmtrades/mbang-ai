"""Static ads: finished 1080x1080 image ads from a brief, each waiting in the approval queue.

With an image AI (an OpenAI key, the AI switched on and budget left) the model paints a text-free background and the
app sets the headline, line and call to action on top, so the words are always sharp and spelled right. Without one,
the app designs the ad itself in the brand's colours. Either way nothing is posted: each ad is a queue item with its
image, approved (or not) by a person.
"""
from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import secrets
import textwrap
import time
from pathlib import Path

import httpx

from . import brief as brief_mod
from . import config, llm, queue

KIND = "static_ad"
SIZE = 1080
IMAGE_TOKENS = 2500  # what one generated image counts against the AI budget
_NAME = re.compile(r"^[a-z0-9]{8,40}\.png$")
FONT_BOLD = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]
FONT_REG = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"]
# three looks, so a test has real variety: (background top, background bottom, accent)
PALETTES = [("0F172A", "1E293B", "F59E0B"), ("111827", "312E81", "34D399"), ("1F2937", "7C2D12", "FBBF24"),
            ("052E16", "14532D", "A3E635"), ("1E1B4B", "581C87", "F472B6"), ("0C4A6E", "082F49", "38BDF8"),
            ("3F0D12", "1C1917", "FB923C"), ("18181B", "27272A", "FDE047")]


class AdError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _yours(text: str) -> str:
    """Speak to the reader: 'blowing up their account' -> 'blowing up your account'."""
    for a, z in (("their", "your"), ("they", "you"), ("them", "you"), ("themselves", "yourself")):
        text = re.sub(rf"\b{a}\b", z, text)
    return text


def concepts(b: dict) -> list[dict]:
    """Three angles from the brief, each a headline, one supporting line and a call to action."""
    o, aud, mech, pain, tf = b["outcome"], b["audience"], b["mechanism"], b["pain"], b["timeframe"]
    yo = _yours(o)
    cap = yo[:1].upper() + yo[1:]
    return [
        {"angle": "outcome", "headline": f"{cap} in {tf}.", "line": f"For {aud}, without {pain}.",
         "cta": "Book a free call", "scene": f"{aud} enjoying {o}, calm and confident"},
        {"angle": "problem", "headline": f"Tired of {_yours(pain)}?", "line": f"Our {mech} shows {aud} a better way.",
         "cta": "See how it works",
         "scene": f"{aud} at the moment they decide to change, determined"},
        {"angle": "mechanism", "headline": f"The {mech}.", "line": f"How {aud} get {o}, step by step.",
         "cta": "Get the free guide", "scene": f"a clean, premium workspace that suggests {o} for {aud}"},
    ]


WRITER_SYSTEM = (
    "You write static image ads for a high-ticket coaching or info business. Return ONLY JSON: {\"ads\": [3 objects "
    "with keys angle, headline, line, cta, scene]}. angle is outcome, problem and mechanism, in that order. headline: "
    "max 8 words, speaks to the reader as 'you', punchy, no hype words. line: max 14 words, plain and specific. "
    "cta: max 4 words. scene: one sentence describing a photo with no text in it. No income claims, guarantees or "
    "fake results. Use only the brief's facts."
)
_LIMITS = {"headline": 70, "line": 120, "cta": 28, "scene": 240}


def ai_concepts(tenant, b: dict) -> tuple[list[dict] | None, str | None]:
    """Three angles written by the text AI from the brief, as (concepts, None); (None, why) when it is off or its reply
    can't be used, and the template angles are used then. Forgiving about shape: a bare list, any key case, and a
    missing scene or call to action is filled from the template for that angle."""
    reason = llm.unavailable_reason(tenant)
    if reason is not None:
        return None, reason
    keys = ("business", "niche", "audience", "offer", "outcome", "mechanism", "pain", "timeframe")
    facts = {k: b.get(k) for k in keys}
    try:
        out = llm.complete(tenant, WRITER_SYSTEM, f"Brief: {facts}", purpose="static_ads", max_tokens=1500,
                           timeout=45.0)
    except llm.AIUnavailable as e:
        return None, e.reason
    text = out["text"].strip()
    try:
        start = min(i for i in (text.find("{"), text.find("[")) if i >= 0)
        data = json.loads(text[start:max(text.rfind("}"), text.rfind("]")) + 1])
    except (ValueError, json.JSONDecodeError):
        return None, "the AI's reply wasn't in the expected format"
    if isinstance(data, dict):
        data = data.get("ads") or data.get("Ads") or next((v for v in data.values() if isinstance(v, list)), [])
    rows = [{str(k).lower(): v for k, v in a.items()} for a in (data or []) if isinstance(a, dict)]
    if len(rows) < 3:
        return None, f"the AI wrote {len(rows)} ads instead of 3"
    base = concepts(b)
    good = []
    for row, tpl in zip(rows[:3], base, strict=False):
        c = dict(tpl)
        for k, n in _LIMITS.items():
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                c[k] = v.strip().replace("**", "")[:n]
        if c["headline"] == tpl["headline"]:
            return None, "the AI's reply had no headline"
        good.append(c)
    return good, None


def _font(paths: list[str], size: int):
    from PIL import ImageFont

    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _gradient(top: str, bottom: str):
    from PIL import Image, ImageDraw

    a, z = _rgb(top), _rgb(bottom)
    img = Image.new("RGB", (SIZE, SIZE))
    d = ImageDraw.Draw(img)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        d.line([(0, y), (SIZE, y)], fill=(int(a[0] + (z[0] - a[0]) * t), int(a[1] + (z[1] - a[1]) * t),
                                         int(a[2] + (z[2] - a[2]) * t)))
    return img


def _wrap(draw, text: str, font, width: int) -> list[str]:
    for chars in range(28, 8, -1):
        lines = textwrap.wrap(text, chars)
        if all(draw.textlength(ln, font=font) <= width for ln in lines):
            return lines
    return textwrap.wrap(text, 10)


def _decorate(d, deco: int, acc: tuple[int, int, int]) -> None:
    """One of four background motifs, so designed ads don't all look alike."""
    if deco == 0:  # rings
        d.ellipse([SIZE - 420, -180, SIZE + 180, 420], outline=acc, width=6)
        d.ellipse([SIZE - 330, -90, SIZE + 90, 330], outline=acc, width=2)
    elif deco == 1:  # diagonal band
        d.polygon([(SIZE - 360, 0), (SIZE - 250, 0), (SIZE, 250), (SIZE, 360)], fill=acc)
    elif deco == 2:  # dot grid
        for gx in range(SIZE - 330, SIZE - 40, 48):
            for gy in range(60, 360, 48):
                d.ellipse([gx, gy, gx + 9, gy + 9], fill=acc)
    else:  # corner block with a bar
        d.rectangle([SIZE - 240, 0, SIZE, 240], outline=acc, width=6)
        d.rectangle([SIZE - 170, 70, SIZE - 70, 82], fill=acc)


def compose(c: dict, brand: str, palette: tuple[str, str, str], background: bytes | None = None,
            deco: int = 0) -> bytes:
    """The finished ad as PNG bytes: background (AI picture or designed gradient), headline, line, CTA, brand."""
    from PIL import Image, ImageDraw

    top, bottom, accent = palette
    if background:
        bg = Image.open(io.BytesIO(background)).convert("RGB").resize((SIZE, SIZE))
        shade = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        start = SIZE // 6
        for y in range(SIZE):  # a light veil everywhere, much darker below, so the words read on any picture
            alpha = 80 if y < start else 80 + int(160 * (y - start) / (SIZE - start))
            sd.line([(0, y), (SIZE, y)], fill=(8, 12, 24, alpha))
        img = Image.alpha_composite(bg.convert("RGBA"), shade).convert("RGB")
    else:
        img = _gradient(top, bottom)
        _decorate(ImageDraw.Draw(img), deco, _rgb(accent))
    d = ImageDraw.Draw(img)
    pad = 80
    d.rectangle([pad, pad, pad + 70, pad + 8], fill=_rgb(accent))
    d.text((pad, pad + 26), brand.upper(), font=_font(FONT_BOLD, 30), fill=(226, 232, 240))
    head_font = _font(FONT_BOLD, 92)
    lines = _wrap(d, c["headline"], head_font, SIZE - 2 * pad)
    while len(lines) > 4 and head_font.size > 56:
        head_font = _font(FONT_BOLD, head_font.size - 8)
        lines = _wrap(d, c["headline"], head_font, SIZE - 2 * pad)
    sub_font, cta_font = _font(FONT_REG, 40), _font(FONT_BOLD, 38)
    sub = _wrap(d, c["line"], sub_font, SIZE - 2 * pad)[:3]
    line_h, sub_h = int(head_font.size * 1.12), 52
    y = SIZE - pad - 96 - 40 - len(sub) * sub_h - 30 - len(lines) * line_h
    for ln in lines:
        d.text((pad, y), ln, font=head_font, fill=(255, 255, 255))
        y += line_h
    y += 30
    for ln in sub:
        d.text((pad, y), ln, font=sub_font, fill=(203, 213, 225))
        y += sub_h
    y += 40
    w = int(d.textlength(c["cta"], font=cta_font)) + 80
    d.rounded_rectangle([pad, y, pad + w, y + 96], radius=48, fill=_rgb(accent))
    d.text((pad + 40, y + 26), c["cta"], font=cta_font, fill=_rgb(top))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


STYLES = ["editorial magazine photography", "cinematic, moody lighting", "bright lifestyle photography",
          "minimal studio shot with bold colour", "documentary, candid and real", "luxury brand campaign look"]


def _hf_key() -> tuple[str, str] | None:
    kid, sec = os.environ.get("HF_API_KEY_ID", ""), os.environ.get("HF_API_KEY_SECRET", "")
    if not (kid and sec) and ":" in os.environ.get("HIGGSFIELD_API_KEY", ""):
        kid, sec = os.environ["HIGGSFIELD_API_KEY"].split(":", 1)
    return (kid.strip(), sec.strip()) if kid.strip() and sec.strip() else None


def image_provider() -> str | None:
    """Which image AI paints backgrounds: Higgsfield when its key is set, else OpenAI, else none."""
    want = os.environ.get("OPERATOR_IMAGE_PROVIDER", "").lower()
    if want in ("higgsfield", "openai"):
        return want if (want == "openai" and os.environ.get("OPENAI_API_KEY")) or (want == "higgsfield" and _hf_key()) \
            else None
    return "higgsfield" if _hf_key() else "openai" if os.environ.get("OPENAI_API_KEY") else None


def image_ai_reason(tenant) -> str | None:
    """Why the image AI is off (None when it can be used): it needs a Higgsfield or OpenAI key and the AI gate open."""
    if image_provider() is None:
        return "no image AI key (Higgsfield or OpenAI)"
    return llm.unavailable_reason(tenant)


def _prompt(c: dict, b: dict, style: str) -> str:
    return (f"Photorealistic advertising photograph for {b['niche']}: {c['scene']}. Style: {style}. Modern and "
            "aspirational, with calm space in the lower half. Absolutely no text, letters, numbers, logos or "
            "watermarks.")


def _openai(prompt: str) -> bytes:
    body = {"model": os.environ.get("OPERATOR_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": "1024x1024",
            "n": 1}
    with httpx.Client(transport=llm._transport, timeout=120.0) as client:
        r = client.post("https://api.openai.com/v1/images/generations", json=body,
                        headers={"authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        if r.status_code >= 400:
            raise llm.AIUnavailable(f"OpenAI image error HTTP {r.status_code}")
        item = (r.json().get("data") or [{}])[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            return client.get(item["url"]).content
    raise llm.AIUnavailable("OpenAI returned no image")


HF_BASE = "https://api.higgsfield.ai"


def _higgsfield(prompt: str, wait: float = 150.0) -> bytes:
    key = _hf_key()
    if not key:
        raise llm.AIUnavailable("no Higgsfield key")
    head = {"Authorization": f"Key {key[0]}:{key[1]}", "Content-Type": "application/json",
            "Idempotency-Key": secrets.token_hex(12)}
    path = os.environ.get("HIGGSFIELD_IMAGE_PATH", "/higgsfield-ai/soul/v2/standard")
    with httpx.Client(transport=llm._transport, timeout=60.0) as client:
        r = client.post(HF_BASE + path, json={"prompt": prompt, "aspect_ratio": "1:1"}, headers=head)
        if r.status_code >= 400:
            raise llm.AIUnavailable(f"Higgsfield error HTTP {r.status_code}")
        job = r.json()
        status_url = job.get("status_url") or f"{HF_BASE}/requests/{job.get('request_id')}/status"
        deadline = time.monotonic() + wait
        while True:
            if job.get("status") == "completed":
                imgs = job.get("images") or []
                if not imgs or not imgs[0].get("url"):
                    raise llm.AIUnavailable("Higgsfield returned no image")
                return client.get(imgs[0]["url"]).content
            if job.get("status") in ("failed", "nsfw", "canceled"):
                raise llm.AIUnavailable(f"Higgsfield job {job.get('status')}")
            if time.monotonic() > deadline:
                raise llm.AIUnavailable("Higgsfield took too long")
            time.sleep(2.0)
            g = client.get(status_url, headers={"Authorization": head["Authorization"]})
            if g.status_code >= 400:
                raise llm.AIUnavailable(f"Higgsfield status error HTTP {g.status_code}")
            job = g.json()


def _paint(tenant, c: dict, b: dict) -> bytes:
    """One text-free background picture from the image AI (a different photographic style each time). Raises
    llm.AIUnavailable when it can't."""
    reason = image_ai_reason(tenant)
    if reason:
        raise llm.AIUnavailable(reason)
    prompt = _prompt(c, b, random.choice(STYLES))
    try:
        data = _higgsfield(prompt) if image_provider() == "higgsfield" else _openai(prompt)
    except httpx.HTTPError as e:
        raise llm.AIUnavailable(f"image AI call failed ({type(e).__name__})") from None
    llm._charge(tenant, "ai_token_budget", IMAGE_TOKENS)
    return data


def _dir(tenant) -> Path:
    d = Path(tenant.dir) / "static_ads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make(tenant, partial: dict | None = None, user: dict | None = None) -> dict:
    """Three finished static ads from one brief, each queued for approval with its image."""
    built = brief_mod.build(tenant, partial or {})
    b = built["brief"]
    brand = str(b.get("business") or config.get().brand)
    ads, reason = [], None
    written, words_note = ai_concepts(tenant, b)
    looks, decos = random.sample(PALETTES, 3), random.sample(range(4), 3)  # a fresh look every batch
    for i, c in enumerate(written or concepts(b)):
        background, mode = None, "design"
        if reason is None:
            try:
                background, mode = _paint(tenant, c, b), "ai"
            except llm.AIUnavailable as e:
                reason = e.reason
        png = compose(c, brand, looks[i], background, deco=decos[i])
        name = f"{secrets.token_hex(8)}.png"
        (_dir(tenant) / name).write_bytes(png)
        url = f"/t/{tenant.id}/api/static-ads/{name}"
        caption = f"{c['headline']}\n{c['line']}\n{c['cta']}"
        title = f"[Static ad] {c['angle'].title()}: {c['headline']}"[:200]
        item = queue.add(tenant, KIND, caption, channel="manual", title=title,
                         meta={"image": url, "client_asset": True, "angle": c["angle"], "mode": mode},
                         actor=(user or {}).get("username", "system"))
        ads.append({"queue_id": item["id"], "status": item["status"], "image": url, "angle": c["angle"],
                    "headline": c["headline"], "mode": mode})
    return {"ads": ads, "queue_ids": [a["queue_id"] for a in ads], "image_ai": reason is None, "note": reason,
            "image_provider": image_provider() if reason is None else None,
            "words": "ai" if written else "template", "words_note": words_note,
            "defaults_used": built["missing"]}


def image_path(tenant, name: str) -> Path:
    if not _NAME.match(name or ""):
        raise AdError(404, "Not found")
    p = _dir(tenant) / name
    if not p.exists():
        raise AdError(404, "Not found")
    return p
