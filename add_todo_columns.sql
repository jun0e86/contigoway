-- TODO 기능 확장: 기존 todo_items 테이블에 새 컬럼 추가
-- (SQLAlchemy의 create_all은 새 테이블(todo_categories/todo_subitems/todo_media)은
--  자동으로 만들어주지만, 이미 존재하는 todo_items 테이블에 컬럼을 추가해주지는 않으므로
--  이 스크립트를 먼저 수동으로 실행해야 합니다.)
--
-- 실행 방법:
--   docker exec -i contigoway-db psql -U contigoway -d contigoway < add_todo_columns.sql

ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS category_id INTEGER;
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS priority VARCHAR(10);
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS progress_start INTEGER;
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS progress_end INTEGER;
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS progress_current INTEGER;
