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

## Roadmap

| Iteration | Scope | Status |
|---|---|---|
| 1 | Foundation: scaffold, auth, multi-tenant CRUD, migrations | ✅ done |
| 2 | CometAPI client, skills (`viral_talking_points`, `*_creator`), SSE | in progress |
| 3 | YouTube + Whisper transcription, knowledge import, voice embeddings | planned |
| 4 | Telegram publishing (dry-run → approve → channel) | planned |
| 5 | Selectel deploy + Postgres/Redis/S3 managed | planned |
