"""Bulk-load Данил's existing knowledge base into ContentOS via the public API.

Usage:
    cd tools/content-os-backend
    export CONTENTOS_API_URL=http://localhost:8000
    export CONTENTOS_EMAIL=danil@example.com
    export CONTENTOS_PASSWORD=...
    python scripts/import_knowledge.py [--workspace /path/to/personal-brand] [--dry-run]

The script logs in via /api/v1/auth/login, parses markdown sources from
the workspace folders, and creates KnowledgeItems via POST /api/v1/knowledge.

Strategy is intentionally lenient: every section we can parse becomes one
item; everything else is logged as skipped. Re-running the script is safe
in the sense that it just creates more items — there is no de-duplication
on the server side. Use the UI to clean up duplicates.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger("import_knowledge")

DEFAULT_WORKSPACE = Path("/Users/danilkocnev/Documents/Claude/Projects/personal-brand")


@dataclass
class Item:
    type: str
    title: str
    body: str
    tags: list[str]
    viral_score: int | None
    source_file: str
    pillar: str | None = None  # R1 / R2 / R3 / R4 if found in source


_TEZIS_HEADER = re.compile(r"^###\s+(B-\d+)\.\s+(.+?)\s*$")
_PP_HEADER = re.compile(r"^###\s+Priority Pick #(\d+)\s+—?\s+Viral Score:\s*(\d+)/20\s*$")
_PP_THESIS = re.compile(r"^\*\*Тезис:?\*\*\s*(.+)$", re.M)
_PP_HOOK = re.compile(r"^\*\*Готовый хук:?\*\*\s*[«\"]?(.+?)[»\"]?\s*$", re.M)
_PILLAR_RE = re.compile(r"(?:Столб|Пиллар|Pillar)[:\s]+(R[1-4])", re.I)


def _extract_pillar(block: str) -> str | None:
    m = _PILLAR_RE.search(block)
    return m.group(1).upper() if m else None


def _strip_status(title: str) -> tuple[str, str | None]:
    m = re.match(r"^(.+?)\s+—\s+(ФЛАГМАН|ГОТОВО|БАНК|АРХИВ|В РАБОТЕ|ОПУБЛИКОВАНО)\s*$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title.strip(), None


def parse_tezis_bank(text: str, source_file: str) -> list[Item]:
    """Parse extracted/tezis-bank.md style: ### B-XX. Title with **Суть:** sections."""
    items: list[Item] = []
    blocks = re.split(r"^###\s+B-", text, flags=re.M)[1:]
    for raw in blocks:
        head, _, body = raw.partition("\n")
        head = "B-" + head.strip()
        m = _TEZIS_HEADER.match("### " + head)
        if not m:
            continue
        code = m.group(1)
        title_full = m.group(2)
        title, status = _strip_status(title_full)

        # Suть as primary body content
        gist_m = re.search(r"\*\*Суть:?\*\*\s*(.+?)(?=\n\*\*|\n###|\Z)", body, flags=re.S)
        gist = gist_m.group(1).strip() if gist_m else body.strip()
        gist = re.sub(r"\n\s*\n+", "\n\n", gist)[:4000]

        tags = [code]
        if status:
            tags.append(status.lower().replace(" ", "-"))

        items.append(
            Item(
                type="tezis",
                title=f"{code} {title}"[:500],
                body=gist,
                tags=tags,
                viral_score=None,
                source_file=source_file,
                pillar=_extract_pillar(body),
            )
        )
    return items


def parse_priority_picks(text: str, source_file: str) -> list[Item]:
    """Parse viral-talking-points*.md style with Priority Pick + Viral Score."""
    items: list[Item] = []
    blocks = re.split(r"^###\s+Priority Pick", text, flags=re.M)[1:]
    for raw in blocks:
        head_match = _PP_HEADER.match("### Priority Pick" + raw.split("\n", 1)[0])
        score = None
        if head_match:
            try:
                score = int(head_match.group(2))
            except ValueError:
                score = None

        thesis_m = _PP_THESIS.search(raw)
        if not thesis_m:
            continue
        thesis = thesis_m.group(1).strip()

        hook_m = _PP_HOOK.search(raw)
        hook = hook_m.group(1).strip() if hook_m else None

        body = thesis
        if hook:
            body = f"{thesis}\n\nХук: {hook}"

        title = thesis.split(".")[0][:120] or thesis[:120]
        items.append(
            Item(
                type="tezis",
                title=title,
                body=body[:4000],
                tags=["priority-pick"],
                viral_score=score if score is not None and 0 <= score <= 20 else None,
                source_file=source_file,
                pillar=_extract_pillar(raw),
            )
        )
    return items


def parse_voice_rules(text: str, source_file: str) -> list[Item]:
    """Parse knowledge-base/brand-voice.md sections as voice_rule items."""
    items: list[Item] = []
    blocks = re.split(r"^##\s+", text, flags=re.M)[1:]
    for block in blocks:
        head, _, body = block.partition("\n")
        title = head.strip()[:200]
        body = body.strip()
        if not body or len(body) < 50:
            continue
        items.append(
            Item(
                type="voice_rule",
                title=title or "Untitled voice rule",
                body=body[:4000],
                tags=["brand-voice"],
                viral_score=None,
                source_file=source_file,
            )
        )
    return items


