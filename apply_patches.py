"""
서버의 /root/contigoway 안에서 실행:
    python3 apply_patches.py

models.py, main.py, schedule.py를 정확한 문자열 매칭으로 수정한다.
매칭되는 부분이 없거나 이미 패치되어 있으면 그 파일은 건드리지 않고 넘어간다.
원본은 각각 .bak으로 백업해둔다.
"""

from pathlib import Path

FILES = {
    "models.py": [
        (
            '    event_time = Column(String(10), nullable=True)\n    created_at = Column(DateTime(timezone=True), server_default=func.now())\n\n    owner = relationship("User")',
            '    event_time = Column(String(10), nullable=True)\n'
            '    google_event_id = Column(String(255), nullable=True, index=True)\n'
            '    last_modified_source = Column(String(20), nullable=False, default="contigoway")\n'
            '    created_at = Column(DateTime(timezone=True), server_default=func.now())\n\n'
            '    owner = relationship("User")',
        ),
    ],
    "main.py": [
        (
            "from announcements import router as announcements_router\n",
            "from announcements import router as announcements_router\n"
            "from models_addition import GoogleAccount  # noqa: F401  (create_all이 인식하도록 import)\n"
            "from google_auth import router as google_auth_router\n"
            "from scheduler import start_scheduler\n",
        ),
        (
            "app.include_router(announcements_router)\n",
            "app.include_router(announcements_router)\n"
            "app.include_router(google_auth_router)\n",
        ),
        (
            '@app.get("/")\ndef root():',
            '@app.on_event("startup")\n'
            'def start_google_sync_scheduler():\n'
            '    start_scheduler()\n\n\n'
            '@app.get("/")\ndef root():',
        ),
    ],
    "schedule.py": [
        (
            "router = APIRouter(prefix=\"/schedule\", tags=[\"스케줄\"])\n",
            "router = APIRouter(prefix=\"/schedule\", tags=[\"스케줄\"])\n\n"
            "from sync_service import push_to_google  # noqa: E402\n",
        ),
        (
            "    db.add(event)\n    db.commit()\n    db.refresh(event)\n    return event",
            "    db.add(event)\n    db.commit()\n    db.refresh(event)\n"
            "    push_to_google(db, current_user.id, event, action=\"create\")\n    return event",
        ),
        (
            "    event.event_time = data.event_time\n    db.commit()\n    db.refresh(event)\n    return event",
            "    event.event_time = data.event_time\n    db.commit()\n    db.refresh(event)\n"
            "    push_to_google(db, current_user.id, event, action=\"update\")\n    return event",
        ),
        (
            "    event = _get_owned_event(event_id, db, current_user)\n    db.delete(event)\n    db.commit()\n    return {\"message\": \"일정이 삭제되었습니다\"}",
            "    event = _get_owned_event(event_id, db, current_user)\n"
            "    push_to_google(db, current_user.id, event, action=\"delete\")\n"
            "    db.delete(event)\n    db.commit()\n    return {\"message\": \"일정이 삭제되었습니다\"}",
        ),
    ],
}


def apply_patches():
    for filename, patches in FILES.items():
        path = Path(filename)
        if not path.exists():
            print(f"[스킵] {filename} 없음")
            continue

        text = path.read_text(encoding="utf-8")
        original = text
        changed = False

        for old, new in patches:
            if new in text:
                continue  # 이미 패치됨
            if old not in text:
                print(f"[경고] {filename}: 매칭 실패 -> 아래 블록을 수동으로 확인해주세요:\n{old[:80]}...")
                continue
            text = text.replace(old, new, 1)
            changed = True

        if changed:
            path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
            path.write_text(text, encoding="utf-8")
            print(f"[완료] {filename} 패치 적용됨 (원본은 {filename}.bak)")
        else:
            print(f"[변경 없음] {filename}")


if __name__ == "__main__":
    apply_patches()
