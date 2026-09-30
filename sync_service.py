from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from models import ScheduleEvent
from models_addition import GoogleAccount
from google_calendar_service import (
    CALENDAR_ID,
    get_calendar_service,
    to_google_event_body,
    from_google_event,
    create_event,
    update_event,
    delete_event,
)

# 5분마다 아래 "조회 기간" 전체를 구글에서 다시 받아 DB와 맞춘다 (syncToken 미사용).
# 반복 일정은 singleEvents=True로 날짜별 일정으로 펼쳐서 받고,
# 조회 기간 안에서 구글에 없는 일정은 DB에서도 정리한다.
SYNC_FROM = date(2025, 1, 1)
SYNC_FUTURE_DAYS = 400
# 정리(삭제) 안전장치: 한 번에 너무 많이 지우게 되면 이상 상황으로 보고 건너뛴다.
MAX_DELETE_MIN = 20
MAX_DELETE_RATIO = 0.5


def push_to_google(db: Session, user_id: int, schedule: ScheduleEvent, action: str):
    """action: 'create' | 'update' | 'delete'
    schedule.py의 create_event / update_event / delete_event 라우터 끝에서 호출.
    구글 미연동 사용자는 조용히 스킵.
    """
    account = db.query(GoogleAccount).filter_by(user_id=user_id).first()
    if not account:
        return

    service = get_calendar_service(db, account)

    if action == "delete":
        if schedule.google_event_id:
            delete_event(service, schedule.google_event_id)
        return

    body = to_google_event_body(schedule)

    if action == "create" or not schedule.google_event_id:
        result = create_event(service, body)
        schedule.google_event_id = result["id"]
    else:
        update_event(service, schedule.google_event_id, body)

    schedule.last_modified_source = "contigoway"
    db.commit()


def _list_window(service, time_min: str, time_max: str) -> list:
    """조회 기간의 일정을 반복 일정 펼침(singleEvents) 상태로 전부 가져온다."""
    items = []
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            showDeleted=False,
            maxResults=2500,
            timeZone="Asia/Seoul",
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def pull_from_google(db: Session, user_id: int):
    """5분 주기 폴링 잡에서 사용자마다 호출.
    조회 기간(2025-01-01 ~ 오늘+400일)의 구글 일정을 받아 DB에 반영하고,
    구글에서 삭제된 일정은 DB에서도 지운다.
    """
    account = db.query(GoogleAccount).filter_by(user_id=user_id).first()
    if not account:
        return

    service = get_calendar_service(db, account)

    window_end = datetime.now(timezone.utc) + timedelta(days=SYNC_FUTURE_DAYS)
    items = _list_window(
        service,
        time_min=f"{SYNC_FROM.isoformat()}T00:00:00Z",
        time_max=window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    seen = set()
    for g_event in items:
        if g_event.get("status") == "cancelled" or "start" not in g_event:
            continue
        seen.add(g_event["id"])
        _apply_google_event(db, user_id, g_event)
    db.flush()

    _remove_missing(db, user_id, seen, window_end.date())
    db.commit()


def _remove_missing(db: Session, user_id: int, seen: set, window_end: date):
    """구글에서 사라진 일정을 DB에서 정리한다.
    - 조회 기간 안(경계 하루씩 제외)이고 google_event_id가 있는 행만 대상.
    - 구글 응답이 비었거나 삭제 대상이 너무 많으면 이상 상황으로 보고 건너뛴다.
    """
    lo = SYNC_FROM + timedelta(days=1)
    hi = window_end - timedelta(days=1)
    rows = (
        db.query(ScheduleEvent)
        .filter(
            ScheduleEvent.user_id == user_id,
            ScheduleEvent.google_event_id.isnot(None),
            ScheduleEvent.event_date >= lo,
            ScheduleEvent.event_date <= hi,
        )
        .all()
    )
    stale = [r for r in rows if r.google_event_id not in seen]
    if not stale:
        return

    if not seen or len(stale) > max(MAX_DELETE_MIN, int(len(rows) * MAX_DELETE_RATIO)):
        print(
            f"[google-sync] user_id={user_id} 정리 건너뜀: 삭제 대상 {len(stale)}건 / 전체 {len(rows)}건 "
            f"(구글 응답 {len(seen)}건) - 이상 상황으로 판단",
            flush=True,
        )
        return

    for r in stale:
        db.delete(r)
    print(f"[google-sync] user_id={user_id} 구글에서 삭제/변경된 일정 {len(stale)}건 정리", flush=True)


def _norm(value):
    """None과 빈 문자열을 같은 값으로 비교하기 위한 정규화."""
    return "" if value is None else value


def _apply_google_event(db: Session, user_id: int, g_event: dict):
    google_event_id = g_event["id"]
    contigoway_id = g_event.get("extendedProperties", {}).get("private", {}).get("contigoway_id")

    existing = (
        db.query(ScheduleEvent)
        .filter_by(google_event_id=google_event_id, user_id=user_id)
        .first()
    )

    # 사이트에서 만들어 구글로 올라간 일정이 되돌아온 경우:
    # 아직 google_event_id가 연결 안 된 행만 id로 찾아 연결한다 (중복 행 방지).
    # 이미 다른 구글 일정과 연결된 행은 건드리지 않는다 (반복 일정 펼침으로 같은 값이 여러 번 올 수 있음).
    if existing is None and contigoway_id:
        try:
            candidate = db.query(ScheduleEvent).filter_by(id=int(contigoway_id), user_id=user_id).first()
        except ValueError:
            candidate = None
        if candidate is not None and candidate.google_event_id is None:
            candidate.google_event_id = google_event_id
            existing = candidate

    fields = from_google_event(g_event)

    if existing:
        # 값이 실제로 달라진 경우에만 갱신 (사이트가 방금 올린 일정의 에코는 그대로 둔다)
        if any(_norm(getattr(existing, k)) != _norm(v) for k, v in fields.items()):
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.last_modified_source = "google"
    else:
        new_event = ScheduleEvent(
            user_id=user_id,
            google_event_id=google_event_id,
            last_modified_source="google",
            **fields,
        )
        db.add(new_event)
