"""
account_recovery.py

아이디 찾기 / 암호 재설정 (가입 이메일로 발송).

  POST /auth/find-id                   이메일 입력 → 그 이메일로 아이디 안내 메일
  POST /auth/password-reset/request    아이디·전화번호·이메일 입력 → 재설정 링크 메일 (30분, 1회용)
  POST /auth/password-reset/confirm    링크의 토큰 + 새 비밀번호 → 비밀번호 변경

보안 메모:
  - 계정 존재 여부와 관계없이 항상 같은 응답 (가입자 목록 수집 방지)
  - 메일 발송은 백그라운드 처리 → 응답 시간 차이로 존재 여부를 알 수 없음
  - 재설정 토큰은 SHA-256 해시만 DB 저장, 30분 만료, 1회 사용, 새 요청 시 이전 토큰 무효화
  - 링크는 URL fragment(#)로 전달 → nginx 로그·Referer에 토큰이 남지 않음
  - IP당 요청 횟수 제한 (15분 5회)
"""

import hashlib
import os
import secrets
import smtplib
import ssl
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Session

try:
    from database import Base, get_db
except ImportError:
    from database import get_db
    from models import Base

from models import User
from auth import find_user_by_identifier, get_password_hash

router = APIRouter(prefix="/auth", tags=["계정 찾기"])

SITE_URL = os.environ.get("SITE_URL", "https://contigoway.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "contigoway")

RESET_MINUTES = 30
GENERIC_ID_MSG = "입력하신 이메일로 가입된 계정이 있으면 아이디를 메일로 보내드렸어요."
GENERIC_PW_MSG = "가입된 계정이 있으면 등록된 이메일로 암호 재설정 링크를 보내드렸어요. 30분 안에 열어주세요."


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    request_ip = Column(String(64))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


_hits: dict = defaultdict(deque)


def _rate_limit(request: Request, bucket: str, limit: int = 5, window_sec: int = 900) -> None:
    key = f"{bucket}:{_client_ip(request)}"
    q = _hits[key]
    now = time.time()
    while q and q[0] < now - window_sec:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "요청이 너무 많아요. 15분 후에 다시 시도해주세요")
    q.append(now)


def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return (name[:2] + "*" * max(len(name) - 2, 1)) + "@" + domain


def send_mail(to: str, subject: str, text: str, html: str) -> None:
    """Gmail SMTP(SSL 465)로 메일 발송. 실패해도 예외를 밖으로 던지지 않고 로그만 남김."""
    if not (SMTP_USER and SMTP_PASSWORD):
        print("[mail] SMTP 설정이 없어 메일을 보내지 못했어요 (SMTP_USER / SMTP_PASSWORD)")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_USER))
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=15) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        print(f"[mail] 발송 완료 → {_mask_email(to)} / {subject}")
    except Exception as e:  # noqa: BLE001
        print(f"[mail] 발송 실패 → {_mask_email(to)}: {e}")


def _html(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#F5F5F7;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif">
<div style="max-width:440px;margin:30px auto;background:#fff;border-radius:18px;padding:32px 28px;color:#1D1D1F">
  <div style="font-weight:700;font-size:18px;margin-bottom:18px">contigo<span style="color:#0071E3">way</span></div>
  <div style="font-size:17px;font-weight:700;margin-bottom:12px">{title}</div>
  <div style="font-size:14px;line-height:1.7">{body_html}</div>
  <div style="font-size:12px;color:#6E6E73;margin-top:26px;border-top:1px solid #D2D2D7;padding-top:14px">
    본인이 요청하지 않았다면 이 메일은 무시하셔도 돼요. 계정 정보는 바뀌지 않아요.
  </div>
</div></body></html>"""


def _find_user_for_reset(db: Session, value: str) -> Optional[User]:
    value = (value or "").strip()
    if "@" in value:
        return db.query(User).filter(func.lower(User.email) == value.lower()).first()
    return find_user_by_identifier(db, value)


# ---------------------------------------------------------------------------
# 아이디 찾기
# ---------------------------------------------------------------------------
class FindIdRequest(BaseModel):
    email: str


def _send_find_id(to: str, username: str, full_name: str) -> None:
    send_mail(
        to,
        "[contigoway] 아이디 안내",
        f"{full_name}님의 contigoway 아이디는 {username} 입니다.\n로그인: {SITE_URL}/login.html",
        _html(
            "아이디 안내",
            f"""{full_name}님의 contigoway 아이디는<br>
<div style="font-size:20px;font-weight:700;margin:14px 0;letter-spacing:.5px">{username}</div>
<a href="{SITE_URL}/login.html" style="display:inline-block;background:#1D1D1F;color:#fff;text-decoration:none;
padding:11px 20px;border-radius:10px;font-weight:600">로그인하러 가기</a>""",
        ),
    )


