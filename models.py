from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Date,
    Boolean,
    Text,
    ForeignKey,
    Float,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False)
    # 전화번호로도 회원가입/로그인 가능하도록. 선택 입력, 중복 불가.
    phone = Column(String(20), unique=True, nullable=True)
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


class TodoCategory(Base):
    """TODO 대분류 (청구작업/오더리뷰/전화예약 등). 사용자가 화면에서 자유롭게 추가/수정."""

    __tablename__ = "todo_categories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    emoji = Column(String(10), nullable=True)
    color = Column(String(20), nullable=True)  # 카드/칩 색상 (hex)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User")


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
    # 완료 처리된 시각 (타임테이블: 언제 등록하고 언제 끝났는지 표시용)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # 대분류 (청구작업/오더리뷰 등). 미지정 가능.
    category_id = Column(Integer, ForeignKey("todo_categories.id"), nullable=True, index=True)
    # 신호등 우선순위: 'red' | 'yellow' | 'green' (미지정 가능)
    priority = Column(String(10), nullable=True)
    # 청구작업처럼 "몇 번부터 몇 번까지 중 어디까지 처리했는지" 숫자 범위 진행률
    progress_start = Column(Integer, nullable=True)
    progress_end = Column(Integer, nullable=True)
    progress_current = Column(Integer, nullable=True)

    owner = relationship("User")
    category = relationship("TodoCategory")
    subitems = relationship(
        "TodoSubItem", back_populates="todo", cascade="all, delete-orphan",
        order_by="TodoSubItem.sort_order",
    )
    media = relationship(
        "TodoMedia", back_populates="todo", cascade="all, delete-orphan",
    )


class TodoSubItem(Base):
    """할 일 하나를 쪼갠 하위 체크리스트 항목 (청구작업 등 큰 일을 단계별로 체크할 때 사용)."""

    __tablename__ = "todo_subitems"

    id = Column(Integer, primary_key=True, index=True)
    todo_id = Column(Integer, ForeignKey("todo_items.id"), nullable=False, index=True)
    content = Column(String(300), nullable=False)
    is_done = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)

    todo = relationship("TodoItem", back_populates="subitems")


class TodoMedia(Base):
    """TODO 항목에 첨부된 사진 (jpg/png/heic 등). 아이폰 HEIC는 업로드 시 JPEG로 변환."""

    __tablename__ = "todo_media"

    id = Column(Integer, primary_key=True, index=True)
    todo_id = Column(Integer, ForeignKey("todo_items.id"), nullable=False, index=True)
    file_path = Column(String(500), nullable=False)
    thumbnail_path = Column(String(500), nullable=True)
    original_filename = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    todo = relationship("TodoItem", back_populates="media")


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
    google_event_id = Column(String(255), nullable=True, index=True)
    last_modified_source = Column(String(20), nullable=False, default="contigoway")
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
    # AI가 일기 내용을 보고 남기는 응원/칭찬 코멘트 (저장 시 자동 생성, 실패 시 null)
    ai_comment = Column(Text, nullable=True)

    # 그 날짜의 실제 날씨 기록 (WMO 날씨코드 + 최고/최저기온). 조회 실패 시 null.
    weather_code = Column(Integer, nullable=True)
    weather_temp_max = Column(Integer, nullable=True)
    weather_temp_min = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    owner = relationship("User")
    media = relationship(
        "DiaryMedia", back_populates="entry", cascade="all, delete-orphan"
    )
    comments = relationship(
        "DiaryComment", back_populates="entry", cascade="all, delete-orphan",
        order_by="DiaryComment.created_at",
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


class DiaryComment(Base):
    """일기 항목에 대한 댓글 (본인 또는 관리자/파트너가 남길 수 있음)."""

    __tablename__ = "diary_comments"

    id = Column(Integer, primary_key=True, index=True)
    diary_entry_id = Column(
        Integer, ForeignKey("diary_entries.id"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    entry = relationship("DiaryEntry", back_populates="comments")
    author = relationship("User")


class PageVisit(Base):
    """방문자 카운터용 - 페이지 로드 1회당 1행. 개인식별정보는 저장하지 않음."""

    __tablename__ = "page_visits"

    id = Column(Integer, primary_key=True, index=True)
    visited_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class Announcement(Base):
    """로그인 전/후 화면에 뜨는 공지 팝업. start_at~end_at 기간과 is_active를 함께 만족해야 노출됨."""

    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    start_at = Column(Date, nullable=True)
    end_at = Column(Date, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    author = relationship("User")


class Photo(Base):
    """사진모음에 업로드된 이미지(jpg)/동영상(mov) 파일과 촬영 메타데이터."""

    __tablename__ = "photos"

    id = Column(Integer, primary_key=True, index=True)
    uploader_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    media_type = Column(String(10), nullable=False)  # "image" or "video"
    file_path = Column(String(500), nullable=False)
    thumbnail_path = Column(String(500), nullable=True)
    original_filename = Column(String(255), nullable=True)
    taken_at = Column(DateTime(timezone=True), nullable=True)       # 촬영일시 (EXIF, 없으면 NULL)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_name = Column(String(255), nullable=True)               # GPS -> 장소명 변환 결과
    weather_code = Column(Integer, nullable=True)
    weather_temp_max = Column(Integer, nullable=True)
    weather_temp_min = Column(Integer, nullable=True)
    occasion = Column(String(500), nullable=True)                     # 업로드 시 직접 입력한 태그/메모
    category = Column(String(50), nullable=True)                       # 큰 분류 (일상/여름휴가/해외여행 등)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    uploader = relationship("User")
    comments = relationship("PhotoComment", back_populates="photo", cascade="all, delete-orphan")


class PhotoComment(Base):
    """사진에 대한 댓글."""

    __tablename__ = "photo_comments"

    id = Column(Integer, primary_key=True, index=True)
    photo_id = Column(Integer, ForeignKey("photos.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    photo = relationship("Photo", back_populates="comments")
    author = relationship("User")
