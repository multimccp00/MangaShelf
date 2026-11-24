"""Unofficial machine translation of raw (ja/ko/zh) chapters.

Pipeline per page: RapidOCR finds text lines -> lines are grouped into bubbles ->
the bubbles' text goes to a local Ollama model in chapter-sized batches (with the
previous lines as context, so names and tone carry over) -> the result is saved as
TEXT ONLY next to the images:

    <chapter>/translation.en.json
        {"model", "source_lang", "created",
         "pages": [{"file": "001.jpg", "w": W, "h": H,
                    "boxes": [{"x", "y", "w", "h", "src", "en"}]}]}

Images are never modified; the reader overlays the English on top and can toggle
it off. Jobs run on their own queue and, unless started with "now", only while the
PC is idle (no keyboard/mouse input for a while), pausing between pages when the
user comes back.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

TRANSLATION_FILE = "translation.en.json"
CHAPTER_INFO = "chapter.json"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

OLLAMA = os.environ.get("MANGASHELF_OLLAMA", "http://127.0.0.1:11434")
DEFAULT_MODEL = "gemma3:12b"
# Used when the main model refuses or returns broken JSON. Overridden by
# "fallback_model" in ~/.mangashelf/mt_local.json (see _local below).
FALLBACK_MODEL = "qwen2.5:14b"
LANG_NAMES = {"ja": "Japanese", "ko": "Korean", "zh": "Chinese", "zh-hk": "Chinese (Traditional)"}
RAW_LANGS = {"ja", "ko", "zh", "zh-hk", "ja-ro", "ko-ro", "zh-ro"}
_BATCH = 40          # bubbles per LLM call
_CONTEXT_LINES = 20  # previously translated lines sent along as context


# Version of the translation pipeline; files written by an older one get redone.
PIPELINE_VERSION = 5

_TITLE_LANG_RE = re.compile(r"(?:^|[\[\(\s_-])(japanese|chinese|korean|english)(?:[\]\)\s_-]|$)", re.I)
SERIES_LANG = {"jp": "ja", "ja": "ja", "japanese": "ja", "cn": "zh", "zh": "zh", "chinese": "zh",
               "kr": "ko", "ko": "ko", "korean": "ko", "pt": "pt", "br": "pt-br", "pt-br": "pt-br",
               "en": "en", "english": "en"}


def language_from_title(title: str) -> str:
    """Folder names often state their language: "[Japanese]", "Series-Japanese-Title"."""
    m = _TITLE_LANG_RE.search(title or "")
    return {"japanese": "ja", "chinese": "zh", "korean": "ko", "english": "en"}[m.group(1).lower()] if m else ""


def series_language(title: str, column: str) -> str:
    """The name wins over the library's language column, which is sometimes wrong."""
    return language_from_title(title) or SERIES_LANG.get((column or "").strip().lower(), "")


def base_lang(lang: str) -> str:
    lang = (lang or "").lower()
    if lang.startswith("zh"):
        return "zh-hk" if lang == "zh-hk" else "zh"
    return lang.split("-")[0]


# --------------------------------------------------------------------- OCR --
_OCR_LOCK = threading.Lock()
_OCR: dict[str, Any] = {}


def ocr_available() -> bool:
    try:
        import rapidocr  # noqa: F401
        return True
    except Exception:
        return False


def _engine(lang: str):
    """RapidOCR instance per language (models download once on first use)."""
    import logging
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    lang = base_lang(lang)
    if lang not in _OCR:
        if lang == "ko":   # PP-OCRv6 small has no Korean recognizer; v5 mobile does
            params = {"Rec.lang_type": LangRec.KOREAN, "Rec.ocr_version": OCRVersion.PPOCRV5,
                      "Rec.model_type": ModelType.MOBILE}
        elif lang == "ja":
            params = {"Rec.lang_type": LangRec.JAPAN}
        elif lang == "zh-hk":
            params = {"Rec.lang_type": LangRec.CHINESE_CHT}
        else:
            params = {"Rec.lang_type": LangRec.CH}
        _OCR[lang] = RapidOCR(params=params)
    return _OCR[lang]


