"""
auth.py

인증 관련 공통 유틸 + 회원가입/로그인/내 정보 조회 라우터.

흐름:
  1) POST /auth/register  - 회원가입 신청 (status='pending'으로 생성, 바로 로그인은 안 됨)
  2) 관리자가 admin.py의 승인 API로 status를 'active'로 변경
  3) POST /auth/login      - active 상태인 계정만 로그인(JWT 토큰 발급) 가능
                             아이디 또는 전화번호(하이픈 유무 무관)로 로그인
  4) GET  /auth/me         - 토큰으로 내 정보 조회

소셜 로그인(카카오 등)은 social_auth.py 참고.
"""

import os
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import get_db
from models import User

router = APIRouter(prefix="/auth", tags=["인증"])

SECRET_KEY = os.environ["SECRET_KEY"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

PHONE_RE = re.compile(r"^01\d{8,9}$")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def normalize_phone(value: Optional[str]) -> Optional[str]:
    """'010-1234-5678', '010 1234 5678' → '01012345678'. 휴대폰 형식이 아니면 None."""
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return digits if PHONE_RE.match(digits) else None


def find_user_by_identifier(db: Session, identifier: str) -> Optional[User]:
    """아이디 또는 전화번호로 사용자 조회."""
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    phone = normalize_phone(identifier)
    if phone:
        user = db.query(User).filter(User.phone == phone).first()
        if user:
            return user
    return db.query(User).filter(User.username == identifier).first()


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증 정보가 유효하지 않습니다",
        headers={"WWW-Authenticate": "Bearer"},
    )
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


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    return current_user


def login_response(user: User) -> dict:
    """로그인 성공 응답 (일반 로그인/소셜 로그인 공통)."""
    return {
        "access_token": create_access_token({"sub": user.username}),
        "token_type": "bearer",
        "full_name": user.full_name,
        "role": user.role,
    }


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    full_name: str
    password: str
    phone: Optional[str] = None


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    status: str
    role: str

    class Config:
        from_attributes = True


def check_new_user_conflicts(db: Session, username: str, email: str, phone: Optional[str]) -> None:
    """가입 시 아이디/이메일/전화번호 중복 검사 (일반 가입/소셜 가입 공통)."""
    if len(username) < 3:
        raise HTTPException(400, "아이디는 3자 이상이어야 합니다")
    if not re.match(r"^[A-Za-z0-9._-]+$", username):
        raise HTTPException(400, "아이디는 영문, 숫자, . _ - 만 사용할 수 있습니다")
    if PHONE_RE.match(username):
        raise HTTPException(400, "전화번호 형식은 아이디로 쓸 수 없습니다")
    exists = db.query(User).filter(
        (User.username == username) | (User.email == email)
    ).first()
    if exists:
        raise HTTPException(400, "이미 사용중인 아이디 또는 이메일입니다")
    if phone and db.query(User).filter(User.phone == phone).first():
        raise HTTPException(400, "이미 등록된 전화번호입니다")


# ---------------------------------------------------------------------------
# 라우터
# ---------------------------------------------------------------------------
@router.post("/register", status_code=201)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    if len(data.password) < 8:
        raise HTTPException(400, "비밀번호는 8자 이상이어야 합니다")

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
        hashed_password=get_password_hash(data.password),
        status="pending",
        role="user",
    )
    db.add(user)
    db.commit()
    return {"message": "가입 신청이 완료되었습니다. 관리자 승인 후 로그인할 수 있습니다."}


@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = find_user_by_identifier(db, form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")
    if user.status == "pending":
        raise HTTPException(status_code=403, detail="아직 관리자 승인 대기중입니다")
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다")

    return login_response(user)


@router.get("/me", response_model=UserOut)
def read_me(current_user: User = Depends(get_current_user)):
    return current_user
