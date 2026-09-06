from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Date,
    Boolean,
    Text,
    ForeignKey,
)
from sqlalchemy.orm import relationship
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

    # 비밀일기 잠금용 PIN (4~6자리, 해시로 저장). 로그인 비밀번호와는 별개.
    diary_pin_hash = Column(String(200), nullable=True)

    # 캘린더 구독(webcal) 공유용 비밀 토큰. 사용자가 원할 때만 발급/재발급.
    calendar_feed_token = Column(String(64), unique=True, nullable=True, index=True)


class TodoItem(Base):
    """오늘의 TODO 리스트 항목."""

    __tablename__ = "todo_items"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(String(500), nullable=False)
    is_done = Column(Boolean, nullable=False, default=False)
    # 이 TODO가 속한 날짜 (예: 2026-09-06). 날짜별로 조회하기 위함.
    todo_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User")


class ScheduleEvent(Base):
    """캘린더 스케줄 이벤트."""

    __tablename__ = "schedule_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    event_date = Column(Date, nullable=False, index=True)
    # HH:MM 형식의 간단한 시간 표기 (선택 입력)
    event_time = Column(String(10), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User")


class DiaryEntry(Base):
    """비밀일기 항목. PIN 검증을 통과해야만 접근 가능."""

    __tablename__ = "diary_entries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=True)
    content = Column(Text, nullable=False)
    mood_emoji = Column(String(20), nullable=True)
    entry_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    owner = relationship("User")
    media = relationship(
        "DiaryMedia", back_populates="entry", cascade="all, delete-orphan"
    )


class DiaryMedia(Base):
    """일기에 첨부된 이미지(jpg)/동영상(mov) 파일."""

    __tablename__ = "diary_media"

    id = Column(Integer, primary_key=True, index=True)
    diary_entry_id = Column(
        Integer, ForeignKey("diary_entries.id"), nullable=False, index=True
    )
    media_type = Column(String(10), nullable=False)  # "image" or "video"
    file_path = Column(String(500), nullable=False)
    thumbnail_path = Column(String(500), nullable=True)
    original_filename = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    entry = relationship("DiaryEntry", back_populates="media")
