"""
packing.py

여행 준비물 체크리스트 API (로그인한 회원이 함께 쓰는 공용 목록).
한 사람이 체크하면 다른 사람 화면에도 반영되고, 누가 체크했는지 기록된다.

  GET    /packing?trip=sapporo-2026-12   - 목록 조회 (처음 조회 시 기본 항목 자동 생성)
  POST   /packing                        - 항목 추가 {trip, category, name}
  PUT    /packing/{id}                   - 이름/카테고리 수정
  PATCH  /packing/{id}/toggle            - 체크/해제 (체크한 사람·시각 기록)
  DELETE /packing/{id}                   - 삭제

모델(PackingItem)은 이 파일에 정의되어 있고, main.py에서 이 파일을 import하면
Base.metadata.create_all()이 packing_items 테이블을 자동으로 만든다.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Session, relationship

from auth import get_current_user
from database import Base, get_db

router = APIRouter(prefix="/packing", tags=["여행 준비물"])

DEFAULT_TRIP = "sapporo-2026-12"
CATEGORIES = ["서류", "방한", "전자기기", "돈·결제", "생활용품", "기타"]

# 겨울 삿포로 기본 준비물 (처음 조회할 때 한 번만 생성)
DEFAULT_ITEMS = {
    "서류": ["여권 (유효기간 6개월 이상)", "항공권 e-티켓", "호텔 예약 확인서", "비에이 투어 바우처", "여행자보험 가입", "Visit Japan Web 등록"],
    "방한": ["미끄럼 방지 신발 / 아이젠", "롱패딩", "장갑 (터치 가능)", "목도리 · 넥워머", "귀마개 · 비니", "히트텍 상하의", "핫팩", "두꺼운 양말"],
    "전자기기": ["110V 돼지코 어댑터", "보조배터리 (기내 반입)", "휴대폰 충전기", "eSIM · 포켓와이파이"],
    "돈·결제": ["엔화 현금", "트래블카드", "해외결제 신용카드"],
    "생활용품": ["보습크림 · 립밤", "상비약 (감기약·소화제)", "선글라스 (설원 눈부심)", "지퍼백 · 비닐봉투"],
}


class PackingItem(Base):
    __tablename__ = "packing_items"

    id = Column(Integer, primary_key=True, index=True)
    trip = Column(String(50), nullable=False, index=True, default=DEFAULT_TRIP)
    category = Column(String(30), nullable=False, default="기타")
    name = Column(String(100), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)

    checked_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    checked_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    checker = relationship("User", foreign_keys=[checked_by])


class ItemCreate(BaseModel):
    trip: str = Field(default=DEFAULT_TRIP, max_length=50)
    category: str = Field(default="기타", max_length=30)
    name: str = Field(min_length=1, max_length=100)


class ItemUpdate(BaseModel):
    category: Optional[str] = Field(default=None, max_length=30)
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)


def _to_dict(it: PackingItem):
    return {
        "id": it.id,
        "trip": it.trip,
        "category": it.category,
        "name": it.name,
        "checked": it.checked_by is not None,
        "checked_by_name": it.checker.full_name if it.checker else None,
        "checked_at": it.checked_at.isoformat() if it.checked_at else None,
    }


def _seed(db: Session, trip: str, user_id: int):
    order = 0
    for cat in CATEGORIES:
        for name in DEFAULT_ITEMS.get(cat, []):
            db.add(PackingItem(trip=trip, category=cat, name=name, sort_order=order, created_by=user_id))
            order += 1
    db.commit()


def _get_item(db: Session, item_id: int) -> PackingItem:
    it = db.query(PackingItem).filter(PackingItem.id == item_id).first()
    if not it:
        raise HTTPException(404, "항목을 찾을 수 없습니다")
    return it


@router.get("")
def list_items(
    trip: str = Query(default=DEFAULT_TRIP, max_length=50),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    q = db.query(PackingItem).filter(PackingItem.trip == trip)
    if trip == DEFAULT_TRIP and q.count() == 0:
        _seed(db, trip, current_user.id)
    items = q.order_by(PackingItem.sort_order, PackingItem.id).all()
    done = sum(1 for i in items if i.checked_by is not None)
    return {"trip": trip, "categories": CATEGORIES, "items": [_to_dict(i) for i in items],
            "done": done, "total": len(items)}


@router.post("")
def add_item(body: ItemCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    max_order = db.query(func.max(PackingItem.sort_order)).filter(PackingItem.trip == body.trip).scalar() or 0
    it = PackingItem(trip=body.trip, category=body.category.strip() or "기타", name=body.name.strip(),
                     sort_order=max_order + 1, created_by=current_user.id)
    db.add(it)
    db.commit()
    db.refresh(it)
    return _to_dict(it)


@router.put("/{item_id}")
def update_item(item_id: int, body: ItemUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    it = _get_item(db, item_id)
    if body.name is not None:
        it.name = body.name.strip()
    if body.category is not None:
        it.category = body.category.strip() or "기타"
    db.commit()
    db.refresh(it)
    return _to_dict(it)


@router.patch("/{item_id}/toggle")
def toggle_item(item_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    it = _get_item(db, item_id)
    if it.checked_by is None:
        it.checked_by = current_user.id
        it.checked_at = datetime.now(timezone.utc)
    else:
        it.checked_by = None
        it.checked_at = None
    db.commit()
    db.refresh(it)
    return _to_dict(it)


@router.delete("/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    it = _get_item(db, item_id)
    db.delete(it)
    db.commit()
    return {"ok": True}
