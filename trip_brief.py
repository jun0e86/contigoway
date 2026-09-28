"""
trip_brief.py

삿포로 여행 기간 동안 그날 필요한 정보를 카드 형태(카카오 피드 템플릿)로
카카오톡 '나에게 보내기'로 보내준다. 카카오 연결 + talk_message 동의한 회원 모두에게 발송.

자동 발송 (scheduler.py에서 register_trip_jobs 호출)
  - 12/15 20:00  출발 전날 카드: 내일 일정 + 아직 안 챙긴 준비물
  - 12/16~12/19 07:00  당일 카드: 날씨 · 일정 · 집합/이동 · 숙소 · 주의사항

수동 (로그인 필요)
  GET  /trip-brief/preview?day=2        카드 내용 미리보기(JSON)
  POST /trip-brief/test?day=2           내 카카오톡으로만 테스트 발송 (day=0은 전날 카드)
  POST /trip-brief/send-all?day=2       관리자: 연결된 모든 회원에게 발송

선택 환경변수
  KAKAO_CARD_IMAGE_URL  카드 상단 이미지(https 공개 URL, 예: https://contigoway.com/img/sapporo-card.jpg)
"""

import json
import os
from datetime import date, datetime
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
CARD_IMAGE = os.environ.get("KAKAO_CARD_IMAGE_URL", "").strip()
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
        left_txt = ", ".join(i.name.split(" (")[0] for i in left[:6]) + (f" 외 {len(left) - 6}개" if len(left) > 6 else "")
        return {
            "title": "✈️ 내일 삿포로 출발!",
            "description": f"{_date_label(TRIP_DATES[0])} 1일차 · {info['title']}",
            "items": [
                ("날씨", _weather(TRIP_DATES[0])),
                ("이동", info["move"]),
                ("숙소", HOTEL),
                ("미준비", left_txt if left else "모두 챙겼어요 🎉"),
                ("체크", "여권 · e-티켓 · 엔화 · 충전기"),
            ],
            "link_path": "/index.html#secPacking",
            "button": "준비물 체크하기",
        }
    if day not in DAYS:
        raise HTTPException(400, "day는 0~4 사이여야 해요")
    info, ds = DAYS[day], TRIP_DATES[day - 1]
    return {
        "title": f"🗾 삿포로 {day}일차 · {_date_label(ds)}",
        "description": info["title"],
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
    if CARD_IMAGE:
        content.update({"image_url": CARD_IMAGE, "image_width": 800, "image_height": 400})
    return {
        "object_type": "feed",
        "content": content,
        "item_content": {
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


def register_trip_jobs(scheduler):
    """scheduler.py의 start_scheduler()에서 호출. 지난 시각은 건너뜀."""
    plan = [(0, "2026-12-15 20:00")] + [(i + 1, f"{d} 07:00") for i, d in enumerate(TRIP_DATES)]
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)  # 스케줄러와 같은 한국시간 기준
    for day, when in plan:
        run_at = datetime.strptime(when, "%Y-%m-%d %H:%M")
        if run_at <= now:
            continue
        scheduler.add_job(send_to_all, "date", run_date=run_at, args=[day],
                          id=f"trip_brief_day{day}", replace_existing=True, misfire_grace_time=3600)


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
