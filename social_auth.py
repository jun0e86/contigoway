"""
social_auth.py

카카오·네이버 로그인 + 카카오톡 '나에게 보내기'.

흐름:
  GET  /auth/kakao/login        카카오 동의 화면으로 이동 (state 쿠키로 CSRF 방지)
  GET  /auth/kakao/callback     토큰 발급 → 연결된 계정이면 바로 로그인,
                                처음이면 10분짜리 연결 티켓과 함께 login.html로 복귀
  GET  /auth/naver/login        네이버 동의 화면으로 이동
  GET  /auth/naver/callback     (카카오와 동일한 흐름)
  POST /auth/social/link        기존 계정 아이디·비밀번호 확인 후 카카오 연결 (+ 로그인)
  POST /auth/social/register    새 계정 가입 신청 + 카카오 연결 (관리자 승인 필요)
  GET  /auth/social/status      내 소셜 연결 상태
  POST /auth/social/unlink/{provider}  소셜 연결 해제 (kakao / naver)
  POST /kakao/test-message      나에게 카카오톡 테스트 메시지

보안 메모:
  - 카카오 토큰은 FERNET_KEY로 암호화해 저장 (DB 유출 시 평문 노출 방지)
  - 이메일 자동 매칭 대신 본인 비밀번호 확인으로 계정 연결 (계정 탈취 방지)
  - 로그인 결과는 URL fragment(#)로 전달 → 서버 로그/Referer에 남지 않음
"""

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Session

try:
    from database import Base, get_db
except ImportError:  # Base가 models.py에 정의된 구조일 경우
    from database import get_db
    from models import Base

from models import User
from auth import (
    ALGORITHM,
    SECRET_KEY,
    check_new_user_conflicts,
    find_user_by_identifier,
    get_current_user,
    get_password_hash,
    login_response,
    normalize_phone,
    verify_password,
)

router = APIRouter(tags=["소셜 로그인"])

KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO_CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET", "")
KAKAO_REDIRECT_URI = os.environ.get(
    "KAKAO_REDIRECT_URI", "https://contigoway.com/api/auth/kakao/callback"
)
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
NAVER_REDIRECT_URI = os.environ.get(
    "NAVER_REDIRECT_URI", "https://contigoway.com/api/auth/naver/callback"
)
SITE_URL = os.environ.get("SITE_URL", "https://contigoway.com")

PROVIDER_NAME = {"kakao": "카카오", "naver": "네이버", "apple": "Apple"}
LINK_TICKET_MINUTES = 10

_fernet = Fernet(os.environ["FERNET_KEY"].encode())


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------
class SocialAccount(Base):
    """소셜 계정 연결 정보. user_id가 비어 있으면 아직 연결 대기중인 상태."""

    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_social_provider_uid"),
    )

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(20), nullable=False)          # kakao / naver / apple
    provider_user_id = Column(String(64), nullable=False)  # 카카오 회원번호 등
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    nickname = Column(String(100))

    access_token_enc = Column(Text)
    refresh_token_enc = Column(Text)
    access_expires_at = Column(DateTime(timezone=True))
    refresh_expires_at = Column(DateTime(timezone=True))
    scopes = Column(String(500))  # 동의한 항목 (예: "profile_nickname talk_message")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enc(value: Optional[str]) -> Optional[str]:
    return _fernet.encrypt(value.encode()).decode() if value else None


def _dec(value: Optional[str]) -> Optional[str]:
    return _fernet.decrypt(value.encode()).decode() if value else None


def _http(method: str, url: str, data: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded;charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:300]
        print(f"[kakao] {method} {url} 실패 {e.code}: {detail}")
        raise HTTPException(502, f"카카오 API 오류 ({e.code})")
    except urllib.error.URLError as e:
        print(f"[kakao] {method} {url} 연결 실패: {e}")
        raise HTTPException(502, "카카오 서버에 연결하지 못했습니다")


def _state_cookie(provider: str) -> str:
    return f"{provider}_oauth_state"


def _to_login(cookie_provider: str, **fragment) -> RedirectResponse:
    """login.html로 복귀. fragment(#)에 결과를 담고, 사용한 state 쿠키는 삭제."""
    resp = RedirectResponse(f"{SITE_URL}/login.html#" + urllib.parse.urlencode(fragment), status_code=302)
    resp.delete_cookie(_state_cookie(cookie_provider), path="/")
    return resp


