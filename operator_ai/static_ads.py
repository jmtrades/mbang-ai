"""Static ads: finished 1080x1080 image ads from a brief, each waiting in the approval queue.

With an image AI (an OpenAI key, the AI switched on and budget left) the model paints a text-free background and the
app sets the headline, line and call to action on top, so the words are always sharp and spelled right. Without one,
the app designs the ad itself in the brand's colours. Either way nothing is posted: each ad is a queue item with its
image, approved (or not) by a person.
"""
from __future__ import annotations

import base64
import io
import os
import re
import secrets
import textwrap
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
PALETTES = [("0F172A", "1E293B", "F59E0B"), ("111827", "312E81", "34D399"), ("1F2937", "7C2D12", "FBBF24")]


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


def ai_concepts(tenant, b: dict) -> list[dict] | None:
    """Three angles written by the text AI from the brief; None when it is off or replies with something unusable
    (the template angles are used then)."""
    if llm.unavailable_reason(tenant) is not None:
        return None
    keys = ("business", "niche", "audience", "offer", "outcome", "mechanism", "pain", "timeframe")
    facts = {k: b.get(k) for k in keys}
    try:
        out = llm.complete(tenant, WRITER_SYSTEM, f"Brief: {facts}", purpose="static_ads", max_tokens=1200)
        ads = llm.parse_json(out["text"]).get("ads") or []
    except (llm.AIUnavailable, ValueError, AttributeError):
        return None
    good = []
    for a, angle in zip(ads[:3], ("outcome", "problem", "mechanism"), strict=False):
        if not isinstance(a, dict) or not all(isinstance(a.get(k), str) and a[k].strip() for k in _LIMITS):
            return None
        good.append({"angle": angle, **{k: a[k].strip().replace("**", "")[:n] for k, n in _LIMITS.items()}})
    return good if len(good) == 3 else None


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


def compose(c: dict, brand: str, palette: tuple[str, str, str], background: bytes | None = None) -> bytes:
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
        deco = ImageDraw.Draw(img)
        deco.ellipse([SIZE - 420, -180, SIZE + 180, 420], outline=_rgb(accent), width=6)
        deco.ellipse([SIZE - 330, -90, SIZE + 90, 330], outline=_rgb(accent), width=2)
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


def image_ai_reason(tenant) -> str | None:
    """Why the image AI is off (None when it can be used): it needs an OpenAI key and the AI gate open."""
    if not os.environ.get("OPENAI_API_KEY"):
        return "no image AI key (OPENAI_API_KEY)"
    return llm.unavailable_reason(tenant)


def _paint(tenant, c: dict, b: dict) -> bytes:
    """One text-free background picture from the image model. Raises llm.AIUnavailable when it can't."""
    reason = image_ai_reason(tenant)
    if reason:
        raise llm.AIUnavailable(reason)
    prompt = (f"Photorealistic, premium advertising photograph for {b['niche']}: {c['scene']}. Natural light, shallow "
              "depth of field, modern and aspirational, plenty of calm space in the lower half. Absolutely no text, "
              "letters, numbers, logos or watermarks.")
    body = {"model": os.environ.get("OPERATOR_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": "1024x1024",
            "n": 1}
    try:
        with httpx.Client(transport=llm._transport, timeout=120.0) as client:
            r = client.post("https://api.openai.com/v1/images/generations", json=body,
                            headers={"authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    except httpx.HTTPError as e:
        raise llm.AIUnavailable(f"image AI call failed ({type(e).__name__})") from None
    if r.status_code >= 400:
        raise llm.AIUnavailable(f"image AI error HTTP {r.status_code}")
    item = (r.json().get("data") or [{}])[0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        with httpx.Client(transport=llm._transport, timeout=60.0) as client:
            data = client.get(item["url"]).content
    else:
        raise llm.AIUnavailable("image AI returned no image")
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
    written = ai_concepts(tenant, b)
    for i, c in enumerate(written or concepts(b)):
        background, mode = None, "design"
        if reason is None:
            try:
                background, mode = _paint(tenant, c, b), "ai"
            except llm.AIUnavailable as e:
                reason = e.reason
        png = compose(c, brand, PALETTES[i % len(PALETTES)], background)
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
            "words": "ai" if written else "template",
            "defaults_used": built["missing"]}


def image_path(tenant, name: str) -> Path:
    if not _NAME.match(name or ""):
        raise AdError(404, "Not found")
    p = _dir(tenant) / name
    if not p.exists():
        raise AdError(404, "Not found")
    return p
