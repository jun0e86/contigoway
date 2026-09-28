"""
flights.py

Aviationstack API를 프록시해서 인천(ICN)↔신치토세(CTS) 노선의 실시간 운항 상태를 보여준다.
direction 파라미터로 가는편(outbound: ICN→CTS)/오는편(inbound: CTS→ICN)을 선택할 수 있다.

API 키는 서버 .env.docker의 AVIATIONSTACK_API_KEY에만 있고, 프론트엔드(브라우저)에는 절대
노출되지 않는다 (브라우저는 이 엔드포인트만 호출하고, 이 엔드포인트가 대신 aviationstack을 호출).

무료 플랜 제약 대응 (월 100회, 당일 실시간 데이터만 제공)
- 가는편은 출발일(12/16), 오는편은 귀국일(12/19) 당일(한국시간)에만 실제로 호출한다.
  그 외 날짜에는 API를 호출하지 않고 mode="waiting"을 돌려준다.
- 같은 방향 결과는 15분간 메모리에 캐시한다.
- 방향별 하루 최대 호출 횟수를 제한한다 (기본 30회 → 여행 전체 최대 60회).
- 한도 초과(429) 응답을 받으면 1시간 동안 재호출하지 않는다.

선택 환경변수
- FLIGHT_NUMBERS_OUTBOUND / FLIGHT_NUMBERS_INBOUND : 예약한 편명(IATA)만 보여주고 싶을 때
  쉼표로 구분 (예: "OZ174"). 비워두면 노선 전체를 보여준다.
- FLIGHTS_FORCE_LIVE=1 : 날짜 제한을 무시하고 호출 (테스트용, 한도 소모 주의)
"""

import os
import time
import threading
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user

router = APIRouter(prefix="/flights", tags=["항공 운항정보"])

AVIATIONSTACK_BASE = "http://api.aviationstack.com/v1/flights"
KST = timezone(timedelta(hours=9))

TRIP_DATES = {"outbound": "2026-12-16", "inbound": "2026-12-19"}
ROUTES = {"outbound": ("ICN", "CTS"), "inbound": ("CTS", "ICN")}

CACHE_TTL_SEC = 15 * 60
DAILY_CAP_PER_DIRECTION = 30
LIMIT_BACKOFF_SEC = 60 * 60

_lock = threading.Lock()
_cache = {}          # direction -> {"at": epoch, "flights": [...]}
_calls = {}          # (direction, date) -> count
_blocked_until = 0.0  # 한도 초과 시 재호출 금지 시각


def _env_flight_numbers(direction: str):
    raw = os.environ.get(f"FLIGHT_NUMBERS_{direction.upper()}", "")
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _parse(data, wanted):
    flights = []
    for f in data.get("data") or []:
        fl = f.get("flight") or {}
        if fl.get("codeshared"):  # 공동운항 중복편 제외 (실제 운항편만)
            continue
        number = (fl.get("iata") or "").upper()
        if wanted and number not in wanted:
            continue
        dep = f.get("departure") or {}
        arr = f.get("arrival") or {}
        flights.append({
            "airline": (f.get("airline") or {}).get("name"),
            "flight_number": number,
            "status": f.get("flight_status"),
            "dep_scheduled": dep.get("scheduled"),
            "dep_estimated": dep.get("estimated"),
            "dep_terminal": dep.get("terminal"),
            "dep_gate": dep.get("gate"),
            "dep_delay": dep.get("delay"),
            "arr_scheduled": arr.get("scheduled"),
            "arr_estimated": arr.get("estimated"),
        })
        if len(flights) >= 10:
            break
    return flights


@router.get("/sapporo-status")
def sapporo_flight_status(
    direction: str = Query(default="outbound", pattern="^(outbound|inbound)$"),
    current_user=Depends(get_current_user),
):
    """direction='outbound' → 인천(ICN)→신치토세(CTS), 'inbound' → 신치토세(CTS)→인천(ICN)."""
    global _blocked_until

    today = datetime.now(KST).strftime("%Y-%m-%d")
    trip_date = TRIP_DATES[direction]
    base = {"direction": direction, "trip_date": trip_date}

    force = os.environ.get("FLIGHTS_FORCE_LIVE") == "1"
    if today != trip_date and not force:
        return {**base, "mode": "waiting", "flights": [], "count": 0}

    key = os.environ.get("AVIATIONSTACK_API_KEY")
    if not key:
        raise HTTPException(503, "항공 운항정보 API 키가 설정되지 않았습니다")

    now = time.time()
    with _lock:
        cached = _cache.get(direction)
        if cached and now - cached["at"] < CACHE_TTL_SEC:
            return {**base, "mode": "live", "flights": cached["flights"],
                    "count": len(cached["flights"]), "updated_at": cached["at"]}

        calls_today = _calls.get((direction, today), 0)
        if now < _blocked_until or calls_today >= DAILY_CAP_PER_DIRECTION:
            stale = cached["flights"] if cached else []
            return {**base, "mode": "limited", "flights": stale, "count": len(stale),
                    "updated_at": cached["at"] if cached else None}
        _calls[(direction, today)] = calls_today + 1

    dep_iata, arr_iata = ROUTES[direction]
    try:
        resp = requests.get(
            AVIATIONSTACK_BASE,
            params={"access_key": key, "dep_iata": dep_iata, "arr_iata": arr_iata},
            timeout=10,
        )
        data = resp.json()
    except Exception:
        raise HTTPException(502, "항공 운항정보를 가져오지 못했습니다")

    if "error" in data:
        err = data["error"] or {}
        code = err.get("code") or err.get("type") or ""
        if resp.status_code == 429 or "limit" in str(code):
            with _lock:
                _blocked_until = time.time() + LIMIT_BACKOFF_SEC
                cached = _cache.get(direction)
            stale = cached["flights"] if cached else []
            return {**base, "mode": "limited", "flights": stale, "count": len(stale),
                    "updated_at": cached["at"] if cached else None}
        msg = err.get("message") or err.get("info") or code or "알 수 없는 오류"
        print(f"[flights] aviationstack 오류: {msg}")
        raise HTTPException(502, f"항공 운항정보 조회 실패: {msg}")

    flights = _parse(data, _env_flight_numbers(direction))
    with _lock:
        _cache[direction] = {"at": time.time(), "flights": flights}
    return {**base, "mode": "live", "flights": flights, "count": len(flights),
            "updated_at": time.time()}
