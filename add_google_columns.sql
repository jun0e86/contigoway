-- schedule_events 테이블에 구글 연동용 컬럼 추가.
-- 이 프로젝트는 alembic을 안 쓰고 Base.metadata.create_all()로 테이블을 자동생성하는 방식이라,
-- 기존 테이블(schedule_events)에 컬럼을 추가하는 건 SQL로 직접 실행해야 한다.
-- google_accounts 테이블 자체는 main.py에서 models_addition을 import하면 create_all이 자동 생성해준다.

ALTER TABLE schedule_events
    ADD COLUMN IF NOT EXISTS google_event_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS last_modified_source VARCHAR(20) NOT NULL DEFAULT 'contigoway';

CREATE INDEX IF NOT EXISTS ix_schedule_events_google_event_id
    ON schedule_events (google_event_id);
