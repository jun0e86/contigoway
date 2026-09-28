"""
trip_brief.py

삿포로 여행 기간 동안 그날 필요한 정보를 카드 형태(카카오 피드 템플릿)로
카카오톡 '나에게 보내기'로 보내준다. 카카오 연결 + talk_message 동의한 회원 모두에게 발송.

자동 발송 (scheduler.py에서 register_trip_jobs 호출)
  - 12/15 20:00  출발 전날 카드: 내일 일정 + 아직 안 챙긴 준비물
  - 12/16~12/19 07:00  당일 카드: 날씨 · 일정 · 집합/이동 · 숙소 · 주의사항

자동 점검 (같은 곳에서 등록)
  - 매일 04:10  카카오 토큰 점검·갱신. 카카오 리프레시 토큰은 약 2달이면 만료되고, 갱신할 때
    남은 기간이 1달 미만일 때만 새로 내려온다. 첫 발송(12/15)까지 석 달 가까이 걸려서
    이 점검이 없으면 그 전에 만료돼 카드가 조용히 안 갈 수 있다.

수동 (로그인 필요)
  GET  /trip-brief/preview?day=2        카드 내용 미리보기(JSON)
  POST /trip-brief/test?day=2           내 카카오톡으로만 테스트 발송 (day=0은 전날 카드)
  POST /trip-brief/send-all?day=2       관리자: 연결된 모든 회원에게 발송
  GET  /trip-brief/kakao-status         관리자: 회원별 카카오 토큰 만료일, 12/15 전 만료 위험 여부
  GET  /trip-brief/jobs                 관리자: 등록된 자동 발송·점검 예약 목록
  POST /trip-brief/kakao-keepalive      관리자: 카카오 토큰 점검을 지금 실행

선택 환경변수
  KAKAO_CARD_IMAGE_BASE  카드 상단 이미지 폴더 URL (기본: https://contigoway.com/img)
"""

import json
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from auth import get_current_user
from database import SessionLocal, get_db
from packing import DEFAULT_TRIP, PackingItem
from social_auth import SITE_URL, SocialAccount, _http, get_kakao_access_token

router = APIRouter(prefix="/trip-brief", tags=["여행 카카오 브리핑"])

TRIP_DATES = ["2026-12-16", "2026-12-17", "2026-12-18", "2026-12-19"]
HOTEL = "그란벨 호텔 스스키노"
# 카드 상단 이미지: /var/www/contigoway/img/trip-card-day{0~4}.jpg (800x400)
CARD_IMAGE_BASE = os.environ.get("KAKAO_CARD_IMAGE_BASE", SITE_URL.rstrip("/") + "/img").rstrip("/")
DOW = "월화수목금토일"

# 날짜별 카드 내용 (여행 페이지 '일자별 동선'과 맞춰 관리)
DAYS = {
    1: {
        "title": "도착 · 삿포로 시내 · 스스키노",
        "move": "공항→JR 쾌속에어포트→삿포로역",
        "plan": "호텔 체크인 → 오도리공원 → 노리아 → 스스키노 야경",
        "food": "테시카가라멘 요코쵸점",
        "tip": "여권 · 엔화 현금 · Visit Japan Web QR 준비",
    },
    2: {
        "title": "비에이 · 후라노 근교 투어",
        "move": "삿포로역 북쪽 출구 집합 (바우처 확인)",
        "plan": "흰수염폭포 → 청의 호수 → 나홀로 나무 → 닝구르 테라스",
        "food": "비에이 마을 우동/카레",
        "tip": "투어 바우처 · 방한화 · 핫팩 · 보조배터리",
    },
    3: {
        "title": "오타루 당일치기 · 삿포로 미식 저녁",
        "move": "JR 하코다테 본선 삿포로역→오타루역 (약 35분)",
        "plan": "오타루 운하 → 유리공방 거리 → 스시거리 → 모리히코 카페",
        "food": "오타루 스시 · 라멘 하가쿠레",
        "tip": "운하 주변 빙판길 주의 · 현금 준비",
    },
    4: {
        "title": "미식 마무리 · 쇼핑 · 출국",
        "move": "호텔 체크아웃 → JR 치토세선 → 신치토세공항",
        "plan": "맥주박물관 → 수프카레 → 다누키코지 쇼핑 → 공항",
        "food": "Curry Shop S · 라멘 리퍼블릭(공항)",
        "tip": "체크아웃 시간 · 귀국편 시간 · 면세 한도 확인",
    },
}

