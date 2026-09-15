import os
from datetime import datetime, timedelta, date

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from crypto_utils import encrypt_token, decrypt_token
from models_addition import GoogleAccount

CALENDAR_ID = "primary"
DEFAULT_DURATION_MINUTES = 60  # 시간이 지정된 일정은 기본 1시간짜리로 구글에 등록


def get_calendar_service(db: Session, account: GoogleAccount):
    creds = Credentials(
        token=decrypt_token(account.encrypted_access_token) if account.encrypted_access_token else None,
        refresh_token=decrypt_token(account.encrypted_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/calendar"],
    )

    if creds.expired or not creds.token:
        creds.refresh(GoogleAuthRequest())
        account.encrypted_access_token = encrypt_token(creds.token)
        account.access_token_expiry = creds.expiry
        db.commit()

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def to_google_event_body(schedule) -> dict:
    """ScheduleEvent(title, description, event_date, event_time) -> Google API event body.
    event_time이 있으면 "HH:MM" 시각부터 1시간짜리 일정으로, 없으면 하루짜리 종일 일정으로 만든다.
    """
    base = {
        "summary": schedule.title,
        "description": schedule.description or "",
        "extendedProperties": {"private": {"contigoway_id": str(schedule.id)}},
    }

    if schedule.event_time:
        hour, minute = map(int, schedule.event_time.split(":"))
        start_dt = datetime.combine(schedule.event_date, datetime.min.time()).replace(hour=hour, minute=minute)
        end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MINUTES)
        base["start"] = {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Seoul"}
        base["end"] = {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Seoul"}
    else:
        # 구글 캘린더 종일 일정은 end.date가 "다음날"이어야 하루짜리로 표시된다 (exclusive)
        end_date = schedule.event_date + timedelta(days=1)
        base["start"] = {"date": schedule.event_date.isoformat()}
        base["end"] = {"date": end_date.isoformat()}

    return base


def from_google_event(g_event: dict) -> dict:
    """Google API event -> {title, description, event_date, event_time} 딕셔너리.
    ScheduleEvent 생성/수정 시 그대로 필드에 대입해서 쓸 수 있게 반환한다.
    """
    start = g_event.get("start", {})
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"])
        event_date = dt.date()
        event_time = dt.strftime("%H:%M")
    else:
        event_date = date.fromisoformat(start["date"])
        event_time = None

    return {
        "title": g_event.get("summary", "(제목 없음)"),
        "description": g_event.get("description", ""),
        "event_date": event_date,
        "event_time": event_time,
    }


def create_event(service, body: dict) -> dict:
    return service.events().insert(calendarId=CALENDAR_ID, body=body).execute()


def update_event(service, google_event_id: str, body: dict) -> dict:
    return service.events().update(calendarId=CALENDAR_ID, eventId=google_event_id, body=body).execute()


def delete_event(service, google_event_id: str) -> None:
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=google_event_id).execute()
    except Exception as e:
        if "404" not in str(e):
            raise
