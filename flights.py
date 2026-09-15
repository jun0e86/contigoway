"""
flights.py

Aviationstack API를 프록시해서 인천(ICN)↔신치토세(CTS) 노선의 실시간 운항 상태를 보여준다.
direction 파라미터로 가는편(outbound: ICN→CTS)/오는편(inbound: CTS→ICN)을 선택할 수 있다.

API 키는 서버 .env.docker의 AVIATIONSTACK_API_KEY에만 있고, 프론트엔드(브라우저)에는 절대
노출되지 않는다 (브라우저는 이 엔드포인트만 호출하고, 이 엔드포인트가 대신 aviationstack을 호출).

참고: aviationstack 무료 플랜은 "실시간" 운항정보만 지원해서 당일(現在) 기준 데이터만 나온다.
특정 미래/과거 날짜(flight_date) 조회는 Basic 플랜($49.99/월) 이상에서만 가능하다.
"""

import os
import requests
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user

router = APIRouter(prefix="/flights", tags=["항공 운항정보"])

AVIATIONSTACK_BASE = "http://api.aviationstack.com/v1/flights"


@router.get("/sapporo-status")
def sapporo_flight_status(
    direction: str = Query(default="outbound", pattern="^(outbound|inbound)$"),
    current_user=Depends(get_current_user),
):
    """direction='outbound' → 인천(ICN)→신치토세(CTS), 'inbound' → 신치토세(CTS)→인천(ICN)."""
    key = os.environ.get("AVIATIONSTACK_API_KEY")
    if not key:
        raise HTTPException(503, "항공 운항정보 API 키가 설정되지 않았습니다")

    if direction == "inbound":
        dep_iata, arr_iata = "CTS", "ICN"
    else:
        dep_iata, arr_iata = "ICN", "CTS"

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
        msg = data["error"].get("info") or data["error"].get("type") or "알 수 없는 오류"
        raise HTTPException(502, f"항공 운항정보 조회 실패: {msg}")

    flights = []
    for f in (data.get("data") or [])[:10]:
        flights.append({
            "airline": (f.get("airline") or {}).get("name"),
            "flight_number": (f.get("flight") or {}).get("iata"),
            "status": f.get("flight_status"),
            "dep_scheduled": (f.get("departure") or {}).get("scheduled"),
            "dep_terminal": (f.get("departure") or {}).get("terminal"),
            "arr_scheduled": (f.get("arrival") or {}).get("scheduled"),
        })

    return {"flights": flights, "count": len(flights), "direction": direction}