def _start_oauth(provider: str, authorize_url: str, params: dict) -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    params = {**params, "response_type": "code", "state": state}
    resp = RedirectResponse(authorize_url + "?" + urllib.parse.urlencode(params), 302)
    resp.set_cookie(_state_cookie(provider), state, max_age=600, httponly=True, secure=True,
                    samesite="lax", path="/")
    return resp


def _state_ok(provider: str, request: Request, code: Optional[str], state: Optional[str]) -> bool:
    cookie_state = request.cookies.get(_state_cookie(provider))
    return bool(code and state and cookie_state and secrets.compare_digest(state, cookie_state))


def _save_tokens(acct: SocialAccount, tok: dict) -> None:
    acct.access_token_enc = _enc(tok["access_token"])
    acct.access_expires_at = _now() + timedelta(seconds=int(tok.get("expires_in", 0)))
    if tok.get("refresh_token"):  # 갱신 시에는 만료 1개월 전일 때만 새로 내려옴
        acct.refresh_token_enc = _enc(tok["refresh_token"])
        rt_exp = tok.get("refresh_token_expires_in")
        acct.refresh_expires_at = _now() + timedelta(seconds=int(rt_exp)) if rt_exp else None
    if tok.get("scope"):
        acct.scopes = tok["scope"]


