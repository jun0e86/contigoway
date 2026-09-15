"""
todo.py

오늘의 TODO 리스트 API.

기본:
  GET    /todo?target_date=YYYY-MM-DD   - 특정 날짜(기본값 오늘) 내 TODO 목록 조회
  POST   /todo                          - 새 TODO 추가
  PUT    /todo/{id}                     - 내용/카테고리/우선순위/진행률 수정
  PATCH  /todo/{id}/toggle              - 완료/미완료 토글 (완료 시 completed_at 기록)
  PATCH  /todo/{id}/progress            - 진행률(progress_current)만 빠르게 업데이트
  DELETE /todo/{id}                     - 삭제

대분류 카테고리 (청구작업/오더리뷰/전화예약 등, 사용자가 자유롭게 추가/수정):
  GET    /todo/categories
  POST   /todo/categories
  PUT    /todo/categories/{id}
  DELETE /todo/categories/{id}

하위 체크리스트 (할 일 하나를 작은 단계로 쪼개서 체크):
  POST   /todo/{id}/subitems
  PATCH  /todo/subitems/{sub_id}/toggle
  PUT    /todo/subitems/{sub_id}
  DELETE /todo/subitems/{sub_id}

사진 첨부 (jpg/jpeg/png/heic/heif -> 썸네일 자동 생성, heic는 JPEG로 변환해서 저장):
  POST   /todo/{id}/media
  DELETE /todo/media/{media_id}
"""

import csv
import io
import os
import uuid
from datetime import date as date_cls, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import TodoItem, TodoCategory, TodoSubItem, TodoMedia, User
from auth import get_current_user

