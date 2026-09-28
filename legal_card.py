"""
legal_card.py

법령검색 결과의 핵심을 카카오톡 '나에게 보내기' 카드(피드 템플릿)로 보낸다.
화면(legal-search.html)이 이미 받아온 검색 결과에서 핵심 줄만 골라 보내므로
법령 API를 다시 호출하지 않는다. 받는 사람은 버튼을 누른 로그인 사용자 본인뿐이다.

  POST /legal-card/send   {q, items:[{label, text}], counts:{articles, precedents, interpretations}}

개인정보 보호: 카카오 서버에 메시지가 남으므로, 검색어·내용에서
주민등록번호·전화번호·이메일 패턴은 발송 전에 자동으로 가린다.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from social_auth import SITE_URL, SocialAccount, _http, get_kakao_access_token

router = APIRouter(prefix="/legal-card", tags=["법령검색 카카오 카드"])

CARD_IMAGE = SITE_URL.rstrip("/") + "/img/legal-card.jpg"
KST = timezone(timedelta(hours=9))

_MASKS = [
    (re.compile(r"\d{6}\s*-\s*[1-8]\d{6}"), "******-*******"),               # 주민등록번호
    (re.compile(r"01[016789]\s*-?\s*\d{3,4}\s*-?\s*\d{4}"), "010-****-****"),  # 휴대전화
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***@***"),                     # 이메일
]


def _mask(text: str) -> str:
    for pat, rep in _MASKS:
        text = pat.sub(rep, text)
    return text


class CardItem(BaseModel):
    label: str = Field(max_length=6)
    text: str = Field(max_length=300)


class CardCounts(BaseModel):
    articles: int = 0
    precedents: int = 0
    interpretations: int = 0


class CardRequest(BaseModel):
    q: str = Field(min_length=1, max_length=500)
    items: List[CardItem] = Field(default_factory=list)  # 앞 5개만 사용
    counts: Optional[CardCounts] = None


def _template(q: str, items: List[CardItem], counts: Optional[CardCounts]) -> dict:
    url = SITE_URL.rstrip("/") + "/legal-search.html?q=" + quote(q[:200])
    link = {"web_url": url, "mobile_web_url": url}
    c = counts or CardCounts()
    desc = f"조문 {c.articles} · 판례 {c.precedents} · 해석례 {c.interpretations}건"
    return {
        "object_type": "feed",
        "content": {
            "title": f"“{q[:60]}”",
            "description": desc,
            "image_url": CARD_IMAGE,
            "image_width": 800,
            "image_height": 400,
            "link": link,
        },
        "item_content": {
            "title_image_text": "⚖️ 법령검색 요약",
            "title_image_category": datetime.now(KST).strftime("%m/%d %H:%M"),
            "items": [{"item": it.label[:6], "item_op": it.text[:50]} for it in items[:5]],
        },
        "buttons": [{"title": "검색 결과 전체 보기", "link": link}],
    }


@router.post("/send")
def send_card(body: CardRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not body.items:
        raise HTTPException(400, "보낼 검색 결과가 없어요")
    acct = db.query(SocialAccount).filter_by(provider="kakao", user_id=current_user.id).first()
    if not acct or not acct.scopes or "talk_message" not in acct.scopes:
        raise HTTPException(409, "카카오 연결(메시지 전송 동의)이 필요해요. 카카오로 로그인해 주세요")

    q = _mask(body.q.strip())
    items = [CardItem(label=_mask(i.label.strip()) or "-", text=_mask(i.text.strip()) or "-") for i in body.items[:5]]
    token = get_kakao_access_token(db, acct)
    _http(
        "POST",
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        {"template_object": json.dumps(_template(q, items, body.counts), ensure_ascii=False)},
        headers={"Authorization": f"Bearer {token}"},
    )
    return {"ok": True, "masked": q != body.q.strip()}
