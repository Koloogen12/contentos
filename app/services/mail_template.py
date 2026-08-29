"""Вёрстка писем в дизайн-системе продукта.

Почтовые клиенты — не браузеры. Gmail вырезает `<style>` из тела, Outlook
рендерит движком Word, тёмная тема Apple Mail самовольно инвертирует цвета.
Поэтому здесь всё, что в вебе считается плохим тоном, и является правильным:

* **таблицы вместо flex и grid** — единственная раскладка, которую одинаково
  понимают все клиенты;
* **инлайновые стили** — блок `<style>` в Gmail просто не доезжает;
* **никаких внешних шрифтов** — `@font-face` в почте не работает, поэтому
  системный стек вместо Onest; начертания и размеры сохраняем;
* **ширина 600px** — исторический предел, за которым Outlook начинает ломать
  колонки.

Цвета взяты из `prime2.css` — те же, что в интерфейсе: оранжевый `#F2601A`,
чернила `#171717`, бумага `#F4F3F1`, линия `rgba(23,23,23,.09)`, радиус 14px.

Текстовая версия отправляется вместе с HTML, а не вместо: часть клиентов
показывает её в списке писем, а часть людей отключает HTML совсем.
"""
from __future__ import annotations

from app.config import settings

# ── токены дизайн-системы ────────────────────────────────────────────────────
OR = "#F2601A"          # акцент
INK = "#171717"         # основной текст
INK_2 = "#5A5A57"       # вторичный
INK_3 = "#767573"       # подписи
PAPER = "#F4F3F1"       # фон письма
CARD = "#FFFFFF"        # карточка
LINE = "#E8E7E4"        # граница (сплошная: rgba в Outlook не работает)
RADIUS = "14px"

FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)


def _button(href: str, label: str) -> str:
    """Кнопка таблицей: `<a>` с padding в Outlook теряет фон."""
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:26px 0 0">
      <tr><td align="center" bgcolor="{OR}" style="border-radius:12px">
        <a href="{href}" style="display:inline-block;padding:14px 26px;font:600 15px/1 {FONT};
           color:#ffffff;text-decoration:none;border-radius:12px">{label}</a>
      </td></tr>
    </table>"""


def _code_block(code: str) -> str:
    """Код крупно и с разрядкой: его переписывают глазами с телефона."""
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin:26px 0 0">
      <tr><td align="center" bgcolor="{PAPER}" style="border-radius:12px;padding:22px 16px">
        <div style="font:700 34px/1 {FONT};letter-spacing:.24em;color:{INK};
             padding-left:.24em">{code}</div>
      </td></tr>
    </table>"""


def shell(*, title: str, body_html: str, preheader: str = "") -> str:
    """Общий каркас письма: шапка, карточка, подпись."""
    hidden = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{preheader}</div>'
        if preheader else ""
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:{PAPER};">
{hidden}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{PAPER};padding:32px 16px">
  <tr><td align="center">

    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
           style="width:600px;max-width:100%">

      <tr><td style="padding:0 4px 18px">
        <span style="font:700 14px/1 {FONT};letter-spacing:.14em;
              text-transform:uppercase;color:{INK}">THE&nbsp;DRAFT</span>
      </td></tr>

      <tr><td bgcolor="{CARD}"
              style="background:{CARD};border:1px solid {LINE};border-radius:{RADIUS};
                     padding:34px 36px">
        {body_html}
      </td></tr>

      <tr><td style="padding:20px 4px 0">
        <p style="margin:0;font:400 12.5px/1.55 {FONT};color:{INK_3}">
          Письмо отправлено сервисом THE DRAFT.<br>
          Если ты его не запрашивал — просто удали, ничего делать не нужно.
        </p>
      </td></tr>

    </table>

  </td></tr>
</table>
</body>
</html>"""


# ── письмо с кодом подтверждения ─────────────────────────────────────────────

CODE_SUBJECT = "Код подтверждения THE DRAFT"


def code_html(code: str) -> str:
    minutes = settings.EMAIL_CODE_TTL_MINUTES
    body = f"""
        <h1 style="margin:0;font:700 24px/1.25 {FONT};letter-spacing:-.02em;color:{INK}">
          Подтверди почту
        </h1>
        <p style="margin:12px 0 0;font:400 15.5px/1.6 {FONT};color:{INK_2}">
          Введи этот код на странице регистрации.
        </p>
        {_code_block(code)}
        <p style="margin:22px 0 0;font:400 13.5px/1.6 {FONT};color:{INK_3}">
          Код действует {minutes} минут. Если код запрашивал не ты — просто
          проигнорируй письмо: без подтверждения аккаунт не активируется.
        </p>"""
    return shell(
        title=CODE_SUBJECT,
        preheader=f"Код {code} — действует {minutes} минут",
        body_html=body,
    )


def code_text(code: str) -> str:
    minutes = settings.EMAIL_CODE_TTL_MINUTES
    return (
        f"Код подтверждения: {code}\n\n"
        f"Введи его на странице регистрации. Код действует {minutes} минут.\n\n"
        "Если код запрашивал не ты — просто проигнорируй письмо: "
        "без подтверждения аккаунт не активируется.\n\n"
        "THE DRAFT"
    )


# ── приветственное письмо ────────────────────────────────────────────────────

WELCOME_SUBJECT = "Добро пожаловать в THE DRAFT"


def welcome_html(*, name: str | None, app_url: str) -> str:
    greeting = f"Привет, {name}" if name else "Привет"
    body = f"""
        <h1 style="margin:0;font:700 24px/1.25 {FONT};letter-spacing:-.02em;color:{INK}">
          {greeting}
        </h1>
        <p style="margin:12px 0 0;font:400 15.5px/1.6 {FONT};color:{INK_2}">
          Почта подтверждена, аккаунт готов. THE DRAFT собирает контент-план из
          того, что ты уже знаешь и умеешь, — а не из пустого листа.
        </p>

        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="margin:24px 0 0;border-top:1px solid {LINE}">
          <tr><td style="padding:18px 0 0">
            <p style="margin:0;font:600 15px/1.4 {FONT};color:{INK}">С чего начать</p>
            <p style="margin:8px 0 0;font:400 14.5px/1.65 {FONT};color:{INK_2}">
              1. Брось материал — голосовое, запись созвона или ссылку.<br>
              2. Забери из него тезисы с оценкой потенциала.<br>
              3. Поставь их в план и опубликуй.
            </p>
          </td></tr>
        </table>

        {_button(app_url, "Открыть THE DRAFT")}"""
    return shell(
        title=WELCOME_SUBJECT,
        preheader="Аккаунт готов — можно собирать первый план",
        body_html=body,
    )


def welcome_text(*, name: str | None, app_url: str) -> str:
    greeting = f"Привет, {name}!" if name else "Привет!"
    return (
        f"{greeting}\n\n"
        "Почта подтверждена, аккаунт готов.\n\n"
        "С чего начать:\n"
        "1. Брось материал — голосовое, запись созвона или ссылку.\n"
        "2. Забери из него тезисы с оценкой потенциала.\n"
        "3. Поставь их в план и опубликуй.\n\n"
        f"{app_url}\n\n"
        "THE DRAFT"
    )
