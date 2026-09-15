-- announcements 테이블에 노출 기간(start_at/end_at) 및 updated_at 컬럼 추가.
-- 이 프로젝트는 alembic을 안 쓰므로 SQL로 직접 실행한다.

ALTER TABLE announcements
    ADD COLUMN IF NOT EXISTS start_at DATE,
    ADD COLUMN IF NOT EXISTS end_at DATE,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