def parse_audience(text: str, source_file: str) -> list[Item]:
    items: list[Item] = []
    blocks = re.split(r"^##\s+", text, flags=re.M)[1:]
    for block in blocks:
        head, _, body = block.partition("\n")
        title = head.strip()[:200]
        body = body.strip()
        if not body:
            continue
        items.append(
            Item(
                type="audience",
                title=title or "Audience segment",
                body=body[:4000],
                tags=["audience"],
                viral_score=None,
                source_file=source_file,
            )
        )
    return items


def parse_generic_reference(text: str, source_file: str, default_title: str) -> list[Item]:
    title_m = re.search(r"^#\s+(.+)$", text, flags=re.M)
    title = title_m.group(1).strip() if title_m else default_title
    return [
        Item(
            type="reference",
            title=title[:500],
            body=text.strip()[:8000],
            tags=["reference", Path(source_file).stem],
            viral_score=None,
            source_file=source_file,
        )
    ]


def collect_items(workspace: Path) -> list[Item]:
    items: list[Item] = []

    extracted = workspace / "extracted"
    knowledge_base = workspace / "knowledge-base"

    files: list[tuple[Path, str]] = []
    if extracted.exists():
        files += [(extracted / "tezis-bank.md", "tezis_bank")]
        for fname in (
            "viral-talking-points.md",
            "viral-talking-points-ajtbd.md",
            "ajtbd-talking-points.md",
            "zamesin-ideas-master.md",
        ):
            p = extracted / fname
            if p.exists():
                files.append((p, "priority_picks"))
    if knowledge_base.exists():
        if (knowledge_base / "brand-voice.md").exists():
            files.append((knowledge_base / "brand-voice.md", "voice_rules"))
        if (knowledge_base / "audience-segments.md").exists():
            files.append((knowledge_base / "audience-segments.md", "audience"))
        for fname in (
            "content-formula.md",
            "hook-library.md",
            "storylines.md",
            "offer-stack.md",
            "visual-code.md",
            "goals-2026.md",
            "metrics-baseline.md",
        ):
            p = knowledge_base / fname
            if p.exists():
                files.append((p, "reference"))
        bp = knowledge_base / "brand-profiles"
        if bp.exists():
            for f in bp.glob("*.md"):
                files.append((f, "reference"))

    for path, kind in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("cannot read %s", path)
            continue
        rel = str(path.relative_to(workspace))
        if kind == "tezis_bank":
            items += parse_tezis_bank(text, rel)
        elif kind == "priority_picks":
            items += parse_priority_picks(text, rel)
        elif kind == "voice_rules":
            items += parse_voice_rules(text, rel)
        elif kind == "audience":
            items += parse_audience(text, rel)
        else:
            items += parse_generic_reference(text, rel, default_title=path.stem)

    return items


def login(client: httpx.Client, base_url: str, email: str, password: str) -> str:
    r = client.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": email, "password": password},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def post_item(client: httpx.Client, base_url: str, token: str, item: Item) -> int:
    r = client.post(
        f"{base_url}/api/v1/knowledge",
        json={
            "type": item.type,
            "title": item.title,
            "body": item.body,
            "tags": item.tags,
            "viral_score": item.viral_score,
            "pillar": item.pillar,
            "source_file": item.source_file,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return r.status_code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no API calls")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists():
        logger.error("workspace not found: %s", workspace)
        return 2

    items = collect_items(workspace)
    logger.info("parsed %d items", len(items))
    by_type: dict[str, int] = {}
    for it in items:
        by_type[it.type] = by_type.get(it.type, 0) + 1
    for k, v in by_type.items():
        logger.info("  %s: %d", k, v)

    if args.dry_run:
        return 0

    base_url = os.environ.get("CONTENTOS_API_URL", "http://localhost:8000").rstrip("/")
    email = os.environ.get("CONTENTOS_EMAIL")
    password = os.environ.get("CONTENTOS_PASSWORD")
    if not email or not password:
        logger.error("CONTENTOS_EMAIL / CONTENTOS_PASSWORD not set")
        return 2

    with httpx.Client() as client:
        try:
            token = login(client, base_url, email, password)
        except httpx.HTTPError as exc:
            logger.error("login failed: %s", exc)
            return 1

        ok = 0
        fail = 0
        for it in items:
            try:
                code = post_item(client, base_url, token, it)
                if 200 <= code < 300:
                    ok += 1
                else:
                    fail += 1
                    logger.warning("item failed (%d): %s", code, it.title[:80])
            except httpx.HTTPError as exc:
                fail += 1
                logger.warning("item error: %s — %s", it.title[:80], exc)

        logger.info("done: %d created, %d failed", ok, fail)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
