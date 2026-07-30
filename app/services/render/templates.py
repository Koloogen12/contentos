"""HTML templates for carousel slides.

Each template is a function that takes a structured payload and returns a
single-page HTML document. The document is sized exactly for 1080×1350
(Instagram portrait, also fine for LinkedIn carousel pages) and Playwright
screenshots it at deviceScaleFactor=3 to produce 3240×4050 source images
that downscale crisply on retina screens.

Design language: **editorial dark** — black background, big bold Manrope
title, red accent (#FF3B30) for eyebrow tags and inline emphasis spans.
Inspired by the Virale carousel style (validated by their RU audience)
but with our own typography (Manrope vs Inter) so we don't look identical.

All fonts are loaded from Google Fonts at render time — Playwright waits
for `networkidle` before the screenshot so font loading is not a race.

Why inline HTML over a template engine (Jinja2/etc): renders run inside
the worker process where Jinja2's I/O isn't worth the complexity for
two templates. If we add a third template we'll factor this out.
"""
from __future__ import annotations

import html
import re
from typing import Any

# Public so the worker can pass it to Playwright as the viewport size.
SLIDE_WIDTH = 1080
SLIDE_HEIGHT = 1350

# Image generation outputs at 1024×1024 by default and we crop / cover-fit
# inside the slide. For the cover we ask for 1024×1536 directly (closest
# to our 4:5 ratio in gpt-image-1's allowed sizes) so the cropping artifacts
# are minimal.
COVER_IMAGE_GEN_SIZE = "1024x1536"


_BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@500;700;800;900&display=swap');

:root {
  --bg: #0a0a0a;
  --fg: #ffffff;
  --muted: rgba(255, 255, 255, 0.55);
  --accent: #ff3b30;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  width: 1080px;
  height: 1350px;
  background: var(--bg);
  color: var(--fg);
  font-family: 'Manrope', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
"""


_BODY_SLIDE_CSS = (
    _BASE_CSS
    + """
.slide {
  width: 1080px;
  height: 1350px;
  padding: 80px;
  display: flex;
  flex-direction: column;
  position: relative;
  background: var(--bg);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 22px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.02em;
}

.eyebrow {
  margin-top: 56px;
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--accent);
}
.eyebrow::before { content: '— '; }

.title {
  margin-top: 28px;
  font-size: 84px;
  line-height: 1.04;
  font-weight: 800;
  letter-spacing: -0.02em;
  color: var(--fg);
  /* Allow long lines to break cleanly even with short single-character words. */
  overflow-wrap: break-word;
  word-break: normal;
}

.body {
  margin-top: auto;
  font-size: 34px;
  line-height: 1.38;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.92);
}
.body .accent { color: var(--accent); font-weight: 700; }

.footer {
  margin-top: 40px;
  font-size: 20px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.02em;
}
"""
)


_COVER_SLIDE_CSS = (
    _BASE_CSS
    + """
.slide {
  width: 1080px;
  height: 1350px;
  position: relative;
  overflow: hidden;
  background: var(--bg);
}

.slide__bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center;
  filter: brightness(0.7) contrast(1.05);
}

/* Bottom gradient so the title reads on any AI image. */
.slide__scrim {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(0, 0, 0, 0.2) 0%,
    rgba(0, 0, 0, 0) 35%,
    rgba(0, 0, 0, 0.45) 70%,
    rgba(0, 0, 0, 0.85) 100%
  );
}

.slide__content {
  position: absolute;
  inset: 0;
  padding: 80px;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
}

.cover-tag {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 24px;
}

.cover-title {
  font-size: 96px;
  line-height: 1.0;
  font-weight: 900;
  letter-spacing: -0.025em;
  color: var(--fg);
  text-wrap: balance;
  /* Slight text shadow stays readable against AI images of any colour. */
  text-shadow: 0 4px 18px rgba(0, 0, 0, 0.45);
}

.cover-subtitle {
  margin-top: 24px;
  font-size: 28px;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.85);
  letter-spacing: 0.01em;
}

