"""
todo.py

오늘의 TODO 리스트 API.
  GET    /todo?date=YYYY-MM-DD   - 특정 날짜(기본값 오늘) 내 TODO 목록 조회
  POST   /todo                   - 새 TODO 추가
  PATCH  /todo/{id}/toggle       - 완료/미완료 토글
  PUT    /todo/{id}              - 내용 수정
  DELETE /todo/{id}              - 삭제
"""

from datetime import date as date_cls
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import TodoItem, User
from auth import get_current_user

router = APIRouter(prefix="/todo", tags=["TODO"])


class TodoCreate(BaseModel):
    content: str
    todo_date: Optional[date_cls] = None  # 미지정 시 오늘 날짜


class TodoUpdate(BaseModel):
    content: str


class TodoOut(BaseModel):
    id: int
    content: str
    is_done: bool
    todo_date: date_cls

    class Config:
        from_attributes = True


@router.get("", response_model=list[TodoOut])
def list_todos(
    target_date: Optional[date_cls] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q_date = target_date or date_cls.today()
    return (
        db.query(TodoItem)
        .filter(TodoItem.user_id == current_user.id, TodoItem.todo_date == q_date)
        .order_by(TodoItem.created_at)
        .all()
    )


@router.post("", response_model=TodoOut, status_code=201)
def create_todo(
    data: TodoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not data.content.strip():
        raise HTTPException(400, "내용을 입력해주세요")
    item = TodoItem(
        user_id=current_user.id,
        content=data.content.strip(),
        todo_date=data.todo_date or date_cls.today(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


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
    db.commit()
    db.refresh(item)
    return item


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
    item.content = data.content.strip()
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{todo_id}")
def delete_todo(
    todo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_owned_todo(todo_id, db, current_user)
    db.delete(item)
    db.commit()
    return {"message": "삭제되었습니다"}
