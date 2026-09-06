"""
visits.py

방문자 수 카운터.
  POST /visits/ping   - 페이지 로드 1회 기록 (인증 불필요 - 로그인 전 화면에서도 호출)
  GET  /visits/stats  - {total, today} 반환 (인증 불필요)
"""

from datetime import date as date_cls

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import PageVisit

router = APIRouter(prefix="/visits", tags=["방문자"])


@router.post("/ping")
def ping(db: Session = Depends(get_db)):
    db.add(PageVisit())
    db.commit()
    return {"ok": True}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    total = db.query(PageVisit).count()
    today = (
        db.query(PageVisit)
        .filter(func.date(PageVisit.visited_at) == date_cls.today())
        .count()
    )
    return {"total": total, "today": today}
