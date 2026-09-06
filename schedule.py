"""
schedule.py

캘린더 스케줄 API.

기본 CRUD:
  GET    /schedule?year=&month=          - 내 일정만 (해당 월)
  GET    /schedule/couple?year=&month=   - 내 일정 + 파트너 일정 한번에 (소유자 구분)
  GET    /schedule/{id}
  POST   /schedule
  PUT    /schedule/{id}
  DELETE /schedule/{id}

공유 (사용자가 원할 때만 사용하는 선택 기능):
  GET    /schedule/{id}/share-text        - 카카오톡/문자/메일 공유용 텍스트
  GET    /schedule/{id}/google-calendar-link - 구글 캘린더 "일정 추가" 딥링크
  GET    /schedule/{id}/export.ics        - 이 일정 하나만 담긴 .ics 파일 (애플 캘린더 등에서 열면 바로 추가됨)

캘린더 구독(webcal, 애플 캘린더/구글 캘린더에 "URL로 구독" 기능으로 등록하면
자동으로 최신 일정이 동기화됨):
  POST   /schedule/feed-token   - 구독용 비밀 토큰 발급/재발급 (기존 토큰은 즉시 무효화됨)
  GET    /schedule/feed-token   - 현재 구독 URL 조회 (없으면 null)
  DELETE /schedule/feed-token   - 구독 중지 (토큰 폐기)
  GET    /schedule/feed/{token}.ics  - 실제 구독 피드 (인증 불필요, 토큰 자체가 비밀키 역할)
"""

import calendar
import secrets
import urllib.parse
from datetime import date as date_cls, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import ScheduleEvent, User
from auth import get_current_user

router = APIRouter(prefix="/schedule", tags=["스케줄"])


class ScheduleCreate(BaseModel):
    title: str
    description: Optional[str] = None
    event_date: date_cls
    event_time: Optional[str] = None  # "HH:MM"


class ScheduleOut(BaseModel):
    id: int
    title: str
    description: Optional[str]
    event_date: date_cls
    event_time: Optional[str]

    class Config:
        from_attributes = True


class CoupleScheduleOut(ScheduleOut):
    owner_username: str
    owner_full_name: str
    is_mine: bool


# ---------------------------------------------------------------------------
# 기본 CRUD (내 일정)
# ---------------------------------------------------------------------------
def _month_range(year: int, month: int):
    if not (1 <= month <= 12):
        raise HTTPException(400, "month는 1~12 사이여야 합니다")
    last_day = calendar.monthrange(year, month)[1]
    return date_cls(year, month, 1), date_cls(year, month, last_day)


