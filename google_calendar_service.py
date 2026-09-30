import os
from datetime import datetime, timedelta, date, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from crypto_utils import encrypt_token, decrypt_token
from models_addition import GoogleAccount

CALENDAR_ID = "primary"
DEFAULT_DURATION_MINUTES = 60  # 시간이 지정된 일정은 기본 1시간짜리로 구글에 등록
KST = timezone(timedelta(hours=9))  # 한국은 DST가 없어서 고정 오프셋으로 충분


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
        # 구글 캘린더 종일 일정의 end.date는 "마지막 날의 다음날"(exclusive)이다.
        # schedule.end_date는 마지막 날(포함)이고, 비어 있으면 하루짜리.
        last = getattr(schedule, "end_date", None)
        if not last or last < schedule.event_date:
            last = schedule.event_date
        end_date = last + timedelta(days=1)
        base["start"] = {"date": schedule.event_date.isoformat()}
        base["end"] = {"date": end_date.isoformat()}

    return base


def from_google_event(g_event: dict) -> dict:
    """Google API event -> {title, description, event_date, event_time, end_date} 딕셔너리.
    ScheduleEvent 생성/수정 시 그대로 필드에 대입해서 쓸 수 있게 반환한다.
    """
    start = g_event.get("start", {})
    end_date = None
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"])
        event_date = dt.date()
        event_time = dt.strftime("%H:%M")
    else:
        event_date = date.fromisoformat(start["date"])
        event_time = None
        # 구글의 종일 일정 end.date는 "마지막 날의 다음날"이라 하루를 빼서 마지막 날(포함)로 저장.
        # 하루짜리(또는 길이 0)면 None. 시간이 있는 일정은 하루짜리로 취급한다.
        end = g_event.get("end", {})
        if "date" in end:
            last = date.fromisoformat(end["date"]) - timedelta(days=1)
            if last > event_date:
                end_date = last

    return {
        "title": g_event.get("summary", "(제목 없음)"),
        "description": g_event.get("description", ""),
        "event_date": event_date,
        "event_time": event_time,
        "end_date": end_date,
    }


def create_event(service, body: dict) -> dict:
    return service.events().insert(calendarId=CALENDAR_ID, body=body).execute()


def _local_start(start: dict):
    """Google start 값 -> (날짜 ISO 문자열, 'HH:MM' 또는 None). 시각은 KST 기준으로 맞춘다."""
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"])
        if dt.tzinfo is not None:
            dt = dt.astimezone(KST)
        return dt.date().isoformat(), dt.strftime("%H:%M")
    return start["date"], None


def _shifted_times(current: dict, body: dict):
    """시작 날짜/시각이 바뀐 경우의 새 (start, end). 기존 일정 길이(며칠짜리/몇 분짜리)는 유지한다."""
    cur_s, cur_e, new_s = current["start"], current["end"], body["start"]

    if "date" in cur_s and "date" in new_s:  # 종일 -> 종일
        days = (date.fromisoformat(cur_e["date"]) - date.fromisoformat(cur_s["date"])).days
        start = date.fromisoformat(new_s["date"])
        end = start + timedelta(days=max(days, 1))
        return {"date": start.isoformat()}, {"date": end.isoformat()}

    if "dateTime" in cur_s and "dateTime" in new_s:  # 시간 -> 시간
        duration = datetime.fromisoformat(cur_e["dateTime"]) - datetime.fromisoformat(cur_s["dateTime"])
        start = datetime.fromisoformat(new_s["dateTime"])  # body의 시각은 KST 기준 naive 값
        end = start + duration
        return (
            {"dateTime": start.isoformat(), "timeZone": "Asia/Seoul"},
            {"dateTime": end.isoformat(), "timeZone": "Asia/Seoul"},
        )

    return body["start"], body["end"]  # 종일 <-> 시간으로 형태가 바뀐 경우는 body 그대로


def update_event(service, google_event_id: str, body: dict) -> dict:
    """구글 일정을 통째로 덮어쓰지 않고 바뀐 필드만 patch한다.
    - 제목/설명만 patch -> 알림, 색상, 장소, 참석자, 반복 규칙이 그대로 유지된다.
    - 종일 일정: 시작일 또는 기간(며칠짜리)이 달라졌을 때만 start/end를 바꾼다.
    - 시간 일정: 시작 날짜/시각이 바뀐 경우에만 start/end를 바꾸고, 기존 일정 길이는 유지한다.
    """
    current = service.events().get(calendarId=CALENDAR_ID, eventId=google_event_id).execute()

    patch = {
        "summary": body["summary"],
        "description": body.get("description", ""),
    }

    cur_s, cur_e = current["start"], current["end"]
    new_s, new_e = body["start"], body["end"]
    if "date" in cur_s and "date" in new_s:  # 종일 -> 종일
        cur_days = max((date.fromisoformat(cur_e["date"]) - date.fromisoformat(cur_s["date"])).days, 1)
        new_days = (date.fromisoformat(new_e["date"]) - date.fromisoformat(new_s["date"])).days
        if cur_s["date"] != new_s["date"] or cur_days != new_days:
            patch["start"], patch["end"] = new_s, new_e
    elif _local_start(cur_s) != _local_start(new_s):  # 시간 일정 이동 또는 종일<->시간 변경
        patch["start"], patch["end"] = _shifted_times(current, body)

    return service.events().patch(calendarId=CALENDAR_ID, eventId=google_event_id, body=patch).execute()


def delete_event(service, google_event_id: str) -> None:
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=google_event_id).execute()
    except Exception as e:
        if "404" not in str(e):
            raise
