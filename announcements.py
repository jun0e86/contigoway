"""
announcements.py

로그인 전/후 화면에 뜨는 공지 팝업.
  GET    /announcements/active         - 현재 노출 조건(is_active + 기간)을 만족하는 공지 1건 (인증 불필요)
  GET    /announcements                - 관리자 전용: 전체 공지 이력
  POST   /announcements                - 관리자 전용: 새 공지 게시 (기간 지정 가능)
  PUT    /announcements/{id}           - 관리자 전용: 공지 내용/기간 수정
  DELETE /announcements/{id}           - 관리자 전용: 공지 완전 삭제
  POST   /announcements/{id}/deactivate - 관리자 전용: 공지 숨김
  POST   /announcements/{id}/activate   - 관리자 전용: 숨겼던 예전 공지 다시 게시(복구)
"""

from datetime import date as date_cls
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
    start_at: Optional[date_cls] = None  # 노출 시작일 (비우면 즉시부터)
    end_at: Optional[date_cls] = None    # 노출 종료일 (비우면 숨기기 전까지 무기한)


class AnnouncementUpdate(BaseModel):
    title: str
    content: str
    start_at: Optional[date_cls] = None
    end_at: Optional[date_cls] = None


def _validate_range(start_at: Optional[date_cls], end_at: Optional[date_cls]):
    if start_at and end_at and start_at > end_at:
        raise HTTPException(400, "시작일이 종료일보다 늦을 수 없습니다")


def _serialize(a: Announcement) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "content": a.content,
        "is_active": a.is_active,
        "start_at": a.start_at.isoformat() if a.start_at else None,
        "end_at": a.end_at.isoformat() if a.end_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


@router.get("/active")
def get_active(db: Session = Depends(get_db)):
    """is_active=True 이면서 오늘 날짜가 노출 기간(start_at~end_at) 안에 드는 공지 1건.
    기간이 비어있는 쪽은 제한 없음으로 취급. 인증 불필요."""
    today = date_cls.today()
    ann = (
        db.query(Announcement)
        .filter(Announcement.is_active.is_(True))
        .filter((Announcement.start_at.is_(None)) | (Announcement.start_at <= today))
        .filter((Announcement.end_at.is_(None)) | (Announcement.end_at >= today))
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
    """새 공지 게시. 기존 활성 공지가 있으면 자동으로 비활성화됨(항상 최신 1건만 활성 상태)."""
    if not data.title.strip() or not data.content.strip():
        raise HTTPException(400, "제목과 내용을 입력해주세요")
    _validate_range(data.start_at, data.end_at)

    db.query(Announcement).filter(Announcement.is_active.is_(True)).update({"is_active": False})
    ann = Announcement(
        title=data.title.strip(),
        content=data.content.strip(),
        is_active=True,
        start_at=data.start_at,
        end_at=data.end_at,
        created_by=admin_user.id,
    )
    db.add(ann)
    db.commit()
    db.refresh(ann)
    return _serialize(ann)


@router.put("/{ann_id}")
def update_announcement(
    ann_id: int,
    data: AnnouncementUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """공지 제목/내용/노출 기간 수정. is_active 상태는 건드리지 않음(따로 숨기기/다시게시 사용)."""
    ann = db.query(Announcement).filter(Announcement.id == ann_id).first()
    if not ann:
        raise HTTPException(404, "공지를 찾을 수 없습니다")
    if not data.title.strip() or not data.content.strip():
        raise HTTPException(400, "제목과 내용을 입력해주세요")
    _validate_range(data.start_at, data.end_at)

    ann.title = data.title.strip()
    ann.content = data.content.strip()
    ann.start_at = data.start_at
    ann.end_at = data.end_at
    db.commit()
    db.refresh(ann)
    return _serialize(ann)


@router.delete("/{ann_id}")
def delete_announcement(
    ann_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """공지를 완전히 삭제(복구 불가). 단순히 숨기고 싶으면 /deactivate 사용."""
    ann = db.query(Announcement).filter(Announcement.id == ann_id).first()
    if not ann:
        raise HTTPException(404, "공지를 찾을 수 없습니다")
    db.delete(ann)
    db.commit()
    return {"message": "공지가 삭제되었습니다"}


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


@router.post("/{ann_id}/activate")
def activate(
    ann_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """숨겨졌던 예전 공지를 다시 활성화(복구). 기존 활성 공지는 자동 비활성화됨."""
    ann = db.query(Announcement).filter(Announcement.id == ann_id).first()
    if not ann:
        raise HTTPException(404, "공지를 찾을 수 없습니다")
    db.query(Announcement).filter(Announcement.is_active.is_(True)).update({"is_active": False})
    ann.is_active = True
    db.commit()
    return {"message": "공지가 다시 게시되었습니다"}
