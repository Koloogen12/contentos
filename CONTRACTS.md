# ContentOS — API Contracts (для фронт-агента)

**Базовый URL (dev):** `http://localhost:8000`
**Версия:** v1, prefix `/api/v1`
**Auth:** Bearer JWT в заголовке `Authorization: Bearer <access_token>`
**Все ID:** UUID v4
**Все timestamps:** ISO 8601, UTC

> Канонический контракт. Если в коде расходится — этот документ источник истины, чините код.

---

## Соглашения

- Multi-tenant: каждый ресурс принадлежит `organization_id`. Пользователь видит только свою org. Сервер сам фильтрует — фронт `organization_id` в payload не шлёт (берётся из JWT).
- Ошибки — `application/json` вида `{"detail": "..."}` со стандартными HTTP-кодами (`400/401/403/404/409/422/500`).
- Async-операции (skill-runs, transcription, publish) — паттерн **start → poll/SSE**: POST возвращает `id` ресурса со статусом `pending|running`, фронт следит через SSE или GET по id.

---

## 0. Health

```http
GET /health
→ 200 { "status": "ok", "version": "0.1.0", "env": "development" }
```

---

## 1. Auth

### Регистрация
```http
POST /api/v1/auth/register
{
  "email": "danil@example.com",
  "password": "min-8-chars",
  "display_name": "Данил",                // optional
  "organization_name": "Personal"         // optional, по умолчанию из email
}
→ 201
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer"
}
```
Создаёт `Organization` + `User` + дефолтный `BrandContext`. Возвращает токены сразу — не нужен отдельный login.

### Логин
```http
POST /api/v1/auth/login
{ "email": "...", "password": "..." }
→ 200 TokenPair
```

### Обновление access-токена
```http
POST /api/v1/auth/refresh
{ "refresh_token": "..." }
→ 200 TokenPair
```

### Текущий пользователь
```http
GET /api/v1/auth/me
Authorization: Bearer <access>
→ 200
{
  "user": { "id", "organization_id", "email", "display_name", "is_active", "created_at" },
  "organization": { "id", "name", "slug", "created_at" }
}
```

---

## 2. Canvases

### Создать канвас
```http
POST /api/v1/canvases
{
  "name": "Неделя 20 — Замесин",
  "description": null,                  // optional
  "project_id": null                     // optional UUID
}
→ 201 CanvasOut
```

### Список канвасов
```http
GET /api/v1/canvases?project_id=<uuid>&is_template=false
→ 200 CanvasOut[]
```

### Канвас с нодами и связями (главный endpoint для открытия канваса)
```http
GET /api/v1/canvases/{canvas_id}
→ 200
{
  "id", "organization_id", "project_id", "name", "description",
  "is_template", "created_at", "updated_at",
  "nodes": NodeOut[],
  "edges": EdgeOut[]
}
```

### Обновить
```http
PATCH /api/v1/canvases/{canvas_id}
{ "name": "...", "description": "...", "project_id": null }
→ 200 CanvasOut
```

### Удалить
```http
DELETE /api/v1/canvases/{canvas_id}
→ 204
```

### Сохранить как шаблон
```http
POST /api/v1/canvases/{canvas_id}/save-as-template
→ 200 CanvasOut (с is_template=true)
```

### Список шаблонов
```http
GET /api/v1/canvases/templates
→ 200 CanvasOut[]
```

---

## 3. Nodes

### Типы и статусы
- `type ∈ { "source", "extract", "format" }`
- `status ∈ { "idle", "running", "done", "error" }`

### Создать ноду
```http
POST /api/v1/canvases/{canvas_id}/nodes
{
  "type": "source",
  "position_x": 120,
  "position_y": 80,
  "data": {}                            // см. NodeData ниже
}
→ 201 NodeOut
```

### Обновить
```http
PATCH /api/v1/nodes/{node_id}
{
  "position_x": 200,                    // optional
  "position_y": 150,                    // optional
  "data": { ... },                      // optional, перезаписывает целиком
  "status": "done"                      // optional, обычно ставит сервер
}
→ 200 NodeOut
```

### Удалить
```http
DELETE /api/v1/nodes/{node_id}
→ 204
```

### NodeData по типам (формат `data` JSON)

**source:**
```ts
{
  input_type: "text" | "url" | "youtube" | "file_upload",
  content: string,                      // итоговый текст (или транскрипт)
  url?: string | null,
  youtube_url?: string | null,
  youtube_video_id?: string | null,
  youtube_title?: string | null,
  youtube_duration_seconds?: number | null,
  file_name?: string | null,
  file_size_bytes?: number | null,
  file_type?: string | null,
  transcript_method?: "youtube_captions" | "whisper" | null,
  transcript_language?: string | null,
  platform?: string | null,             // telegram | instagram | linkedin | web | manual
  author?: string | null,
  notes?: string | null
}
```

**extract:**
```ts
{
  talking_points: TalkingPoint[],
  selected_index: number | null         // какой тезис передаётся в Format
}

TalkingPoint = {
  text: string,
  score_breakdown: {
    audience_fit: number,        // 1..5
    engagement_trigger: number,  // 1..5
    uniqueness: number,          // 1..5
    author_fit: number           // 1..5
  },
  viral_score: number,           // 4..20
  category: string,              // мышление | продукты | ...
  reasoning: string
}
```

