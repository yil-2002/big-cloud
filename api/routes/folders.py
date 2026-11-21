"""``/folders`` — the folder tree (``umumiy`` == root, as in the legacy bot).

The old bot stored folders as *global* names with files carrying a folder string.
Here folders belong to a user and form a real tree (``parent_id``), while the
``umumiy`` concept survives as "root" (``parent_id IS NULL``).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from api.dependencies import get_current_user, get_storage_service, throttle
from api.schemas import FolderCreate, FolderOut, FolderUpdate, MessageResponse
from api.serializers import serialize_folder
from db.models import File, Folder, User
from services.exceptions import DomainError, FolderNotEmptyError, FolderNotFoundError
from services.storage import StorageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/folders", tags=["folders"])


def _raise(exc: DomainError) -> HTTPException:
    """Domain error -> HTTP error envelope."""
    return HTTPException(status_code=exc.status_code, detail=exc.to_dict())


async def _counters(storage: StorageService, folder_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """One query for files/sub-folders/size of many folders (avoids N+1)."""
    if not folder_ids:
        return {}
    file_rows = (
        await storage.session.execute(
            select(
                File.folder_id,
                func.count(File.id),
                func.coalesce(func.sum(File.file_size), 0),
            )
            .where(File.folder_id.in_(folder_ids))
            .group_by(File.folder_id)
        )
    ).all()
    folder_rows = (
        await storage.session.execute(
            select(Folder.parent_id, func.count(Folder.id))
            .where(Folder.parent_id.in_(folder_ids))
            .group_by(Folder.parent_id)
        )
    ).all()

    result: dict[uuid.UUID, dict[str, int]] = {
        folder_id: {"files": 0, "folders": 0, "size": 0} for folder_id in folder_ids
    }
    for folder_id, count, size in file_rows:
        result[folder_id] = {"files": int(count), "folders": result[folder_id]["folders"], "size": int(size)}
    for parent_id, count in folder_rows:
        result[parent_id]["folders"] = int(count)
    return result


@router.get("", response_model=list[FolderOut], summary="List folders")
async def list_folders(
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    parent_id: Annotated[
        uuid.UUID | None, Query(description="Omit for root-level folders")
    ] = None,
    deep: Annotated[bool, Query(description="Return the whole tree instead of one level")] = False,
) -> list[FolderOut]:
    """Folders of one level, or the whole tree with ``deep=true``."""
    from sqlalchemy.orm import selectinload

    if deep:
        rows = (
            await storage.session.scalars(
                select(Folder)
                .where(Folder.user_id == user.telegram_id)
                .options(selectinload(Folder.parent))
                .order_by(Folder.name)
            )
        ).unique().all()
    else:
        rows = await storage.list_folders(user.telegram_id, parent_id=parent_id)

    counters = await _counters(storage, [folder.id for folder in rows])
    return [
        serialize_folder(
            folder,
            files_count=counters.get(folder.id, {}).get("files", 0),
            folders_count=counters.get(folder.id, {}).get("folders", 0),
            size=counters.get(folder.id, {}).get("size", 0),
        )
        for folder in rows
    ]


@router.post(
    "",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a folder",
    dependencies=[Depends(throttle("api"))],
)
async def create_folder(
    payload: FolderCreate,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> FolderOut:
    """Create a folder (duplicate sibling names are idempotent)."""
    try:
        folder = await storage.create_folder(payload.name, user.telegram_id, payload.parent_id)
    except DomainError as exc:
        raise _raise(exc) from exc
    return serialize_folder(folder)


@router.get("/{folder_id}", response_model=FolderOut, summary="Folder details")
async def get_folder(
    folder_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> FolderOut:
    try:
        folder = await storage.get_folder(folder_id, user.telegram_id)
    except FolderNotFoundError as exc:
        raise _raise(exc) from exc
    counters = (await _counters(storage, [folder.id])).get(folder.id, {})
    return serialize_folder(
        folder,
        files_count=counters.get("files", 0),
        folders_count=counters.get("folders", 0),
        size=counters.get("size", 0),
    )


@router.patch("/{folder_id}", response_model=FolderOut, summary="Rename / move a folder")
async def update_folder(
    folder_id: uuid.UUID,
    payload: FolderUpdate,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> FolderOut:
    """Rename and/or move; cycles are rejected by the service layer."""
    try:
        folder = await storage.get_folder(folder_id, user.telegram_id)
        if payload.name is not None:
            folder = await storage.rename_folder(folder_id, user.telegram_id, payload.name)
        if payload.move_to_root or payload.parent_id is not None:
            destination = None if payload.move_to_root else payload.parent_id
            folder = await storage.move_folder(folder_id, user.telegram_id, destination)
    except DomainError as exc:
        raise _raise(exc) from exc
    return serialize_folder(folder)


@router.delete("/{folder_id}", response_model=MessageResponse, summary="Delete a folder")
async def delete_folder(
    folder_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    cascade: Annotated[
        bool, Query(description="Delete contained files too (409 otherwise)")
    ] = False,
) -> MessageResponse:
    """Delete a folder; ``cascade=true`` removes its files and frees quota."""
    try:
        removed = await storage.delete_folder(folder_id, user.telegram_id, cascade=cascade)
    except (FolderNotFoundError, FolderNotEmptyError) as exc:
        raise _raise(exc) from exc
    return MessageResponse(
        ok=True,
        message="Folder deleted" + (f", {removed} file(s) removed" if removed else ""),
    )
