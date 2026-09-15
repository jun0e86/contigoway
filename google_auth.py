import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from crypto_utils import encrypt_token
from models_addition import GoogleAccount
from database import get_db
from auth import get_current_user, SECRET_KEY, ALGORITHM
from models import User

router = APIRouter(prefix="/google", tags=["google-sync"])

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CLIENT_CONFIG = {
    "web": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [os.environ["GOOGLE_REDIRECT_URI"]],
    }
}


def _get_user_from_query_token(token: str, db: Session) -> User:
    """/authorize는 브라우저 주소창 이동(GET) 방식이라 Authorization 헤더를 못 실어서,
    쿼리 파라미터로 넘어온 토큰을 auth.py와 동일한 방식으로 직접 검증한다."""
    credentials_exception = HTTPException(status_code=401, detail="인증 정보가 유효하지 않습니다")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    if user.status != "active":
        raise HTTPException(status_code=403, detail="승인 대기중이거나 비활성화된 계정입니다")
    return user


@router.get("/authorize")
def google_authorize(
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """프론트엔드에서 버튼 클릭 시:
    window.location.href = '/api/google/authorize?token=' + localStorage.getItem('token')
    형태로 호출한다 (헤더 대신 쿼리로 토큰 전달, 리다이렉트 플로우라서 불가피함)."""
    if not token:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    current_user = _get_user_from_query_token(token, db)

    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=str(current_user.id),
    )
    return RedirectResponse(auth_url)


@router.get("/callback")
def google_callback(code: str, state: str, db: Session = Depends(get_db)):
    user_id = int(state)

    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]
    flow.fetch_token(code=code)

    creds: Credentials = flow.credentials

    account = db.query(GoogleAccount).filter_by(user_id=user_id).first()
    if account is None:
        account = GoogleAccount(user_id=user_id)
        db.add(account)

    account.encrypted_refresh_token = encrypt_token(creds.refresh_token)
    account.encrypted_access_token = encrypt_token(creds.token)
    account.access_token_expiry = creds.expiry
    db.commit()

    return RedirectResponse("https://contigoway.com/settings.html?google_connected=1")


@router.post("/disconnect")
def google_disconnect(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = db.query(GoogleAccount).filter_by(user_id=current_user.id).first()
    if not account:
        raise HTTPException(404, "연동된 구글 계정이 없습니다.")
    db.delete(account)
    db.commit()
    return {"ok": True}
