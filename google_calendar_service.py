import os
from datetime import datetime, timedelta, date, time, timezone

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
        last = getattr(schedule, "end_date", None)
        if last and last > schedule.event_date:
            # 며칠짜리 시간 일정: 종료 시각은 저장하지 않으므로 마지막 날 (시작 시각 + 1시간)으로 만든다.
            end_dt = datetime.combine(last, start_dt.time()) + timedelta(minutes=DEFAULT_DURATION_MINUTES)
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
    end = g_event.get("end", {})
    end_date = None
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"])
        event_date = dt.date()
        event_time = dt.strftime("%H:%M")
        # 시간 일정이 다른 날짜에 끝나면(며칠짜리/자정 넘김) 마지막 날을 end_date로 저장.
        # 정확히 자정(00:00)에 끝나는 일정은 그 전날까지로 본다.
        if "dateTime" in end:
            edt = datetime.fromisoformat(end["dateTime"])
            if edt.tzinfo is not None and dt.tzinfo is not None:
                edt = edt.astimezone(dt.tzinfo)
            last = edt.date()
            if edt.time() == time(0, 0) and last > event_date:
                last -= timedelta(days=1)
            if last > event_date:
                end_date = last
    else:
        event_date = date.fromisoformat(start["date"])
        event_time = None
        # 구글의 종일 일정 end.date는 "마지막 날의 다음날"이라 하루를 빼서 마지막 날(포함)로 저장.
        # 하루짜리(또는 길이 0)면 None.
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


def _to_kst(dt: datetime) -> datetime:
    """시간대가 있으면 KST로 변환하고, 없으면 KST 값으로 간주한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def _local_start(start: dict):
    """Google start 값 -> (날짜 ISO 문자열, 'HH:MM' 또는 None). 시각은 KST 기준으로 맞춘다."""
    if "dateTime" in start:
        dt = _to_kst(datetime.fromisoformat(start["dateTime"]))
        return dt.date().isoformat(), dt.strftime("%H:%M")
    return start["date"], None


def _last_day(g_start: dict, g_end: dict) -> date:
    """Google start/end -> 마지막 날(포함, KST 기준). 종일 일정의 end.date는 다음날(exclusive)이다."""
    if "date" in g_start:
        first = date.fromisoformat(g_start["date"])
        return max(date.fromisoformat(g_end["date"]) - timedelta(days=1), first)
    s = _to_kst(datetime.fromisoformat(g_start["dateTime"]))
    e = _to_kst(datetime.fromisoformat(g_end["dateTime"]))
    last = e.date()
    if e.time() == time(0, 0) and last > s.date():
        last -= timedelta(days=1)
    return max(last, s.date())


def _timed_range(current: dict, body: dict, want_last: date):
    """시간 일정의 새 (start, end). 종료 시각은 기존 값을 최대한 유지한다."""
    cur_s = _to_kst(datetime.fromisoformat(current["start"]["dateTime"]))
    cur_e = _to_kst(datetime.fromisoformat(current["end"]["dateTime"]))
    new_start = datetime.fromisoformat(body["start"]["dateTime"])  # body의 시각은 KST 기준 naive 값
    default_end = new_start + timedelta(minutes=DEFAULT_DURATION_MINUTES)

    if want_last > new_start.date():  # 며칠짜리: 마지막 날에 기존 종료 시각을 붙인다
        end = datetime.combine(want_last, cur_e.time())
        if end <= new_start:
            end = default_end
    elif _last_day(current["start"], current["end"]) == cur_s.date():  # 하루짜리 -> 하루짜리: 기존 길이 유지
        end = new_start + (cur_e - cur_s)
    else:  # 며칠짜리 -> 하루짜리: 기존 종료 시각을 시작 날짜에 붙인다
        end = datetime.combine(new_start.date(), cur_e.time())
        if end <= new_start:
            end = default_end
    return (
        {"dateTime": new_start.isoformat(), "timeZone": "Asia/Seoul"},
        {"dateTime": end.isoformat(), "timeZone": "Asia/Seoul"},
    )


def update_event(service, google_event_id: str, body: dict) -> dict:
    """구글 일정을 통째로 덮어쓰지 않고 바뀐 필드만 patch한다.
    - 제목/설명만 patch -> 알림, 색상, 장소, 참석자, 반복 규칙, 종료 시각이 그대로 유지된다.
    - 종일 일정: 시작일 또는 기간(며칠짜리)이 달라졌을 때만 start/end를 바꾼다.
    - 시간 일정: 시작 날짜/시각 또는 마지막 날이 달라졌을 때만 start/end를 바꾸고, 기존 종료 시각/길이는 유지한다.
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
    elif "dateTime" in cur_s and "dateTime" in new_s:  # 시간 -> 시간
        want_last = _last_day(new_s, new_e)
        if _local_start(cur_s) != _local_start(new_s) or want_last != _last_day(cur_s, cur_e):
            patch["start"], patch["end"] = _timed_range(current, body, want_last)
    else:  # 종일 <-> 시간 형태가 바뀐 경우는 body 그대로
        patch["start"], patch["end"] = new_s, new_e

    return service.events().patch(calendarId=CALENDAR_ID, eventId=google_event_id, body=patch).execute()


def delete_event(service, google_event_id: str) -> None:
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=google_event_id).execute()
    except Exception as e:
        if "404" not in str(e):
            raise
