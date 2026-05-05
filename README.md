# ContentOS

Node-based canvas SaaS for content pipelines. Goal: $1M MRR.

> **Repo layout:** monorepo. Backend lives at the root (so `docker compose up` works from here). Frontend (Next.js) will live under `./frontend/`.

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI async, PostgreSQL 16 + pgvector, SQLAlchemy 2.0, Alembic |
| Queue / cache | Arq + Redis |
| AI | CometAPI (OpenAI-compatible) — Claude / Whisper / embeddings |
| Auth | Custom JWT, email + password (multi-tenant via `organization_id`) |
| Storage | Selectel S3 (production) |
| Real-time | Server-Sent Events for skill-run progress |
| Frontend | Next.js 15 + React Flow v12 + Tailwind + shadcn/ui *(in `./frontend/`)* |
| Infra | Selectel VPS, Docker |

## Quickstart (local)

```bash
cp .env.example .env   # fill in COMETAPI_KEY etc. — JWT_SECRET pre-generated
docker compose up --build
```

Migrations run automatically at api container start (idempotent). Then:

- API: <http://localhost:8000>
- Swagger: <http://localhost:8000/docs>
- Postgres on host: `localhost:5433` (user/pass `contentos`/`contentos`)

## Multi-tenancy

Every business object carries `organization_id`. One user = one organization on signup; team support is V2. Postgres RLS planned but not enabled in MVP — application-level filtering through JWT claims for now.

## Backend layout

```
app/
  main.py                FastAPI app + middleware
  config.py              Pydantic Settings
  database.py            async engine + session factory
  api/v1/                routers (auth, canvases, nodes, edges, skill_runs, …)
  models/                SQLAlchemy ORM
  schemas/               Pydantic I/O
  services/              ai_client, auth, brand_context, skills/, transcription/
  workers/               Arq queue + tasks
alembic/                 migrations
scripts/                 import_knowledge.py, etc.
```

## Docs

- [`CONTRACTS.md`](./CONTRACTS.md) — canonical API contract (source of truth for the frontend)
- `../content-os/PRD.md` — product spec
- `../content-os/CLAUDE.md` — original backend brief

## Skills available

| Skill | Trigger | Output |
|---|---|---|
| `viral_talking_points` | extract node `Run` | 5–10 ranked talking points (4-axis viral score) |
| `telegram_creator` | format node, `platform=telegram` | hooks (3) + body + CTA + full_text |
| `linkedin_creator` | format node, `platform=linkedin` | hooks (3) + body + CTA + full_text |
| `carousel_creator` | format node, `platform=carousel` | 5–10 slides + summary + CTA |
| `reels_creator` | format node, `platform=reels` | 3 hooks + 4–6 beats + CTA + caption |
| `transcribe_youtube` | source node, YouTube URL | captions or whisper transcript + metadata |
| `transcribe_audio` | source node, audio upload | whisper transcript (auto-chunked >25 MB) |

Format skills inject the user's voice automatically: top-3 similar `voice_samples` retrieved by cosine similarity over pgvector are added as few-shot examples in the system prompt.

## Roadmap

| Iteration | Scope | Status |
|---|---|---|
| 1 | Foundation: scaffold, auth, multi-tenant CRUD, migrations | ✅ |
| 2 | CometAPI client, skills (extract, telegram/linkedin), SSE auth via `?token=` | ✅ |
| 3A | YouTube + audio transcription, Telegram publishing | ✅ |
| 3B | Voice training (samples, embeddings, extract-traits, few-shot retrieval) | ✅ |
| 3C | `import_knowledge.py` script for bulk-loading existing markdown bases | ✅ |
| D | Starter templates (auto-seeded on signup), `from-template`, carousel/reels skills | ✅ |
| D+ | Canvas duplicate, bulk run-all | ✅ |
| E | Onboarding wizard (frontend) | planned |
| F | Project picker across canvas/knowledge/voice (frontend) | planned |
| G | Selectel deploy: managed Postgres + Redis + S3, Caddy/Traefik SSL | planned |
| V2 | Public sharing, marketplace templates, voice fine-tune, Instagram/LinkedIn API publishing | planned |