def _ocr_lines(img, lang: str, y_off: int = 0) -> list[dict]:
    r = _engine(lang)(img)
    out = []
    for box, txt, score in zip(r.boxes if r.boxes is not None else [], r.txts or [], r.scores or []):
        txt = (txt or "").strip()
        if not txt or score < 0.5:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({"x0": float(min(xs)), "y0": float(min(ys)) + y_off,
                    "x1": float(max(xs)), "y1": float(max(ys)) + y_off, "t": txt})
    return out


def _clean_line(t: str, lang: str) -> str:
    """Drop OCR debris from a CJK line: stray Latin letters/digits read out of
    linework or screentone ("はっはっ7", "XおViあ"). Lines with no CJK at all
    (English signs, titles) are left alone."""
    if base_lang(lang) in ("ja", "zh", "zh-hk", "ko") and _CJK.search(t):
        t = re.sub(r"[A-Za-z0-9]+", "", t).strip()
    return t


_KANA = re.compile(r"[぀-ゟ゠-ヺ]")


def _is_junk(text: str, lang: str, in_bubble: bool) -> bool:
    """A group that is OCR noise, not text: no letters left at all ("ー", "!!"),
    or - for Japanese loose on the art - one to three kanji with no kana and no
    punctuation ("武", "无后"): the Chinese-trained recogniser reading hatching.
    Bubbles keep short kanji lines ("数分後" = "a few minutes later")."""
    letters = re.sub(r"[\W\d_ー・]", "", text)
    if not letters:
        return True
    # Simplified-Chinese-only glyphs never occur in Japanese: on a Japanese page
    # they are the Chinese-trained recogniser hallucinating ("无业", "世川饼").
    if base_lang(lang) == "ja" and not _KANA.search(text) and re.search(r"[无业饼后们这说时为东车门见长马鸟书开个对样]", text):
        return True
    if (not in_bubble and base_lang(lang) == "ja" and not _KANA.search(text)
            and len(letters) <= 2 and not re.search(r"[?!？！…♡♥]", text)):
        return True
    return False


def _region_labels(arr, lines: list[dict]):
    """Tag each line with the white region it sits in (line["region"]).

    A speech bubble is a white area closed off by its outline, so lines in two
    different bubbles get different labels even when the bubbles touch — that's
    what stops neighbouring bubbles on a busy page from being merged into one.
    Text on screentone/art, or loose on the big page background, gets None and
    falls back to proximity-only grouping. Returns (labels, stats) for the
    bubble shapes, or None when skipped."""
    import cv2
    import numpy as np
    h, w = arr.shape[:2]
    if h * w > 20_000_000:          # huge webtoon strip: skip (memory), proximity only
        return None
    white = (cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) > 200).astype(np.uint8)
    _n, labels, stats, _c = cv2.connectedComponentsWithStats(white, connectivity=4)
    areas = stats[:, cv2.CC_STAT_AREA]
    near_white = arr.min(axis=2) > 225
    paper: dict[int, bool] = {}
    for ln in lines:
        x0, y0 = max(0, int(ln["x0"])), max(0, int(ln["y0"]))
        x1, y1 = min(w, int(ln["x1"]) + 1), min(h, int(ln["y1"]) + 1)
        sub = labels[y0:y1, x0:x1]
        ch = max(1.0, min(x1 - x0, y1 - y0))
        # Paper-white behind the text (a caption box), not light skin/art: every
        # channel high, so pale skin tones (blue channel ~200) don't count.
        nw = near_white[y0:y1, x0:x1]
        ln["light"] = bool(nw.mean() > 0.5) if nw.size else False
        # Dialogue is black/grey ink; red/pink lettering is a drawn sound effect,
        # even when it sits on something white (hair, a sheet).
        px = arr[y0:y1, x0:x1].reshape(-1, 3).astype(np.int16)
        ink = px[px.min(axis=1) < 160]
        ln["colored"] = bool(ink.size and (ink.max(axis=1) - ink.min(axis=1)).mean() > 60)
        # Is the light background right around the letters neutral paper (a
        # bubble whose white leaks into the page) or tinted (hair, skin)?
        bg = px[px.min(axis=1) > 200]
        ln["local_paper"] = bool(bg.size and (bg.max(axis=1) - bg.min(axis=1)).mean() < 20)
        vals = sub[sub > 0]
        vals = vals[areas[vals] >= 4 * ch * ch]     # ignore the holes inside characters
        if vals.size < 0.1 * max(1, sub.size):
            continue                                 # mostly dark: text over art/tone
        lab = int(np.bincount(vals).argmax())
        if areas[lab] < 0.4 * h * w:                 # a bubble, not the page background
            ln["region"] = lab
            if lab not in paper:
                # Bubble paper is neutral white; white hair/skin/cloth is shaded
                # and tinted (measured: bubbles <= 17, hair/skin >= 22).
                x, y, rw, rh = (int(v) for v in stats[lab][:4])
                m = labels[y:y + rh, x:x + rw] == lab
                px = arr[y:y + rh, x:x + rw][m].astype(np.int16)
                paper[lab] = bool((px.max(axis=1) - px.min(axis=1)).mean() < 20)
            ln["paper"] = paper[lab]
    # Paper for growing boxes sideways: the same grey>200 test as the regions
    # (scanned black-and-white paper is greyer than the strict near_white).
    return labels, stats, white.astype(bool)