def _make_link_ticket(acct_id: int) -> str:
    return jwt.encode(
        {"typ": "social_link", "sid": acct_id, "exp": _now() + timedelta(minutes=LINK_TICKET_MINUTES)},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _account_from_ticket(ticket: str, db: Session) -> SocialAccount:
    expired = HTTPException(400, "연결 시간이 만료되었어요. 카카오 로그인을 다시 시작해주세요")
    try:
        payload = jwt.decode(ticket, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise expired
    if payload.get("typ") != "social_link":  # 일반 로그인 토큰을 티켓으로 쓰는 것 방지
        raise expired
    acct = db.get(SocialAccount, payload.get("sid"))
    if not acct or acct.user_id:
        raise HTTPException(400, "이미 처리된 요청이에요. 다시 로그인해주세요")
    return acct


def _status_error(user: User) -> Optional[str]:
    if user.status == "pending":
        return "아직 관리자 승인 대기중이에요"
    if user.status == "disabled":
        return "비활성화된 계정이에요"
    return None


# ---------------------------------------------------------------------------
# 카카오 토큰 / 메시지 (여행 알림 등 다른 모듈에서도 사용)
# ---------------------------------------------------------------------------
def get_kakao_access_token(db: Session, acct: SocialAccount) -> str:
    """유효한 액세스 토큰 반환. 만료 5분 전이면 리프레시 토큰으로 자동 갱신."""
    if acct.access_expires_at and acct.access_expires_at > _now() + timedelta(minutes=5):
        return _dec(acct.access_token_enc)
    if not acct.refresh_token_enc or (acct.refresh_expires_at and acct.refresh_expires_at < _now()):
        raise HTTPException(409, "카카오 연결이 만료되었어요. 카카오로 다시 로그인해주세요")
    tok = _http(
        "POST",
        "https://kauth.kakao.com/oauth/token",
        {
            "grant_type": "refresh_token",
            "client_id": KAKAO_REST_API_KEY,
            "refresh_token": _dec(acct.refresh_token_enc),
            "client_secret": KAKAO_CLIENT_SECRET,
        },
    )
    _save_tokens(acct, tok)
    db.commit()
    return tok["access_token"]


def send_kakao_memo(db: Session, acct: SocialAccount, text: str, link_url: Optional[str] = None) -> dict:
    """카카오톡 '나에게 보내기' (텍스트 템플릿, 최대 200자)."""
    token = get_kakao_access_token(db, acct)
    url = link_url or SITE_URL
    template = {
        "object_type": "text",
        "text": text[:200],
        "link": {"web_url": url, "mobile_web_url": url},
        "button_title": "contigoway 열기",
    }
    return _http(
        "POST",
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        {"template_object": json.dumps(template, ensure_ascii=False)},
        headers={"Authorization": f"Bearer {token}"},
    )


def kakao_account_of(db: Session, user_id: int) -> Optional[SocialAccount]:
    return db.query(SocialAccount).filter_by(provider="kakao", user_id=user_id).first()


# ---------------------------------------------------------------------------
# 카카오 로그인
# ---------------------------------------------------------------------------
def _finish_social_login(db: Session, provider: str, provider_uid: str, nickname: str, tok: dict) -> RedirectResponse:
    """소셜 인증 성공 후 공통 처리: 연결된 계정이면 로그인, 아니면 연결 티켓 발급."""
    acct = db.query(SocialAccount).filter_by(provider=provider, provider_user_id=provider_uid).first()
    if not acct:
        acct = SocialAccount(provider=provider, provider_user_id=provider_uid)
        db.add(acct)
    acct.nickname = nickname
    _save_tokens(acct, tok)
    db.commit()
    db.refresh(acct)

    if acct.user_id:
        user = db.get(User, acct.user_id)
        if user:
            err = _status_error(user)
            if err:
                return _to_login(provider, social_error=err)
            data = login_response(user)
            return _to_login(provider, token=data["access_token"], full_name=data["full_name"], role=data["role"])

    return _to_login(provider, link_ticket=_make_link_ticket(acct.id), provider=provider, nickname=nickname)


@router.get("/auth/kakao/login")
def kakao_login():
    if not KAKAO_REST_API_KEY:
        raise HTTPException(503, "카카오 로그인이 아직 설정되지 않았습니다")
    return _start_oauth("kakao", "https://kauth.kakao.com/oauth/authorize",
                        {"client_id": KAKAO_REST_API_KEY, "redirect_uri": KAKAO_REDIRECT_URI})


@router.get("/auth/kakao/callback")
def kakao_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if error:
        return _to_login("kakao", social_error="카카오 로그인을 취소했어요")
    if not _state_ok("kakao", request, code, state):
        return _to_login("kakao", social_error="로그인 요청이 만료되었어요. 다시 시도해주세요")

    try:
        tok = _http(
            "POST",
            "https://kauth.kakao.com/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": KAKAO_REST_API_KEY,
                "redirect_uri": KAKAO_REDIRECT_URI,
                "code": code,
                "client_secret": KAKAO_CLIENT_SECRET,
            },
        )
        me = _http(
            "GET",
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )
    except HTTPException:
        return _to_login("kakao", social_error="카카오 인증에 실패했어요. 잠시 후 다시 시도해주세요")

    profile = (me.get("kakao_account") or {}).get("profile") or {}
    nickname = profile.get("nickname") or (me.get("properties") or {}).get("nickname") or "카카오 사용자"
    return _finish_social_login(db, "kakao", str(me["id"]), nickname, tok)


# ---------------------------------------------------------------------------
# 네이버 로그인
# ---------------------------------------------------------------------------
@router.get("/auth/naver/login")
def naver_login():
    if not NAVER_CLIENT_ID:
        raise HTTPException(503, "네이버 로그인이 아직 설정되지 않았습니다")
    return _start_oauth("naver", "https://nid.naver.com/oauth2.0/authorize",
                        {"client_id": NAVER_CLIENT_ID, "redirect_uri": NAVER_REDIRECT_URI})


@router.get("/auth/naver/callback")
def naver_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if error:
        return _to_login("naver", social_error="네이버 로그인을 취소했어요")
    if not _state_ok("naver", request, code, state):
        return _to_login("naver", social_error="로그인 요청이 만료되었어요. 다시 시도해주세요")

    try:
        tok = _http(
            "POST",
            "https://nid.naver.com/oauth2.0/token",
            {
                "grant_type": "authorization_code",
                "client_id": NAVER_CLIENT_ID,
                "client_secret": NAVER_CLIENT_SECRET,
                "code": code,
                "state": state,
            },
        )
        if "access_token" not in tok:  # 네이버는 실패도 200 + error 필드로 응답
            print(f"[naver] 토큰 발급 실패: {tok.get('error')} {tok.get('error_description')}")
            raise HTTPException(502, "네이버 토큰 발급 실패")
        me = _http(
            "GET",
            "https://openapi.naver.com/v1/nid/me",
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )
        if me.get("resultcode") != "00":
            print(f"[naver] 프로필 조회 실패: {me}")
            raise HTTPException(502, "네이버 프로필 조회 실패")
    except HTTPException:
        return _to_login("naver", social_error="네이버 인증에 실패했어요. 잠시 후 다시 시도해주세요")

    profile = me.get("response") or {}
    nickname = profile.get("nickname") or profile.get("name") or "네이버 사용자"
    return _finish_social_login(db, "naver", str(profile["id"]), nickname, tok)


# ---------------------------------------------------------------------------
# 계정 연결 / 소셜 가입
# ---------------------------------------------------------------------------
class LinkRequest(BaseModel):
    ticket: str
    username: str  # 아이디 또는 전화번호
    password: str


class SocialRegisterRequest(BaseModel):
    ticket: str
    username: str
    full_name: str
    email: EmailStr
    phone: Optional[str] = None


@router.post("/auth/social/link")
def social_link(data: LinkRequest, db: Session = Depends(get_db)):
    acct = _account_from_ticket(data.ticket, db)
    user = find_user_by_identifier(db, data.username)
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")

    if db.query(SocialAccount).filter_by(provider=acct.provider, user_id=user.id).first():
        raise HTTPException(409, f"이 계정에는 이미 다른 {PROVIDER_NAME.get(acct.provider, '')} 계정이 연결되어 있어요")

    acct.user_id = user.id
    db.commit()

    err = _status_error(user)
    if err:
        return {"message": f"{PROVIDER_NAME.get(acct.provider, '')} 계정을 연결했어요. {err}"}
    return login_response(user)


@router.post("/auth/social/register", status_code=201)
def social_register(data: SocialRegisterRequest, db: Session = Depends(get_db)):
    acct = _account_from_ticket(data.ticket, db)

    phone = None
    if data.phone and data.phone.strip():
        phone = normalize_phone(data.phone)
        if not phone:
            raise HTTPException(400, "전화번호는 010으로 시작하는 휴대폰 번호로 입력해주세요")

    username = data.username.strip()
    check_new_user_conflicts(db, username, data.email, phone)

    user = User(
        username=username,
        email=data.email,
        phone=phone,
        full_name=data.full_name.strip(),
        # 소셜 가입자는 비밀번호가 없음 → 추측 불가능한 임의값. 필요하면 암호 재설정으로 설정.
        hashed_password=get_password_hash(secrets.token_urlsafe(32)),
        status="pending",
        role="user",
    )
    db.add(user)
    db.flush()
    acct.user_id = user.id
    db.commit()
    name = PROVIDER_NAME.get(acct.provider, "소셜 계정")
    return {"message": f"가입 신청이 완료되었어요. 관리자 승인 후 {name}로 로그인할 수 있어요."}


@router.get("/auth/social/status")
def social_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(SocialAccount).filter_by(user_id=current_user.id).all()
    return [
        {
            "provider": r.provider,
            "nickname": r.nickname,
            "can_message": "talk_message" in (r.scopes or ""),
            "linked_at": r.updated_at,
        }
        for r in rows
    ]


@router.post("/auth/social/unlink/{provider}")
def social_unlink(provider: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    acct = db.query(SocialAccount).filter_by(provider=provider, user_id=current_user.id).first()
    name = PROVIDER_NAME.get(provider, provider)
    if not acct:
        raise HTTPException(404, f"연결된 {name} 계정이 없어요")
    try:  # 제공자 쪽 연결도 끊기 (실패해도 우리 쪽 연결은 해제)
        if provider == "kakao":
            token = get_kakao_access_token(db, acct)
            _http("POST", "https://kapi.kakao.com/v1/user/unlink",
                  headers={"Authorization": f"Bearer {token}"}, data={})
        elif provider == "naver" and acct.access_token_enc:
            _http("POST", "https://nid.naver.com/oauth2.0/token", {
                "grant_type": "delete",
                "client_id": NAVER_CLIENT_ID,
                "client_secret": NAVER_CLIENT_SECRET,
                "access_token": _dec(acct.access_token_enc),
                "service_provider": "NAVER",
            })
    except HTTPException:
        pass
    db.delete(acct)
    db.commit()
    return {"message": f"{name} 연결을 해제했어요"}


# ---------------------------------------------------------------------------
# 카카오톡 테스트 메시지
# ---------------------------------------------------------------------------
@router.post("/kakao/test-message")
def kakao_test_message(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    acct = kakao_account_of(db, current_user.id)
    if not acct:
        raise HTTPException(404, "카카오 계정이 연결되어 있지 않아요. 카카오로 로그인해 연결해주세요")
    if "talk_message" not in (acct.scopes or ""):
        raise HTTPException(403, "카카오톡 메시지 전송에 동의하지 않았어요. 연결 해제 후 다시 로그인하며 동의해주세요")
    send_kakao_memo(db, acct, f"✈️ contigoway 테스트\n{current_user.full_name}님, 카카오톡 알림이 정상 연결됐어요!")
    return {"message": "카카오톡으로 테스트 메시지를 보냈어요"}