router = APIRouter(prefix="/todo", tags=["TODO"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/app/uploads")
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
CONVERT_TO_JPEG_EXT = {".heic", ".heif"}
MAX_FILE_SIZE_MB = int(os.environ.get("TODO_MAX_FILE_SIZE_MB", "30"))
VALID_PRIORITIES = {"red", "yellow", "green"}


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------
class CategoryCreate(BaseModel):
    name: str
    emoji: Optional[str] = None
    color: Optional[str] = None


class CategoryOut(BaseModel):
    id: int
    name: str
    emoji: Optional[str]
    color: Optional[str]
    sort_order: int

    class Config:
        from_attributes = True


class SubItemCreate(BaseModel):
    content: str


class SubItemOut(BaseModel):
    id: int
    content: str
    is_done: bool

    class Config:
        from_attributes = True


class TodoCreate(BaseModel):
    content: str
    todo_date: Optional[date_cls] = None
    category_id: Optional[int] = None
    priority: Optional[str] = None
    progress_start: Optional[int] = None
    progress_end: Optional[int] = None
    progress_current: Optional[int] = None


class TodoUpdate(BaseModel):
    content: str
    category_id: Optional[int] = None
    priority: Optional[str] = None
    progress_start: Optional[int] = None
    progress_end: Optional[int] = None
    progress_current: Optional[int] = None


class ProgressUpdate(BaseModel):
    progress_current: int


class MediaOut(BaseModel):
    id: int
    url: str
    thumbnail_url: Optional[str] = None


class TodoOut(BaseModel):
    id: int
    content: str
    is_done: bool
    todo_date: date_cls
    created_at: datetime
    completed_at: Optional[datetime] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    category_emoji: Optional[str] = None
    category_color: Optional[str] = None
    priority: Optional[str] = None
    progress_start: Optional[int] = None
    progress_end: Optional[int] = None
    progress_current: Optional[int] = None
    subitems: list[SubItemOut] = []
    media: list[MediaOut] = []


def _to_out(item: TodoItem) -> TodoOut:
    cat = item.category
    return TodoOut(
        id=item.id,
        content=item.content,
        is_done=item.is_done,
        todo_date=item.todo_date,
        created_at=item.created_at,
        completed_at=item.completed_at,
        category_id=item.category_id,
        category_name=cat.name if cat else None,
        category_emoji=cat.emoji if cat else None,
        category_color=cat.color if cat else None,
        priority=item.priority,
        progress_start=item.progress_start,
        progress_end=item.progress_end,
        progress_current=item.progress_current,
        subitems=[SubItemOut.model_validate(s) for s in item.subitems],
        media=[
            MediaOut(
                id=m.id,
                url=f"/uploads/{m.file_path}",
                thumbnail_url=f"/uploads/{m.thumbnail_path}" if m.thumbnail_path else None,
            )
            for m in item.media
        ],
    )


def _validate_priority(priority: Optional[str]):
    if priority is not None and priority not in VALID_PRIORITIES:
        raise HTTPException(400, "priority는 red/yellow/green 중 하나여야 합니다")


# ---------------------------------------------------------------------------
# 카테고리 (대분류)
# ---------------------------------------------------------------------------
@router.get("/categories", response_model=list[CategoryOut])
def list_categories(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(TodoCategory)
        .filter(TodoCategory.user_id == current_user.id)
        .order_by(TodoCategory.sort_order, TodoCategory.id)
        .all()
    )


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(
    data: CategoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not data.name.strip():
        raise HTTPException(400, "카테고리 이름을 입력해주세요")
    count = db.query(TodoCategory).filter(TodoCategory.user_id == current_user.id).count()
    cat = TodoCategory(
        user_id=current_user.id,
        name=data.name.strip(),
        emoji=data.emoji,
        color=data.color,
        sort_order=count,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def _get_owned_category(category_id: int, db: Session, current_user: User) -> TodoCategory:
    cat = (
        db.query(TodoCategory)
        .filter(TodoCategory.id == category_id, TodoCategory.user_id == current_user.id)
        .first()
    )
    if not cat:
        raise HTTPException(404, "카테고리를 찾을 수 없습니다")
    return cat


@router.put("/categories/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: int,
    data: CategoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cat = _get_owned_category(category_id, db, current_user)
    if not data.name.strip():
        raise HTTPException(400, "카테고리 이름을 입력해주세요")
    cat.name = data.name.strip()
    cat.emoji = data.emoji
    cat.color = data.color
    db.commit()
    db.refresh(cat)
    return cat


@router.delete("/categories/{category_id}")
def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cat = _get_owned_category(category_id, db, current_user)
    # 이 카테고리를 쓰던 할 일들은 삭제하지 않고 "카테고리 없음" 상태로 되돌림
    db.query(TodoItem).filter(
        TodoItem.category_id == category_id, TodoItem.user_id == current_user.id
    ).update({"category_id": None})
    db.delete(cat)
    db.commit()
    return {"message": "카테고리가 삭제되었습니다"}


# ---------------------------------------------------------------------------
# TODO 항목
# ---------------------------------------------------------------------------
@router.get("", response_model=list[TodoOut])
def list_todos(
    target_date: Optional[date_cls] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q_date = target_date or date_cls.today()
    items = (
        db.query(TodoItem)
        .filter(TodoItem.user_id == current_user.id, TodoItem.todo_date == q_date)
        .order_by(TodoItem.created_at)
        .all()
    )
    return [_to_out(i) for i in items]


@router.post("", response_model=TodoOut, status_code=201)
def create_todo(
    data: TodoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not data.content.strip():
        raise HTTPException(400, "내용을 입력해주세요")
    _validate_priority(data.priority)
    if data.category_id is not None:
        _get_owned_category(data.category_id, db, current_user)

    item = TodoItem(
        user_id=current_user.id,
        content=data.content.strip(),
        todo_date=data.todo_date or date_cls.today(),
        category_id=data.category_id,
        priority=data.priority,
        progress_start=data.progress_start,
        progress_end=data.progress_end,
        progress_current=data.progress_current,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _to_out(item)


def _get_owned_todo(todo_id: int, db: Session, current_user: User) -> TodoItem:
    item = (
        db.query(TodoItem)
        .filter(TodoItem.id == todo_id, TodoItem.user_id == current_user.id)
        .first()
    )
    if not item:
        raise HTTPException(404, "TODO 항목을 찾을 수 없습니다")
    return item


@router.patch("/{todo_id}/toggle", response_model=TodoOut)
def toggle_todo(
    todo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_owned_todo(todo_id, db, current_user)
    item.is_done = not item.is_done
    item.completed_at = datetime.now(timezone.utc) if item.is_done else None
    db.commit()
    db.refresh(item)
    return _to_out(item)


@router.patch("/{todo_id}/progress", response_model=TodoOut)
def update_progress(
    todo_id: int,
    data: ProgressUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_owned_todo(todo_id, db, current_user)
    item.progress_current = data.progress_current
    db.commit()
    db.refresh(item)
    return _to_out(item)


@router.put("/{todo_id}", response_model=TodoOut)
def update_todo(
    todo_id: int,
    data: TodoUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_owned_todo(todo_id, db, current_user)
    if not data.content.strip():
        raise HTTPException(400, "내용을 입력해주세요")
    _validate_priority(data.priority)
    if data.category_id is not None:
        _get_owned_category(data.category_id, db, current_user)

    item.content = data.content.strip()
    item.category_id = data.category_id
    item.priority = data.priority
    item.progress_start = data.progress_start
    item.progress_end = data.progress_end
    item.progress_current = data.progress_current
    db.commit()
    db.refresh(item)
    return _to_out(item)


@router.delete("/{todo_id}")
def delete_todo(
    todo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_owned_todo(todo_id, db, current_user)
    for m in item.media:
        for p in (m.file_path, m.thumbnail_path):
            if p:
                abs_p = os.path.join(UPLOAD_DIR, p)
                if os.path.exists(abs_p):
                    os.remove(abs_p)
    db.delete(item)
    db.commit()
    return {"message": "삭제되었습니다"}


# ---------------------------------------------------------------------------
# 스프레드시트 연동 (엑셀/CSV 내보내기, 가져오기)
# ---------------------------------------------------------------------------
@router.get("/export")
def export_todos(
    format: str = "xlsx",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = (
        db.query(TodoItem)
        .filter(TodoItem.user_id == current_user.id)
        .order_by(TodoItem.todo_date, TodoItem.created_at)
        .all()
    )
    rows = [
        [
            i.todo_date.isoformat(),
            i.content,
            "완료" if i.is_done else "미완료",
            i.category.name if i.category else "",
        ]
        for i in items
    ]
    header = ["날짜", "내용", "상태", "카테고리"]

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(rows)
        data = buf.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(data),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=todo_export.csv"},
        )

    wb = Workbook()
    ws = wb.active
    ws.title = "TODO"
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=todo_export.xlsx"},
    )


@router.post("/import")
async def import_todos(
    file: UploadFile = File(...),
    default_date: Optional[date_cls] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    content = await file.read()
    fallback_date = default_date or date_cls.today()
    count = 0

    if ext == ".csv":
        text = content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        if rows and rows[0] and "내용" in "".join(rows[0]):
            rows = rows[1:]
        for row in rows:
            if not row:
                continue
            date_str = row[0] if len(row) > 1 else None
            todo_content = row[1] if len(row) > 1 else row[0]
            if not todo_content or not todo_content.strip():
                continue
            try:
                d = date_cls.fromisoformat(date_str) if date_str else fallback_date
            except Exception:
                d = fallback_date
            db.add(TodoItem(user_id=current_user.id, content=todo_content.strip(), todo_date=d))
            count += 1

    elif ext in (".xlsx", ".xls"):
        wb = load_workbook(io.BytesIO(content))
        ws = wb.active
        for idx, row in enumerate(ws.iter_rows(values_only=True)):
            if idx == 0 and row and isinstance(row[0], str) and "날짜" in row[0]:
                continue
            if not row:
                continue
            date_val = row[0] if len(row) > 0 else None
            content_val = row[1] if len(row) > 1 else None
            if not content_val or not str(content_val).strip():
                continue
            if isinstance(date_val, datetime):
                d = date_val.date()
            elif isinstance(date_val, date_cls):
                d = date_val
            else:
                d = fallback_date
            db.add(TodoItem(user_id=current_user.id, content=str(content_val).strip(), todo_date=d))
            count += 1
    else:
        raise HTTPException(400, "xlsx 또는 csv 파일만 가져올 수 있습니다")

    db.commit()
    return {"message": f"{count}건을 가져왔습니다"}


# ---------------------------------------------------------------------------
# 하위 체크리스트
# ---------------------------------------------------------------------------
@router.post("/{todo_id}/subitems", response_model=SubItemOut, status_code=201)
def create_subitem(
    todo_id: int,
    data: SubItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_owned_todo(todo_id, db, current_user)
    if not data.content.strip():
        raise HTTPException(400, "내용을 입력해주세요")
    sort_order = len(item.subitems)
    sub = TodoSubItem(todo_id=item.id, content=data.content.strip(), sort_order=sort_order)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _get_owned_subitem(sub_id: int, db: Session, current_user: User) -> TodoSubItem:
    sub = (
        db.query(TodoSubItem)
        .join(TodoItem, TodoSubItem.todo_id == TodoItem.id)
        .filter(TodoSubItem.id == sub_id, TodoItem.user_id == current_user.id)
        .first()
    )
    if not sub:
        raise HTTPException(404, "체크리스트 항목을 찾을 수 없습니다")
    return sub


@router.patch("/subitems/{sub_id}/toggle", response_model=SubItemOut)
def toggle_subitem(
    sub_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = _get_owned_subitem(sub_id, db, current_user)
    sub.is_done = not sub.is_done
    db.commit()
    db.refresh(sub)
    return sub


@router.put("/subitems/{sub_id}", response_model=SubItemOut)
def update_subitem(
    sub_id: int,
    data: SubItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = _get_owned_subitem(sub_id, db, current_user)
    if not data.content.strip():
        raise HTTPException(400, "내용을 입력해주세요")
    sub.content = data.content.strip()
    db.commit()
    db.refresh(sub)
    return sub


@router.delete("/subitems/{sub_id}")
def delete_subitem(
    sub_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = _get_owned_subitem(sub_id, db, current_user)
    db.delete(sub)
    db.commit()
    return {"message": "삭제되었습니다"}


# ---------------------------------------------------------------------------
# 사진 첨부
# ---------------------------------------------------------------------------
def _save_todo_image(upload: UploadFile, user_id: int) -> TodoMedia:
    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise HTTPException(
            400, f"지원하지 않는 파일 형식입니다: {ext} (jpg/jpeg/png/heic/heif만 가능)"
        )

    user_dir = os.path.join(UPLOAD_DIR, "todo", str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    temp_filename = f"{uuid.uuid4().hex}{ext}"
    temp_abs_path = os.path.join(user_dir, temp_filename)

    size = 0
    with open(temp_abs_path, "wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE_MB * 1024 * 1024:
                out.close()
                os.remove(temp_abs_path)
                raise HTTPException(400, f"파일 용량은 {MAX_FILE_SIZE_MB}MB를 넘을 수 없습니다")
            out.write(chunk)

    filename = temp_filename
    abs_path = temp_abs_path
    thumb_rel_path = None

    try:
        from PIL import Image

        if ext in CONVERT_TO_JPEG_EXT:
            import pillow_heif

            pillow_heif.register_heif_opener()
            jpeg_filename = f"{uuid.uuid4().hex}.jpg"
            jpeg_abs_path = os.path.join(user_dir, jpeg_filename)
            with Image.open(abs_path) as img:
                img.convert("RGB").save(jpeg_abs_path, "JPEG", quality=92)
            os.remove(abs_path)
            filename = jpeg_filename
            abs_path = jpeg_abs_path

        thumb_filename = f"thumb_{filename}"
        thumb_abs_path = os.path.join(user_dir, thumb_filename)
        with Image.open(abs_path) as img:
            img.convert("RGB").thumbnail((300, 300))
            img.save(thumb_abs_path, "JPEG", quality=85)
        thumb_rel_path = os.path.join("todo", str(user_id), thumb_filename)
    except HTTPException:
        raise
    except Exception:
        if ext in CONVERT_TO_JPEG_EXT:
            if os.path.exists(abs_path):
                os.remove(abs_path)
            raise HTTPException(
                400, f"{ext} 파일을 처리하지 못했습니다. 파일이 손상되었을 수 있습니다"
            )
        thumb_rel_path = None

    rel_path = os.path.join("todo", str(user_id), filename)

    return TodoMedia(
        file_path=rel_path,
        thumbnail_path=thumb_rel_path,
        original_filename=upload.filename,
    )


@router.post("/{todo_id}/media", response_model=MediaOut, status_code=201)
def upload_media(
    todo_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_owned_todo(todo_id, db, current_user)
    media = _save_todo_image(file, current_user.id)
    media.todo_id = item.id
    db.add(media)
    db.commit()
    db.refresh(media)
    return MediaOut(
        id=media.id,
        url=f"/uploads/{media.file_path}",
        thumbnail_url=f"/uploads/{media.thumbnail_path}" if media.thumbnail_path else None,
    )


@router.delete("/media/{media_id}")
def delete_media(
    media_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    media = (
        db.query(TodoMedia)
        .join(TodoItem, TodoMedia.todo_id == TodoItem.id)
        .filter(TodoMedia.id == media_id, TodoItem.user_id == current_user.id)
        .first()
    )
    if not media:
        raise HTTPException(404, "사진을 찾을 수 없습니다")
    for p in (media.file_path, media.thumbnail_path):
        if p:
            abs_p = os.path.join(UPLOAD_DIR, p)
            if os.path.exists(abs_p):
                os.remove(abs_p)
    db.delete(media)
    db.commit()
    return {"message": "삭제되었습니다"}