@router.get("", response_model=list[ScheduleOut])
def list_month_events(
    year: int,
    month: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start, end = _month_range(year, month)
    return (
        db.query(ScheduleEvent)
        .filter(
            ScheduleEvent.user_id == current_user.id,
            ScheduleEvent.event_date >= start,
            ScheduleEvent.event_date <= end,
        )
        .order_by(ScheduleEvent.event_date, ScheduleEvent.event_time)
        .all()
    )


@router.get("/couple", response_model=list[CoupleScheduleOut])
def list_couple_month_events(
    year: int,
    month: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 일정 + 다른 활성 사용자(파트너) 일정을 한번에 반환.

    프론트엔드에서 owner_username/is_mine 값으로 색상을 다르게 표시하면
    "내 스케줄 / 여자친구 스케줄"이 한 캘린더에 겹쳐 보이게 만들 수 있음.
    """
    start, end = _month_range(year, month)
    rows = (
        db.query(ScheduleEvent, User)
        .join(User, ScheduleEvent.user_id == User.id)
        .filter(
            User.status == "active",
            ScheduleEvent.event_date >= start,
            ScheduleEvent.event_date <= end,
        )
        .order_by(ScheduleEvent.event_date, ScheduleEvent.event_time)
        .all()
    )
    return [
        CoupleScheduleOut(
            id=ev.id,
            title=ev.title,
            description=ev.description,
            event_date=ev.event_date,
            event_time=ev.event_time,
            owner_username=owner.username,
            owner_full_name=owner.full_name,
            is_mine=(owner.id == current_user.id),
        )
        for ev, owner in rows
    ]


# ---------------------------------------------------------------------------
# 캘린더 구독 (webcal) - 사용자가 원할 때만 켜는 선택 기능
# ---------------------------------------------------------------------------
class FeedTokenOut(BaseModel):
    feed_url: Optional[str]
    webcal_url: Optional[str]


def _feed_urls(token: Optional[str]) -> FeedTokenOut:
    if not token:
        return FeedTokenOut(feed_url=None, webcal_url=None)
    https_url = f"https://contigoway.com/api/schedule/feed/{token}.ics"
    webcal_url = f"webcal://contigoway.com/api/schedule/feed/{token}.ics"
    return FeedTokenOut(feed_url=https_url, webcal_url=webcal_url)


@router.get("/feed-token", response_model=FeedTokenOut)
def get_feed_token(current_user: User = Depends(get_current_user)):
    return _feed_urls(current_user.calendar_feed_token)


@router.post("/feed-token", response_model=FeedTokenOut)
def create_or_rotate_feed_token(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """구독 링크를 새로 발급(또는 재발급). 재발급하면 기존 링크는 즉시 무효화됨."""
    current_user.calendar_feed_token = secrets.token_hex(24)
    db.commit()
    return _feed_urls(current_user.calendar_feed_token)


@router.delete("/feed-token")
def revoke_feed_token(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.calendar_feed_token = None
    db.commit()
    return {"message": "캘린더 구독이 중지되었습니다"}


@router.get("/feed/{token}.ics")
def calendar_feed(token: str, db: Session = Depends(get_db)):
    """애플/구글 캘린더가 주기적으로 이 URL을 폴링해서 최신 일정을 가져감.
    토큰 자체가 비밀키이므로 별도 로그인 인증은 요구하지 않음 (URL을 아는 사람만 접근 가능)."""
    user = db.query(User).filter(User.calendar_feed_token == token).first()
    if not user:
        raise HTTPException(404, "유효하지 않은 구독 링크입니다")
    events = (
        db.query(ScheduleEvent)
        .filter(ScheduleEvent.user_id == user.id)
        .order_by(ScheduleEvent.event_date)
        .all()
    )
    ics = _build_ics(events, f"contigoway - {user.full_name}")
    return Response(content=ics, media_type="text/calendar")


def _get_owned_event(event_id: int, db: Session, current_user: User) -> ScheduleEvent:
    event = (
        db.query(ScheduleEvent)
        .filter(ScheduleEvent.id == event_id, ScheduleEvent.user_id == current_user.id)
        .first()
    )
    if not event:
        raise HTTPException(404, "일정을 찾을 수 없습니다")
    return event


@router.get("/{event_id}", response_model=ScheduleOut)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_owned_event(event_id, db, current_user)


@router.post("", response_model=ScheduleOut, status_code=201)
def create_event(
    data: ScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not data.title.strip():
        raise HTTPException(400, "제목을 입력해주세요")
    event = ScheduleEvent(
        user_id=current_user.id,
        title=data.title.strip(),
        description=data.description,
        event_date=data.event_date,
        event_time=data.event_time,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.put("/{event_id}", response_model=ScheduleOut)
def update_event(
    event_id: int,
    data: ScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    event = _get_owned_event(event_id, db, current_user)
    if not data.title.strip():
        raise HTTPException(400, "제목을 입력해주세요")
    event.title = data.title.strip()
    event.description = data.description
    event.event_date = data.event_date
    event.event_time = data.event_time
    db.commit()
    db.refresh(event)
    return event


@router.delete("/{event_id}")
def delete_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    event = _get_owned_event(event_id, db, current_user)
    db.delete(event)
    db.commit()
    return {"message": "일정이 삭제되었습니다"}


# ---------------------------------------------------------------------------
# 공유하기 (카카오톡/문자/메일 텍스트, 구글 캘린더 링크, .ics 다운로드)
# ---------------------------------------------------------------------------
@router.get("/{event_id}/share-text")
def share_text(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """카카오톡 공유하기 / 메일 본문 / 문자 본문에 그대로 붙여넣을 수 있는 텍스트를 생성.

    프론트엔드에서는 이 텍스트를:
      - 카카오톡: Kakao SDK의 텍스트 공유(Kakao.Share.sendDefault, feed 템플릿의 description)에 사용
      - 이메일: mailto:?body=<urlencoded text>
      - 문자: sms:?body=<urlencoded text> (모바일 브라우저 기준)
    로 연결하면 됩니다.
    """
    event = _get_owned_event(event_id, db, current_user)
    time_part = f" {event.event_time}" if event.event_time else ""
    lines = [f"📅 {event.event_date.isoformat()}{time_part}", f"{event.title}"]
    if event.description:
        lines.append(event.description)
    lines.append("- contigoway 일정 공유")
    return {"text": "\n".join(lines)}


def _event_datetimes(event: ScheduleEvent):
    """(start, end, all_day) 튜플 반환. 시간이 없으면 종일 일정으로 취급, 있으면 1시간짜리로 취급."""
    if event.event_time:
        try:
            hh, mm = event.event_time.split(":")
            start = datetime.combine(event.event_date, datetime.min.time()).replace(
                hour=int(hh), minute=int(mm)
            )
            return start, start + timedelta(hours=1), False
        except ValueError:
            pass
    start = datetime.combine(event.event_date, datetime.min.time())
    return start, start + timedelta(days=1), True


@router.get("/{event_id}/google-calendar-link")
def google_calendar_link(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """구글 캘린더 '일정 추가' 화면으로 바로 이동하는 링크. 프론트엔드에서 새 탭으로 열면 됨."""
    event = _get_owned_event(event_id, db, current_user)
    start, end, all_day = _event_datetimes(event)

    if all_day:
        dates = f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
    else:
        dates = f"{start.strftime('%Y%m%dT%H%M%S')}/{end.strftime('%Y%m%dT%H%M%S')}"

    params = {
        "action": "TEMPLATE",
        "text": event.title,
        "dates": dates,
        "details": event.description or "",
    }
    url = "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)
    return {"url": url}


def _build_ics(events: list[ScheduleEvent], calendar_name: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//contigoway//schedule//KO",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{calendar_name}",
    ]
    for ev in events:
        start, end, all_day = _event_datetimes(ev)
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:contigoway-event-{ev.id}@contigoway.com")
        lines.append(f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")
        if all_day:
            lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}")
        summary = ev.title.replace("\n", " ")
        lines.append(f"SUMMARY:{summary}")
        if ev.description:
            desc = ev.description.replace("\n", "\\n")
            lines.append(f"DESCRIPTION:{desc}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


@router.get("/{event_id}/export.ics")
def export_single_ics(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """이 일정 하나만 담긴 .ics 파일. 다운로드해서 열면 애플 캘린더/구글 캘린더/아웃룩 등에
    바로 추가할 수 있음 (일회성 추가, 자동 동기화는 아래 구독 기능을 사용)."""
    event = _get_owned_event(event_id, db, current_user)
    ics = _build_ics([event], "contigoway")
    return Response(
        content=ics,
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="event_{event_id}.ics"'},
    )


