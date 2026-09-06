"""
visits.py

방문자 수 카운터.
  POST /site/hit       - 페이지 로드 1회 기록 (인증 불필요 - 로그인 전 화면에서도 호출)
  GET  /site/counter   - {total, today} 반환 (인증 불필요)

(참고: 예전에는 /visits/ping, /visits/stats 였으나 일부 광고차단 확장프로그램이
"visits", "ping", "stats" 같은 단어가 들어간 요청을 추적(트래킹)으로 오인해서
차단하는 경우가 있어 이름을 바꿈)
"""

from datetime import date as date_cls

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import PageVisit

router = APIRouter(prefix="/site", tags=["방문자"])


@router.post("/hit")
def record_hit(db: Session = Depends(get_db)):
    db.add(PageVisit())
    db.commit()
    return {"ok": True}


@router.get("/counter")
def get_counter(db: Session = Depends(get_db)):
    total = db.query(PageVisit).count()
    today = (
        db.query(PageVisit)
        .filter(func.date(PageVisit.visited_at) == date_cls.today())
        .count()
    )
    return {"total": total, "today": today}