def _light_span(near_white, x0: int, y0: int, x1: int, y1: int, page_w: int,
                labels=None, region=None) -> list[int]:
    """Grow the text box left/right while the strip beside it stays paper-white:
    finds the usable width of a bubble/caption whose outline couldn't be traced,
    so the English gets room without spilling onto the art. With the text's own
    white region known, grow through that region instead: neighbouring columns
    of text (holes in it) don't stop it, the bubble outline does."""
    def white(c):
        if labels is not None and region is not None:
            return (labels[y0:y1, c] == region).mean() > 0.6
        return near_white[y0:y1, c].mean() > 0.85
    limit = max(x1 - x0, int((y1 - y0) * 0.6))
    left, right = x0, x1
    while left > 0 and x0 - left < limit and white(left - 1):
        left -= 1
    while right < page_w and right - x1 < limit and white(right):
        right += 1
    return [left, y0, right - left, y1 - y0]


def _bubble_shape(geo, lab: int, tw: float, th: float, page_area: float):
    """Outline (polygon, image px) + bounding box of white region `lab`, if it
    looks like a bubble/caption for this text: not much bigger than the text it
    holds, and not a whole white panel (painting that would wipe out art)."""
    import cv2
    import numpy as np
    labels, stats, _nw = geo
    x, y, w, h, area = (int(v) for v in stats[lab][:5])
    if area > 12 * max(tw * th, 1.0) or w * h > 0.25 * page_area:
        return None
    # A bubble hugs its text: measured real ones stay within 3.5x the text's width
    # and 2.5x its height (+ margin for one-word lines). A bubble merged with
    # white hair/skin (black-and-white pages) is 3-4x taller: painting it would
    # erase the drawing.
    if w > 3.5 * tw + 60 or h > 2.5 * th + 60:
        return None
    mask = (labels[y:y + h, x:x + w] == lab).astype(np.uint8)
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    # A bubble is white inside apart from its text; a white panel or background
    # wraps around art. Painting the outline of the latter would erase the art.
    if area < 0.75 * cv2.contourArea(c):
        return None
    # ...and roughly convex: white background flowing around a character is
    # solid but wraps the art, and its bounding box would put the English on it.
    # Measured: bubbles (tails, joined pairs) 0.73-0.99, background around art 0.32-0.66.
    if cv2.contourArea(c) < 0.7 * cv2.contourArea(cv2.convexHull(c)):
        return None
    c = cv2.approxPolyDP(c, 0.006 * cv2.arcLength(c, True), True)
    return [[int(p[0][0]) + x, int(p[0][1]) + y] for p in c], [x, y, w, h]


