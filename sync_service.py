from sqlalchemy.orm import Session

from database import SessionLocal
from models import ScheduleEvent
from models_addition import GoogleAccount
from google_calendar_service import (
    get_calendar_service,
    to_google_event_body,
    from_google_event,
    create_event,
    update_event,
    delete_event,
)


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


def pull_from_google(db: Session, user_id: int):
    """5분 주기 폴링 잡에서 사용자마다 호출. syncToken이 있으면 증분만, 없으면 최초 1회 전체 동기화."""
    account = db.query(GoogleAccount).filter_by(user_id=user_id).first()
    if not account:
        return

    service = get_calendar_service(db, account)
    events_api = service.events()

    page_token = None
    all_events = []

    while True:
        try:
            params = {"calendarId": "primary", "pageToken": page_token, "showDeleted": True}
            if account.sync_token:
                params["syncToken"] = account.sync_token
            else:
                params["timeMin"] = "2025-01-01T00:00:00Z"
            resp = events_api.list(**params).execute()
        except Exception as e:
            if "410" in str(e):
                account.sync_token = None
                db.commit()
                return pull_from_google(db, user_id)
            raise

        all_events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            account.sync_token = resp.get("nextSyncToken", account.sync_token)
            break

    for g_event in all_events:
        _apply_google_event(db, user_id, g_event)

    db.commit()


def _apply_google_event(db: Session, user_id: int, g_event: dict):
    google_event_id = g_event["id"]
    contigoway_id = g_event.get("extendedProperties", {}).get("private", {}).get("contigoway_id")

    # contigoway가 만들어서 되돌아온 이벤트는 우리가 이미 알고 있으므로 스킵 (무한루프 방지)
    if contigoway_id:
        return

    existing = db.query(ScheduleEvent).filter_by(google_event_id=google_event_id).first()

    if g_event.get("status") == "cancelled":
        if existing:
            db.delete(existing)
        return

    fields = from_google_event(g_event)

    if existing:
        existing.title = fields["title"]
        existing.description = fields["description"]
        existing.event_date = fields["event_date"]
        existing.event_time = fields["event_time"]
        existing.last_modified_source = "google"
    else:
        new_event = ScheduleEvent(
            user_id=user_id,
            google_event_id=google_event_id,
            last_modified_source="google",
            **fields,
        )
        db.add(new_event)