.cover-swipe {
  margin-top: 32px;
  font-size: 22px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: 0.15em;
  text-transform: uppercase;
}
"""
)


_ACCENT_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def _render_inline(text: str) -> str:
    """Convert simple **bold** markers to `<span class="accent">...</span>`.

    The slide-text generator (carousel skill / tweak) emits emphasis using
    markdown-style `**...**` because the same text is also shown plain in
    the caption / Telegram fallback. We translate to coloured spans only
    at render time so the underlying string stays portable.
    """
    safe = html.escape(text or "").replace("\n", "<br/>")
    return _ACCENT_RE.sub(r'<span class="accent">\1</span>', safe)


def render_body_slide_html(
    *,
    eyebrow: str,
    title: str,
    body: str,
    username: str | None,
    slide_number: int | None,
    slide_total: int | None,
    footer: str | None = None,
) -> str:
    """Build the HTML for a non-cover slide.

    `eyebrow` is the small caps tag at the top ("ОШИБКА", "ШАГ", "ИНСАЙТ").
    Pass empty string to hide it.

    `slide_number`/`slide_total` are optional — when both are present we
    show `N/total` in the top-right corner; otherwise the header is hidden.
    """
    header_left = f"@{html.escape(username.lstrip('@'))}" if username else ""
    header_right = (
        f"{slide_number}/{slide_total}"
        if slide_number is not None and slide_total is not None
        else ""
    )
    header_html = (
        f'<div class="header"><span>{header_left}</span><span>{header_right}</span></div>'
        if (header_left or header_right)
        else ""
    )
    eyebrow_html = (
        f'<div class="eyebrow">{html.escape(eyebrow)}</div>' if eyebrow else ""
    )
    footer_html = (
        f'<div class="footer">{html.escape(footer)}</div>' if footer else ""
    )

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<style>{_BODY_SLIDE_CSS}</style>
</head>
<body>
<div class="slide">
  {header_html}
  {eyebrow_html}
  <h1 class="title">{_render_inline(title)}</h1>
  <div class="body">{_render_inline(body)}</div>
  {footer_html}
</div>
</body>
</html>"""


def render_cover_slide_html(
    *,
    title: str,
    subtitle: str | None,
    tag: str | None,
    image_data_url: str,
    swipe_hint: str = "ЛИСТАЙ →",
) -> str:
    """Build the HTML for the cover slide.

    `image_data_url` is the AI-generated background — passed as a base64
    data: URL so Playwright doesn't need a network round-trip to fetch it.
    """
    tag_html = (
        f'<div class="cover-tag">{html.escape(tag)}</div>' if tag else ""
    )
    subtitle_html = (
        f'<div class="cover-subtitle">{html.escape(subtitle)}</div>'
        if subtitle
        else ""
    )
    swipe_html = (
        f'<div class="cover-swipe">{html.escape(swipe_hint)}</div>'
        if swipe_hint
        else ""
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<style>{_COVER_SLIDE_CSS}</style>
</head>
<body>
<div class="slide">
  <img class="slide__bg" src="{image_data_url}" alt="" />
  <div class="slide__scrim"></div>
  <div class="slide__content">
    {tag_html}
    <h1 class="cover-title">{html.escape(title)}</h1>
    {subtitle_html}
    {swipe_html}
  </div>
</div>
</body>
</html>"""


def derive_eyebrow(slide_index: int, total: int, hint: str | None) -> str:
    """Pick the eyebrow tag for slide N based on a hint from the AI.

    The hint may be a verb-form ("ошибка"/"шаг"/"инсайт"/"совет") or empty.
    We uppercase it and, for known categories, append the running number.
    """
    raw = (hint or "").strip().upper()
    if not raw:
        # Generic "PART" eyebrow keeps the slide chrome consistent.
        return f"ЧАСТЬ {slide_index}"
    return raw


def title_with_number(title: str, slide_index: int, prepend_number: bool) -> str:
    """Optionally prefix the slide title with its number (1., 2., ...).

    The Virale convention is to number titles when the carousel is a
    list ("1. Строить без валидации"). We only number when the project's
    visual style requests numbered titles AND there's at least one numeric
    cue in the underlying slides (TODO: smarter detection later).
    """
    if not prepend_number:
        return title
    title = title.strip()
    if re.match(r"^\d+[\.\)]\s", title):
        return title  # already numbered by the writer
    return f"{slide_index}. {title}"
