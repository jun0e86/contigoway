"""
GoogleAccount 테이블 정의.
main.py에서 이 파일을 import해야 Base.metadata.create_all() 때 테이블이 실제로 생성된다.
(schedule_events 등 기존 테이블에 컬럼 추가하는 것은 이 파일이 아니라 add_google_columns.sql 로 처리)
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class GoogleAccount(Base):
    __tablename__ = "google_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    encrypted_refresh_token = Column(Text, nullable=False)
    encrypted_access_token = Column(Text, nullable=True)
    access_token_expiry = Column(DateTime, nullable=True)

    # Google Calendar 증분 동기화용 토큰 (폴링 방식 핵심)
    sync_token = Column(String(512), nullable=True)

    # events.watch push notification 용 (추후 확장 대비, 지금은 안 씀)
    watch_channel_id = Column(String(255), nullable=True)
    watch_resource_id = Column(String(255), nullable=True)
    watch_expiry = Column(DateTime, nullable=True)

    connected_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
