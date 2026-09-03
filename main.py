import os
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import engine, Base, SessionLocal
from models import User
from auth import router as auth_router, get_password_hash
from admin import router as admin_router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Contigoway API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://contigoway.com", "https://www.contigoway.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(admin_router)


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
