from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False)
    full_name = Column(String(50), nullable=False)
    hashed_password = Column(String(200), nullable=False)

    # pending: 가입 신청, 승인 대기중 / active: 승인 완료, 로그인 가능 / disabled: 비활성화
    status = Column(String(20), nullable=False, default="pending")
    # user: 일반회원 / admin: 관리자 (승인/거부 권한)
    role = Column(String(20), nullable=False, default="user")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    approved_at = Column(DateTime(timezone=True), nullable=True)