@router.post("/find-id")
def find_id(data: FindIdRequest, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    _rate_limit(request, "find-id")
    email = (data.email or "").strip()
    if "@" not in email:
        raise HTTPException(400, "이메일 주소를 입력해주세요")
    user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
    if user and user.status != "disabled":
        bg.add_task(_send_find_id, user.email, user.username, user.full_name)
    return {"message": GENERIC_ID_MSG}


# ---------------------------------------------------------------------------
# 암호 재설정
# ---------------------------------------------------------------------------
class ResetRequest(BaseModel):
    identifier: str  # 아이디, 전화번호 또는 이메일


class ResetConfirm(BaseModel):
    token: str
    new_password: str


def _send_reset(to: str, full_name: str, link: str) -> None:
    send_mail(
        to,
        "[contigoway] 암호 재설정 링크",
        f"{full_name}님, 아래 링크에서 새 암호를 설정해주세요 ({RESET_MINUTES}분 안에, 1회만 사용 가능).\n{link}",
        _html(
            "암호 재설정",
            f"""{full_name}님, 아래 버튼을 눌러 새 암호를 설정해주세요.<br>
링크는 <b>{RESET_MINUTES}분 동안, 한 번만</b> 사용할 수 있어요.<br><br>
<a href="{link}" style="display:inline-block;background:#0071E3;color:#fff;text-decoration:none;
padding:12px 22px;border-radius:10px;font-weight:600">새 암호 설정하기</a>""",
        ),
    )


@router.post("/password-reset/request")
def password_reset_request(data: ResetRequest, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    _rate_limit(request, "pw-reset")
    user = _find_user_for_reset(db, data.identifier)
    if not user or user.status == "disabled":
        return {"message": GENERIC_PW_MSG}

    # 같은 계정에 1분 안의 중복 요청은 무시 (메일 폭탄 방지)
    recent = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.user_id == user.id,
                PasswordResetToken.created_at > _now() - timedelta(minutes=1))
        .first()
    )
    if recent:
        return {"message": GENERIC_PW_MSG}

    # 이전에 발급된 미사용 토큰은 모두 무효화
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)
    ).update({PasswordResetToken.used_at: _now()}, synchronize_session=False)

    token = secrets.token_urlsafe(32)
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=_hash(token),
        expires_at=_now() + timedelta(minutes=RESET_MINUTES),
        request_ip=_client_ip(request),
    ))
    db.commit()

    link = f"{SITE_URL}/reset-password.html#token={token}"
    bg.add_task(_send_reset, user.email, user.full_name, link)
    return {"message": GENERIC_PW_MSG}


@router.post("/password-reset/confirm")
def password_reset_confirm(data: ResetConfirm, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    _rate_limit(request, "pw-confirm", limit=10)
    if len(data.new_password) < 8:
        raise HTTPException(400, "비밀번호는 8자 이상이어야 합니다")

    row = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == _hash(data.token or "")).first()
    if not row or row.used_at or row.expires_at < _now():
        raise HTTPException(400, "링크가 만료되었거나 이미 사용되었어요. 암호 재설정을 다시 요청해주세요")

    user = db.get(User, row.user_id)
    if not user or user.status == "disabled":
        raise HTTPException(400, "사용할 수 없는 계정이에요")

    user.hashed_password = get_password_hash(data.new_password)
    row.used_at = _now()
    db.commit()

    bg.add_task(
        send_mail,
        user.email,
        "[contigoway] 암호가 변경되었어요",
        f"{user.full_name}님의 contigoway 암호가 변경되었어요. 본인이 아니라면 관리자에게 바로 알려주세요.",
        _html("암호가 변경되었어요",
              f"{user.full_name}님의 contigoway 암호가 방금 변경되었어요.<br>"
              "본인이 변경한 게 아니라면 관리자에게 바로 알려주세요."),
    )
    return {"message": "새 암호로 변경했어요. 이제 로그인해주세요."}
