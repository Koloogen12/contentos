
## Чек-лист: новый формат контента или режим правки

Цепочка длинная, и пропуск любого звена даёт ошибку не там, где была правка.
За один день это выстрелило трижды: незапущенный воркер, «нет вывода» в ноде,
два барьера валидации на разных уровнях.

Новый **формат** (платформа ноды контента):

1. `app/services/skills/<name>_creator.py` — сам навык с `@register`
2. `app/services/skills/__init__.py` — импорт модуля, иначе он не в реестре
3. `app/services/skills/base.py` → `FORMAT_PLATFORM_TO_SKILL` — платформа → навык
4. `app/services/brand_context.py` → `collect_input_for_skill` — что формат
   получает на вход. Проверить все три родителя: extract, source, llm
5. `alembic/` — миграция, если формат надо ставить в контент-план:
   `ck_planned_posts_platform` перечисляет платформы поимённо
6. Фронт: `lib/types.ts` → `FormatPlatform`
7. Фронт: `FormatNode.tsx` → `PLATFORM_LIST`, `PLATFORM_LABEL`
8. Фронт: `FormatNode.tsx` → `hasOutput` — **без своей ветки нода решит, что
   вывода нет**, и покажет превью тезиса вместо результата
9. Фронт: `FormatNode.tsx` → ветка отрисовки результата
10. Фронт: `lib/i18n.ts` → `runButtonByPlatform`, `runningStatusByPlatform`
11. Фронт: `CanvasFormatDrawer.tsx` → `PLATFORM_LABEL` и `USES_HOOK_BODY_CTA`,
    если у формата нет полей hook/body/cta

Новый **режим правки** (`tweak`):

1. `app/services/skills/tweak.py` — промпт и запись в `system_map`
   (условие входа выводится из словаря, отдельный список не заводить)
2. `app/api/v1/skill_runs.py` → `_FORMAT_TWEAKS` / `_EXTRACT_TWEAKS`
3. Фронт: `lib/tweaks.ts` → union режимов
4. Фронт: кнопка в `CanvasFormatDrawer.tsx` + подпись в `lib/i18n.ts`

**Деплой.** `api` и `worker` — два разных образа из одного контекста.
Собирать всегда оба: `docker compose build api worker`. Навыки выполняет
воркер, и без пересборки он молча остаётся на старом коде, а проверка
через `docker exec deploy-api-1` этого не покажет.

Секреты для compose нужны в оболочке, иначе интерполяция подставит пустые
пароли: `set -a; . /etc/contentos/secrets.env; set +a` перед `up -d`.
