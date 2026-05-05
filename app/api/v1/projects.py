import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.knowledge import Project
from app.schemas.knowledge import ProjectCreate, ProjectOut, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])


async def _owned(db, project_id: uuid.UUID, org_id: uuid.UUID) -> Project:
    obj = await db.scalar(
        select(Project).where(Project.id == project_id, Project.organization_id == org_id)
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return obj


@router.get("", response_model=list[ProjectOut])
async def list_projects(current: CurrentUser, db: DbSession) -> list[ProjectOut]:
    rows = await db.scalars(
        select(Project)
        .where(Project.organization_id == current.organization_id)
        .order_by(Project.created_at.desc())
    )
    return [ProjectOut.model_validate(r) for r in rows.all()]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate, current: CurrentUser, db: DbSession
) -> ProjectOut:
    obj = Project(
        organization_id=current.organization_id,
        name=payload.name,
        color=payload.color,
        context=payload.context,
    )
    db.add(obj)
    await db.flush()
    return ProjectOut.model_validate(obj)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, current: CurrentUser, db: DbSession
) -> ProjectOut:
    obj = await _owned(db, project_id, current.organization_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await db.flush()
    return ProjectOut.model_validate(obj)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID, current: CurrentUser, db: DbSession
) -> None:
    obj = await _owned(db, project_id, current.organization_id)
    await db.delete(obj)