**format:**
```ts
{
  platform: "telegram" | "instagram" | "linkedin" | "twitter" | "article",
  talking_point_text: string,    // snapshot входа
  hooks: string[],               // 3 варианта
  selected_hook_index: number,
  body: string,
  cta: string,
  full_text: string              // готовый текст для копирования/публикации
}
```

---

## 4. Edges

Допустимые типы связей: `source→extract`, `extract→format`, `source→format`. Иначе 422.

### Создать связь
```http
POST /api/v1/canvases/{canvas_id}/edges
{
  "source_node_id": "<uuid>",
  "target_node_id": "<uuid>"
}
→ 201 EdgeOut
```

### Удалить
```http
DELETE /api/v1/edges/{edge_id}
→ 204
```

---

## 5. Skill Runs

### Запустить скилл на ноде
```http
POST /api/v1/nodes/{node_id}/run
→ 202 { "skill_run_id": "<uuid>", "status": "running" }
```
Сервер сам выбирает скилл по типу:
- `extract` → `viral_talking_points`
- `format` → `{platform}_creator` (telegram_creator, linkedin_creator, …)

### Статус скилл-рана (polling)
```http
GET /api/v1/skill-runs/{skill_run_id}
→ 200
{
  "id", "node_id", "skill", "status", "error",
  "duration_ms", "created_at", "completed_at"
}
```
status: `pending | running | completed | failed`

При `completed` — нужно перезагрузить родительскую ноду (`PATCH` уже применён сервером, фронт делает `GET /api/v1/canvases/{id}`).

### Real-time (SSE) — рекомендуется вместо polling
```http
GET /api/v1/skill-runs/{skill_run_id}/stream?token=<access_token>
Accept: text/event-stream
```
SSE endpoint принимает access token либо через `Authorization: Bearer ...` (для серверных клиентов), либо через query-param `?token=...` (для браузерных EventSource, который не умеет ставить кастомные заголовки). Используй query — `Authorization`-заголовок здесь не пройдёт через `EventSource`.
События:
- `event: status\ndata: {"status": "running"}\n\n`
- `event: progress\ndata: {"step": "transcribing", "percent": 45}\n\n`
- `event: complete\ndata: {"node_id": "...", "snapshot": <NodeOut>}\n\n`
- `event: error\ndata: {"message": "..."}\n\n`

Фронт держит open стрим до `complete` или `error`.

---

## 6. Транскрипция

```http
POST /api/v1/nodes/{node_id}/transcribe-youtube
{ "url": "https://youtube.com/watch?v=..." }
→ 202 { "skill_run_id": "<uuid>" }

POST /api/v1/nodes/{node_id}/upload-audio
Content-Type: multipart/form-data
file=<binary>
→ 202 { "skill_run_id": "<uuid>" }

GET /api/v1/nodes/{node_id}/youtube-meta?url=...
→ 200 { "title", "duration_seconds", "channel" }
```

---

## 7. Knowledge Layer

```http
GET    /api/v1/projects                  → ProjectOut[]
POST   /api/v1/projects                  → ProjectOut
PATCH  /api/v1/projects/{id}             → ProjectOut
DELETE /api/v1/projects/{id}             → 204

GET    /api/v1/brand-context             → BrandContextOut
PUT    /api/v1/brand-context             → BrandContextOut

GET    /api/v1/knowledge?type=&project_id=&is_dormant=  → KnowledgeItemOut[]
POST   /api/v1/knowledge                 → KnowledgeItemOut
PATCH  /api/v1/knowledge/{id}            → KnowledgeItemOut
DELETE /api/v1/knowledge/{id}            → 204
GET    /api/v1/knowledge/dormant         → KnowledgeItemOut[]

POST   /api/v1/nodes/{id}/knowledge/{item_id}    → 204
DELETE /api/v1/nodes/{id}/knowledge/{item_id}    → 204
GET    /api/v1/nodes/{id}/knowledge              → KnowledgeItemOut[]
```

---

## 8. Publishing (Telegram)

```http
GET    /api/v1/telegram-targets          → TelegramTargetOut[]
POST   /api/v1/telegram-targets          → TelegramTargetOut
DELETE /api/v1/telegram-targets/{id}     → 204

POST   /api/v1/nodes/{node_id}/publish
       { "target_id": "<uuid>" }         → 202 { "publish_log_id": "<uuid>" }
GET    /api/v1/publish-logs/{id}         → PublishLogOut
```

---

## Рекомендации фронту

- **Хранение токена:** `access_token` — в памяти / sessionStorage (не localStorage). `refresh_token` — httpOnly cookie если возможно, иначе sessionStorage.
- **Авто-refresh:** при 401 — попробовать refresh, если 401 повторно — logout.
- **Канвас открыт = один SSE-стрим на каждый running skill-run.** Не больше — heavy.
- **Гонки при PATCH:** если редактируют одновременно много полей — батчить debounce 300ms.
- **Удаление:** перед DELETE с контентом — модалка подтверждения (как в прототипе `DeleteConfirmModal`).
- **CORS dev:** API разрешает `http://localhost:3000` и `http://localhost:5173`. Если фронт на другом порту — попроси добавить.

---

## Версионирование

При breaking changes — новый prefix `/api/v2`. До этого все добавления — additive (новые поля nullable, новые endpoint'ы).