WX_TEXT = {0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림"}


def _wx_text(code: int) -> str:
    if code in WX_TEXT:
        return WX_TEXT[code]
    if code <= 48: return "안개"
    if code <= 57: return "이슬비"
    if code <= 67: return "비"
    if code <= 77: return "눈"
    if code <= 82: return "소나기"
    if code <= 86: return "눈보라"
    return "뇌우"


def _wx_icon(code: int) -> str:
    if code == 0: return "☀️"
    if code <= 2: return "🌤️"
    if code == 3: return "☁️"
    if code <= 48: return "🌫️"
    if code <= 67: return "🌧️"
    if code <= 86: return "❄️"
    return "⛈️"


def _weather(ds: str) -> str:
    """해당 날짜 삿포로 예보 한 줄. 예보 범위(16일) 밖이면 안내 문구."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 43.0621, "longitude": 141.3544, "timezone": "Asia/Tokyo",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,snowfall_sum",
                "start_date": ds, "end_date": ds,
            },
            timeout=10,
        )
        d = r.json().get("daily") or {}
        if not d.get("time"):
            return "예보 공개 전 (16일 이내부터)"
        code = int(d["weather_code"][0])
        mx, mn = round(d["temperature_2m_max"][0]), round(d["temperature_2m_min"][0])
        snow = d["snowfall_sum"][0] or 0
        return f"{_wx_icon(code)} {_wx_text(code)} {mx}° / {mn}°" + (f" · 눈 {snow:.0f}cm" if snow >= 1 else "")
    except Exception as e:
        print(f"[trip-brief] 날씨 조회 실패: {e}")
        return "날씨 정보를 불러오지 못했어요"


def _date_label(ds: str) -> str:
    d = date.fromisoformat(ds)
    return f"{d.month}/{d.day}({DOW[d.weekday()]})"


def build_card(db: Session, day: int) -> dict:
    """day 1~4: 당일 카드 / day 0: 출발 전날 카드"""
    if day == 0:
        info = DAYS[1]
        left = (db.query(PackingItem)
                .filter(PackingItem.trip == DEFAULT_TRIP, PackingItem.checked_by.is_(None))
                .order_by(PackingItem.sort_order).all())
        names = [i.name.split(" (")[0] for i in left[:3]]
        left_txt = f"{len(left)}개 · " + ", ".join(names) + (" 등" if len(left) > 3 else "")
        return {
            "day": 0,
            "header": "✈️ 내일 삿포로 출발!",
            "category": f"{_date_label(TRIP_DATES[0])} 1일차",
            "title": info["title"],
            "description": "빠진 준비물은 오늘 밤에 챙겨요 🧳",
            "items": [
                ("날씨", _weather(TRIP_DATES[0])),
                ("이동", info["move"]),
                ("숙소", HOTEL),
                ("남은짐", left_txt if left else "모두 챙겼어요 🎉"),
                ("체크", "여권 · e-티켓 · 엔화 · 충전기"),
            ],
            "link_path": "/index.html#secPacking",
            "button": "준비물 체크하기",
        }
    if day not in DAYS:
        raise HTTPException(400, "day는 0~4 사이여야 해요")
    info, ds = DAYS[day], TRIP_DATES[day - 1]
    return {
        "day": day,
        "header": f"🗾 삿포로 {day}일차",
        "category": _date_label(ds),
        "title": info["title"],
        "description": "오늘도 즐거운 여행 되세요 ☃️" if day < 4 else "마지막 날, 귀국편 시간 꼭 확인해요 ✈️",
        "items": [
            ("날씨", _weather(ds)),
            ("이동", info["move"]),
            ("일정", info["plan"]),
            ("맛집", info["food"]),
            ("주의", info["tip"]),
        ],
        "link_path": "/index.html#secDays",
        "button": "오늘 일정 자세히 보기",
    }


def _template(card: dict) -> dict:
    url = SITE_URL.rstrip("/") + card["link_path"]
    link = {"web_url": url, "mobile_web_url": url}
    content = {"title": card["title"], "description": card["description"], "link": link}
    content.update({"image_url": f"{CARD_IMAGE_BASE}/trip-card-day{card['day']}.jpg",
                    "image_width": 800, "image_height": 400})
    return {
        "object_type": "feed",
        "content": content,
        "item_content": {
            "title_image_text": card["header"][:24],
            "title_image_category": card["category"][:14],
            "items": [{"item": k, "item_op": v[:50]} for k, v in card["items"]],
        },
        "buttons": [{"title": card["button"], "link": link}],
    }


def _send(db: Session, acct: SocialAccount, card: dict) -> dict:
    token = get_kakao_access_token(db, acct)
    return _http(
        "POST",
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        {"template_object": json.dumps(_template(card), ensure_ascii=False)},
        headers={"Authorization": f"Bearer {token}"},
    )


def _recipients(db: Session):
    return [a for a in db.query(SocialAccount).filter_by(provider="kakao").all()
            if a.scopes and "talk_message" in a.scopes]


def send_to_all(day: int):
    """스케줄러에서 호출. 카카오 연결 + 메시지 동의한 모든 회원에게 발송."""
    db = SessionLocal()
    try:
        card = build_card(db, day)
        for acct in _recipients(db):
            try:
                _send(db, acct, card)
                print(f"[trip-brief] day={day} user_id={acct.user_id} 발송 완료")
            except Exception as e:
                print(f"[trip-brief] day={day} user_id={acct.user_id} 발송 실패: {e}")
    finally:
        db.close()


KST = ZoneInfo("Asia/Seoul")
FIRST_CARD_AT = datetime(2026, 12, 15, 20, 0)   # 첫 자동 발송 시각 (한국시간)


def kakao_keepalive():
    """카카오 토큰 점검. 액세스 토큰(약 6시간)이 지났으면 리프레시 토큰으로 갱신하고,
    리프레시 토큰의 남은 기간이 1달 미만이면 카카오가 새 리프레시 토큰을 내려주므로 자동으로 저장된다.
    실패(리프레시 토큰 만료)하면 로그에 남긴다. 그 경우 해당 회원이 카카오로 다시 로그인해야 한다."""
    db = SessionLocal()
    try:
        for acct in _recipients(db):
            try:
                get_kakao_access_token(db, acct)
                print(f"[trip-brief] 카카오 토큰 점검 user_id={acct.user_id}: 정상")
            except Exception as e:
                print(f"[trip-brief] 카카오 토큰 점검 실패 user_id={acct.user_id}: {e} → 카카오로 다시 로그인 필요")
    finally:
        db.close()


def register_trip_jobs(scheduler):
    """scheduler.py의 start_scheduler()에서 호출. 지난 시각은 건너뜀. 등록 결과는 로그에 남긴다."""
    plan = [(0, "2026-12-15 20:00")] + [(i + 1, f"{d} 07:00") for i, d in enumerate(TRIP_DATES)]
    now = datetime.now(KST).replace(tzinfo=None)  # 스케줄러와 같은 한국시간 기준
    for day, when in plan:
        run_at = datetime.strptime(when, "%Y-%m-%d %H:%M")
        if run_at <= now:
            print(f"[trip-brief] 예약 건너뜀(이미 지남) day={day} {when}")
            continue
        scheduler.add_job(send_to_all, "date", run_date=run_at, args=[day],
                          id=f"trip_brief_day{day}", replace_existing=True, misfire_grace_time=3600)
        print(f"[trip-brief] 예약 등록 day={day} {when} (KST)")
    scheduler.add_job(kakao_keepalive, "cron", hour=4, minute=10, id="kakao_keepalive",
                      replace_existing=True, misfire_grace_time=3600)
    print("[trip-brief] 예약 등록 카카오 토큰 점검 매일 04:10 (KST)")


# ---------------------------------------------------------------------------
# API (미리보기 / 테스트 발송)
# ---------------------------------------------------------------------------
@router.get("/preview")
def preview(day: int = Query(default=1, ge=0, le=4), db: Session = Depends(get_db),
            current_user=Depends(get_current_user)):
    card = build_card(db, day)
    return {"card": card, "template": _template(card)}


@router.post("/test")
def test_send(day: int = Query(default=1, ge=0, le=4), db: Session = Depends(get_db),
              current_user=Depends(get_current_user)):
    acct = db.query(SocialAccount).filter_by(provider="kakao", user_id=current_user.id).first()
    if not acct or not acct.scopes or "talk_message" not in acct.scopes:
        raise HTTPException(409, "카카오 연결(메시지 전송 동의)이 필요해요. 카카오로 다시 로그인해주세요")
    _send(db, acct, build_card(db, day))
    return {"ok": True, "day": day}


@router.post("/send-all")
def send_all_now(day: int = Query(default=1, ge=0, le=4), current_user=Depends(get_current_user)):
    if getattr(current_user, "role", "") != "admin":
        raise HTTPException(403, "관리자만 전체 발송할 수 있어요")
    send_to_all(day)
    return {"ok": True, "day": day}


def _require_admin(user):
    if getattr(user, "role", "") != "admin":
        raise HTTPException(403, "관리자만 볼 수 있어요")


def _to_kst_naive(dt):
    """DB에 저장된 시각을 한국시간으로 바꾼다. 시간대 정보가 없으면 UTC로 본다(서버 기본값)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).replace(tzinfo=None)