def _group(lines: list[dict], lang: str, geo=None, page_area: float = 0) -> list[dict]:
    """Merge OCR lines that sit next to each other into bubbles (union-find on
    slightly grown boxes, never across two different bubble regions), then order
    the lines inside each bubble and the bubbles on the page the way the script
    reads (vertical columns right-to-left)."""
    n = len(lines)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def size(a):  # character size: column width (vertical) or line height
        return max(1.0, min(a["x1"] - a["x0"], a["y1"] - a["y0"]))

    for i in range(n):
        for j in range(i + 1, n):
            a, b = lines[i], lines[j]
            ra, rb = a.get("region"), b.get("region")
            if ra is not None and rb is not None and ra != rb:
                continue                    # two separate bubbles, however close
            m = 0.6 * min(size(a), size(b))
            if (a["x0"] - m < b["x1"] and b["x0"] - m < a["x1"]
                    and a["y0"] - m < b["y1"] and b["y0"] - m < a["y1"]):
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, ln in enumerate(lines):
        groups.setdefault(find(i), []).append(ln)

    rtl = base_lang(lang) in ("ja", "zh", "zh-hk")
    joiner = " " if base_lang(lang) == "ko" else ""
    # Groups per bubble region: two speeches in one joined bubble must not both
    # be set across the whole bubble (they'd print on top of each other).
    per_region: dict = {}
    for g in groups.values():
        rs = {l.get("region") for l in g}
        if len(rs) == 1 and None not in rs:
            per_region[next(iter(rs))] = per_region.get(next(iter(rs)), 0) + 1
    bubbles = []
    for g in groups.values():
        vertical = sum((l["y1"] - l["y0"]) > 1.5 * (l["x1"] - l["x0"]) for l in g) > len(g) / 2
        g.sort(key=(lambda l: -l["x1"]) if vertical else (lambda l: (l["y0"], l["x0"])))
        if all(re.fullmatch(r"\d+", l["t"]) for l in g):
            continue   # page number, not dialogue
        joined = "".join(l["t"] for l in g)
        if not _CJK.search(joined) and len(re.sub(r"\W", "", joined)) < 6:
            continue   # a few Latin letters out of scribbles/linework ("Hf", "LTxSM")
        if base_lang(lang) in ("ja", "zh", "zh-hk", "ko") and _CJK.search(joined):
            # In a CJK bubble a line with no CJK at all is debris read off the
            # art ("7", "Litho!!!"): drop it from the text AND from the box/region.
            g = [l for l in g if _CJK.search(l["t"])]
        src = joiner.join(t for t in (_clean_line(l["t"], lang) for l in g) if t)
        regions = {l.get("region") for l in g}
        region = next(iter(regions)) if len(regions) == 1 else None
        if _is_junk(src, lang, region is not None):
            continue
        if re.search(r"[A-Za-z0-9]", joined) and len(re.sub(r"[\W\d_ー・]", "", src)) <= 1:
            continue   # one character left after stripping debris ("ム7"): noise
        x0, y0 = min(l["x0"] for l in g), min(l["y0"] for l in g)
        x1, y1 = max(l["x1"] for l in g), max(l["y1"] for l in g)
        box = {"x": round(x0), "y": round(y0), "w": round(x1 - x0), "h": round(y1 - y0),
               "src": src, "vertical": vertical}
        # Coloured lettering is a sound effect unless it sits on bubble paper
        # (colour pages often letter dialogue in pink inside white bubbles).
        on_paper = region is not None and all(l.get("paper") for l in g)
        sfx = (not on_paper and sum(bool(l.get("colored")) for l in g) * 2 > len(g)
               and not all(l.get("local_paper") for l in g))
        # A white area holding several speeches is two bubbles touching, or a
        # bubble run into white skin/hair (black-and-white art): painting its
        # outline erased a face. Those get a white box per speech instead.
        shape = (_bubble_shape(geo, region, x1 - x0, y1 - y0, page_area)
                 if geo is not None and on_paper and per_region.get(region, 0) <= 1 else None)
        if shape:
            box["poly"], box["bubble"] = shape   # paint the bubble, set English inside it
        elif sfx or (region is None and sum(bool(l.get("light")) for l in g) * 2 < len(g)):
            box["free"] = True                   # on the art (SFX): outlined text, no box
        elif geo is not None:
            box["area"] = _light_span(geo[2], int(x0), int(y0), int(x1), int(y1), geo[2].shape[1],
                                      geo[0], region)
        bubbles.append(box)
    band = max(1.0, max((b["y"] + b["h"] for b in bubbles), default=1) * 0.08)
    bubbles.sort(key=lambda b: (int(b["y"] // band), -(b["x"] + b["w"]) if rtl else b["x"]))
    return bubbles


def ocr_page(path: Path, lang: str) -> tuple[int, int, list[dict]]:
    """(width, height, bubbles) for one page image. Very tall webtoon strips are
    cut into overlapping tiles so the detector doesn't shrink the text away."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        arr = np.asarray(im)
    lines: list[dict] = []
    with _OCR_LOCK:
        if h <= 2.5 * w:
            lines = _ocr_lines(arr, lang)
        else:
            tile, step = int(2 * w), int(2 * w) - 200
            seen: set[tuple] = set()
            for top in range(0, h, step):
                for ln in _ocr_lines(arr[top:top + tile], lang, y_off=top):
                    key = (ln["t"], round(ln["x0"] / 20), round(ln["y0"] / 20))
                    if key not in seen:   # the 200px overlap reads some lines twice
                        seen.add(key)
                        lines.append(ln)
                if top + tile >= h:
                    break
    geo = _region_labels(arr, lines)
    return w, h, _group(lines, lang, geo, w * h)


# --------------------------------------------------------------------- LLM --
# A refusal from the model itself, not a character saying "I can't…" (dialogue is
# full of that): only phrases about helping/translating/content count.
_REFUSAL = re.compile(r"\b(?:I can(?:not|'t) (?:help|assist|provide|translate|fulfil|comply|create|generate)"
                      r"|I(?:'m| am) sorry, but|as an AI|I won't (?:translate|help|provide)"
                      r"|content (?:policy|guidelines))", re.I)


def _refused(out: list) -> bool:
    hits = sum(bool(_REFUSAL.search(str(x))) for x in out)
    return hits > 0 and hits >= max(1, len(out) // 3)


LLAMACPP = os.environ.get("MANGASHELF_LLAMACPP", "http://127.0.0.1:8090")


def _chat(model: str, system: str, user: str, max_tokens: int = 4000) -> str:
    if model.startswith("llamacpp:"):
        # A MoE model served by llama.cpp's llama-server (experts kept in RAM with
        # --n-cpu-moe), which Ollama can't do. OpenAI-style endpoint.
        body = json.dumps({
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},   # qwen3: no reasoning preamble
        }).encode("utf-8")
        req = urllib.request.Request(f"{LLAMACPP}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    body = json.dumps({
        "model": model, "stream": False, "format": "json",
        # Loading gemma3:12b from disk takes minutes; keep it resident between chapters.
        "keep_alive": "30m",
        # 4k fits a 40-line batch; 8k pushed a third of gemma3:12b onto the CPU.
        # num_predict caps a runaway reply (repetition loop): those cost 10+ min a batch.
        "options": {"temperature": 0.2, "num_ctx": 4096, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())["message"]["content"]


def _local() -> dict:
    """Machine-local translation settings (~/.mangashelf/mt_local.json, never in the
    repo): "fallback_model" and "prompt_extra" (owner glossary / style notes
    appended to the prompt)."""
    try:
        return json.loads((Path.home() / ".mangashelf" / "mt_local.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _system_prompt(lang: str, work: str = "") -> str:
    name = LANG_NAMES.get(base_lang(lang), "the source language")
    return (
        f"You translate manga/manhwa/manhua speech bubbles from {name} into natural, colloquial "
        "English that reads like a professional scanlation. Translate every line faithfully and "
        "completely, keeping the original tone; never soften, skip, summarize or add notes. "
        + (f"The work is: {work}. Use the official English names of its characters. A katakana "
           "word that is a character's name stays that name EVERY time it appears, never "
           "translated as an ordinary word. " if work else "")
        + ("Chinese text here is usually a fan translation of a JAPANESE manga: write character "
           "names in their Japanese reading (卫 = Mamoru, 飒空 = Saku, 昴/昂 = Subaru), never pinyin "
           "or a single letter. " if base_lang(lang) in ("zh", "zh-hk") else "")
        + "The source text comes from OCR and may contain wrong characters; infer the intended "
        "meaning from context. Keep names, honorifics and tone consistent with the previous context. "
        "A ○ or 〇 inside a word hides letters: translate the intended word. Interjections and sound "
        "effects become short natural English, never romanized syllables (はぁ…はぁ → Haah… haah, "
        "びくびく → *twitch twitch*, ドキドキ → *ba-dump*). "
        + _local().get("prompt_extra", "")
        + "If a line is meaningless OCR noise, return an empty string for it. Reply with JSON only, "
        "keyed by the input line numbers, every number present exactly once: "
        "{\"t\": {\"1\": \"English for line 1\", \"2\": \"English for line 2\", ...}}. Never merge "
        "two lines into one or move text to a neighbouring number."
    )


_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _cjk_share(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    return sum(bool(_CJK.match(c)) for c in chars) / max(1, len(chars))


_KANA_ONLY = re.compile(r"^[぀-ヿ\s…・！？!?～〜ー♡♥っッ、。.,~-]+$")
# romaji syllables, each optionally closed by the doubled consonant of a small っ
_ROMAJI = re.compile(r"^(?:(?:[kgsztdnhbpmyrwfjv]?[yh]?|ch|sh|ts)[aiueo][kgtpbszdj]{0,2}|n|[\s!?.,~♡♥…'-])+$", re.I)
_PLAIN_SOUND = re.compile(r"^[aeiouh\s!?.,~♡♥…'-]+$", re.I)   # "Aaah~", "Ohh♡" read fine in English


# One romaji word: syllables (optional consonant/glide + vowel, or n, or the
# doubled consonant of a small っ). English words with clusters (th, pl, ck,
# rr, ...) or c/l/q/x don't fit, so ordinary English rarely matches.
_ROMAJI_WORD = re.compile(r"^(?:(?:ky|gy|sh|ch|ts|ny|hy|my|ry|by|py|j|[kgsztdnhbpmyrwf])?[aiueo]|n(?![aiueo])|([kgsztpbdj])(?=\1))+$")


def romanized(src: str, en: str) -> bool:
    """English output that is really the Japanese spelled out in Latin letters
    ("Arigatou gozaimasu", "Ohayou, minna-san") instead of translated:
    most of its longer words are pure romaji syllables."""
    if base_lang_of_text(src) != "ja":
        return False
    toks = re.findall(r"[A-Za-z]+", en or "")
    # Capitalised words past the first are names ("Tanaka", "Sakura"): legit romaji.
    words = [w.lower() for i, w in enumerate(toks)
             if len(w) >= 4 and (i == 0 or not w[0].isupper())]
    hits = [w for w in words if _ROMAJI_WORD.match(w) and w not in _ENGLISH_CV]
    return len(hits) >= 2 and len(hits) * 2 >= len(words)


# Everyday English words that happen to be valid romaji syllables.
_ENGLISH_CV = set("""more take make here home same huge time name safe made side wide ride
hide bite mine nine fine wine tone zone bone gone done none some sure pure wake sake bake fake
rate date gate hate mate note wife tide dame game fame nose rose hose pose dose wise rise base
page rage wage sage maze haze mute tube dude rude hope dope rope type bare dare fare hare
mare rare ware care tame bone pose hire fire tire wire sore bore tore wore gore hole mole pole
mode node rode code wade fade jade babe tote suit""".split())


def base_lang_of_text(src: str) -> str:
    return "ja" if _KANA.search(src or "") else ""


def is_romanized_noise(src: str, en: str) -> bool:
    """A short kana-only sound the model just spelled out in romaji ("んっ" ->
    "Nn!"). Showing that over the art is worse than the original sound, so the
    overlay is left blank and the original stays visible."""
    s = (src or "").strip()
    e = (en or "").strip()
    return (bool(e) and len(s) <= 10 and bool(_KANA_ONLY.match(s))
            and bool(_ROMAJI.match(e)) and not _PLAIN_SOUND.match(e))


def translate_lines(lines: list[str], lang: str, context: list[tuple[str, str]],
                    model: str = DEFAULT_MODEL, work: str = "", strict: bool = False) -> list[str]:
    """Translate `lines` (one bubble each). `context` = recent (source, english) pairs."""
    ctx = "\n".join(f"{s}  =>  {e}" for s, e in context[-_CONTEXT_LINES:])
    user = ((f"Previous context (already translated, for continuity):\n{ctx}\n\n" if ctx else "")
            + ("These lines were previously returned as Japanese written in Latin letters (romaji). "
               "That is wrong: give the English MEANING in natural English words. Write a sound "
               "effect as an English sound (Mmh, Haah, Ba-dump) and never transliterate.\n\n" if strict else "")
            + f"Translate these {len(lines)} lines:\n"
            + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines)))
    last_err = ""
    # A llama-server MoE falls back to gemma (same family of mistakes as before),
    # gemma itself to FALLBACK_MODEL (or mt_local.json's "fallback_model").
    for m in dict.fromkeys([model, DEFAULT_MODEL if model.startswith("llamacpp:") else _local().get("fallback_model", FALLBACK_MODEL)]):
        for _attempt in range(2):
            try:
                t0 = time.time()
                raw = _chat(m, _system_prompt(lang, work), user, max_tokens=120 * len(lines) + 200)
                print(f"[mt] {m}: {len(lines)} lines in {time.time() - t0:.0f}s", flush=True)
                data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
                out = data.get("t") if isinstance(data, dict) else None
                if out is None and isinstance(data, dict) and "1" in data:
                    out = data            # {"1": ..} without the "t" wrapper: same thing
                # Keyed by line number so a skipped/merged line can't silently shift
                # every later translation onto the wrong bubble. A plain list is
                # accepted only if its length matches; a dict must have every key.
                if isinstance(out, dict):
                    out = [out.get(str(i + 1)) for i in range(len(lines))]
                    if any(x is None for x in out):
                        out = None
                if (isinstance(out, list) and len(out) == len(lines)
                        and not _refused(out)):
                    # A degenerate loop ("this way, this way, ..." x300) is cut to three.
                    out = [re.sub(r"(\b.{2,40}?)(?:[\s,.!~♡…-]+\1){3,}", r"\1, \1, \1", str(x).strip())
                           for x in out]
                    # A line echoed back still in the source script (garbled OCR
                    # confuses it in a batch): ask again for just those lines, once.
                    stuck = [i for i, t in enumerate(out)
                             if _cjk_share(t) > 0.3 or romanized(lines[i], t)]
                    if stuck and not strict:
                        try:
                            again = translate_lines([lines[i] for i in stuck], lang, context, model, work,
                                                    strict=True)
                            for i, t in zip(stuck, again):
                                out[i] = t
                        except RuntimeError:
                            pass
                    return out
                last_err = f"{m}: bad/refused reply"
                print(f"[mt] {m}: rejected reply: {raw[:160]!r}", flush=True)
            except Exception as exc:  # noqa: BLE001
                last_err = f"{m}: {exc}"
    raise RuntimeError(f"Translation failed ({last_err})")


# ----------------------------------------------------------------- chapter --
def chapter_language(ch_dir: Path) -> str:
    try:
        return str(json.loads((ch_dir / CHAPTER_INFO).read_text(encoding="utf-8")).get("language") or "")
    except Exception:
        return ""


def _pages(ch_dir: Path) -> list[Path]:
    return sorted(p for p in ch_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT)


def previous_context(ch_dir: Path) -> list[tuple[str, str]]:
    """Last translated lines of the chapter before this one (folders sort by number)."""
    sibs = sorted(p for p in ch_dir.parent.iterdir() if p.is_dir())
    idx = sibs.index(ch_dir) if ch_dir in sibs else -1
    for prev in reversed(sibs[:max(idx, 0)]):
        f = prev / TRANSLATION_FILE
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                pairs = [(b["src"], b["en"]) for p in data.get("pages", []) for b in p.get("boxes", [])]
                return pairs[-_CONTEXT_LINES:]
            except Exception:
                return []
    return []


def translate_chapter(ch_dir: Path, lang: str, model: str = DEFAULT_MODEL,
                      wait: Callable[[], None] = lambda: None,
                      progress: Callable[[str], None] = lambda m: None, work: str = "") -> int:
    """OCR + translate one chapter folder, write translation.en.json. `wait()` is
    called before each page (the idle gate). Returns the number of bubbles.
    `work` = the series title, so the model knows which work (and its character
    names) it's translating; defaults to the folder name."""
    if not work:   # "Series/Chapter 0001" -> "Series"; a one-folder series is its own name
        work = ch_dir.parent.name if re.search(r"chap|^\d+$", ch_dir.name, re.I) else ch_dir.name
    pages = _pages(ch_dir)
    out_pages = []
    for i, p in enumerate(pages, 1):
        wait()
        progress(f"Reading text, page {i}/{len(pages)}")
        w, h, boxes = ocr_page(p, lang)
        out_pages.append({"file": p.name, "w": w, "h": h, "boxes": boxes})
    flat = [b for pg in out_pages for b in pg["boxes"]]
    context = previous_context(ch_dir)
    for start in range(0, len(flat), _BATCH):
        wait()
        progress(f"Translating {min(start + _BATCH, len(flat))}/{len(flat)} lines")
        batch = flat[start:start + _BATCH]
        for b, en in zip(batch, translate_lines([b["src"] for b in batch], lang, context, model, work)):
            # Still romaji after the strict retry: better blank (the original stays
            # visible) than Japanese in Latin letters pretending to be English.
            b["en"] = "" if is_romanized_noise(b["src"], en) or romanized(b["src"], en) else en
            context.append((b["src"], en))
    result = {"model": model, "source_lang": lang, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "v": PIPELINE_VERSION, "pages": out_pages}
    tmp = ch_dir / (TRANSLATION_FILE + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ch_dir / TRANSLATION_FILE)
    return len(flat)


# -------------------------------------------------------------- idle gate --
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input on this PC (0 if unknown)."""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
    except Exception:
        pass
    return 0.0


def gpu_busy(threshold: int = 75) -> bool:   # an animated wallpaper alone holds ~40%
    """Something else (a game) is using the GPU. Checked only before a chapter,
    while our own model isn't loaded. Gamepad input doesn't count as "input" for
    idle_seconds(), so this is what keeps a controller game from being interrupted."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return int(out.strip().splitlines()[0]) >= threshold
    except Exception:
        return False


if __name__ == "__main__":   # self-check: python translate.py <page image> <lang>
    import sys
    w, h, b = ocr_page(Path(sys.argv[1]), sys.argv[2])
    print(w, h, len(b))
    en = translate_lines([x["src"] for x in b], sys.argv[2], [])
    for x, e in zip(b, en):
        print(x["x"], x["y"], x["src"], "=>", e)
