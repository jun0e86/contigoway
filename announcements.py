"""
announcements.py

로그인 전/후 화면에 뜨는 공지 팝업.
  GET  /announcements/active         - 현재 활성 공지 1건 (인증 불필요 - 로그인 화면에도 표시)
  GET  /announcements                - 관리자 전용: 전체 공지 이력
  POST /announcements                - 관리자 전용: 새 공지 게시 (기존 활성 공지는 자동 비활성화)
  POST /announcements/{id}/deactivate - 관리자 전용: 공지 숨김
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Announcement, User
from auth import get_current_user

router = APIRouter(prefix="/announcements", tags=["공지 팝업"])


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다")
    return current_user


class AnnouncementCreate(BaseModel):
    title: str
    content: str


def _serialize(a: Announcement) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "content": a.content,
        "is_active": a.is_active,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("/active")
def get_active(db: Session = Depends(get_db)):
    """현재 활성화된 공지 1건 (없으면 null). 인증 불필요."""
    ann = (
        db.query(Announcement)
        .filter(Announcement.is_active.is_(True))
        .order_by(Announcement.created_at.desc())
        .first()
    )
    return _serialize(ann) if ann else None


@router.get("")
def list_announcements(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    items = db.query(Announcement).order_by(Announcement.created_at.desc()).all()
    return [_serialize(a) for a in items]


@router.post("", status_code=201)
def create_announcement(
    data: AnnouncementCreate,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """새 공지 게시. 기존 활성 공지가 있으면 자동으로 비활성화됨(항상 최신 1건만 노출)."""
    if not data.title.strip() or not data.content.strip():
        raise HTTPException(400, "제목과 내용을 입력해주세요")
    db.query(Announcement).filter(Announcement.is_active.is_(True)).update({"is_active": False})
    ann = Announcement(
        title=data.title.strip(),
        content=data.content.strip(),
        is_active=True,
        created_by=admin_user.id,
    )
    db.add(ann)
    db.commit()
    db.refresh(ann)
    return _serialize(ann)


@router.post("/{ann_id}/deactivate")
def deactivate(
    ann_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    ann = db.query(Announcement).filter(Announcement.id == ann_id).first()
    if not ann:
        raise HTTPException(404, "공지를 찾을 수 없습니다")
    ann.is_active = False
    db.commit()
    return {"message": "공지가 숨김 처리되었습니다"}