@router.get("/kakao-status")
def kakao_status(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """회원별 카카오 리프레시 토큰 만료일. before_first_card=True 이면 12/15 첫 발송 전에 만료된다는 뜻."""
    _require_admin(current_user)
    now = datetime.now(KST).replace(tzinfo=None)
    rows = []
    for a in db.query(SocialAccount).filter_by(provider="kakao").all():
        exp = _to_kst_naive(getattr(a, "refresh_expires_at", None))
        rows.append({
            "user_id": a.user_id,
            "talk_message": bool(a.scopes and "talk_message" in a.scopes),
            "refresh_expires_at": exp.strftime("%Y-%m-%d %H:%M") if exp else None,
            "days_left": (exp - now).days if exp else None,
            "before_first_card": (exp < FIRST_CARD_AT) if exp else None,
        })
    return {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "first_card_at": FIRST_CARD_AT.strftime("%Y-%m-%d %H:%M"),
        "days_until_first_card": (FIRST_CARD_AT - now).days,
        "accounts": rows,
    }


@router.get("/jobs")
def list_jobs(current_user=Depends(get_current_user)):
    """이 서버 프로세스에 실제로 등록된 예약 목록 (자동 발송 예약이 살아 있는지 확인용)."""
    _require_admin(current_user)
    from scheduler import _scheduler
    return {
        "running": _scheduler.running,
        "jobs": [{"id": j.id, "next_run": str(getattr(j, "next_run_time", None))} for j in _scheduler.get_jobs()],
    }


@router.post("/kakao-keepalive")
def keepalive_now(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _require_admin(current_user)
    kakao_keepalive()
    return kakao_status(db=db, current_user=current_user)
