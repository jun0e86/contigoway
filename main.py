import os
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import engine, Base, SessionLocal
from models import User
from auth import router as auth_router, get_password_hash
from admin import router as admin_router
from todo import router as todo_router
from schedule import router as schedule_router
from diary import router as diary_router

Base.metadata.create_all(bind=engine)

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/app/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Contigoway API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://contigoway.com", "https://www.contigoway.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 일기 첨부파일(이미지/동영상) 정적 서빙. 실제 서비스에서는 nginx가 /uploads를
# 직접 서빙하도록 구성하는 것을 권장 (여기서는 앱 단독 구동/개발 편의를 위해 포함).
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(todo_router)
app.include_router(schedule_router)
app.include_router(diary_router)


@app.on_event("startup")
def create_first_admin():
    """최초 기동 시 .env.docker에 지정된 관리자 계정이 없으면 자동 생성."""
    username = os.environ.get("FIRST_ADMIN_USERNAME")
    password = os.environ.get("FIRST_ADMIN_PASSWORD")
    email = os.environ.get("FIRST_ADMIN_EMAIL")
    if not (username and password and email):
        return

    db = SessionLocal()
    try:
        exists = db.query(User).filter(User.username == username).first()
        if exists:
            return
        admin = User(
            username=username,
            email=email,
            full_name="관리자",
            hashed_password=get_password_hash(password),
            status="active",
            role="admin",
            approved_at=datetime.utcnow(),
        )
        db.add(admin)
        db.commit()
        print(f"[초기 설정] 관리자 계정 '{username}' 생성 완료")
    finally:
        db.close()


@app.get("/")
def root():
    return {"message": "Contigoway API"}


@app.get("/health")
def health():
    return {"status": "ok"}
