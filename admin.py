"""
admin.py

관리자 전용 회원 관리 API.
  GET    /admin/users/pending   - 승인 대기중인 회원 목록
  GET    /admin/users           - 전체 회원 목록
  POST   /admin/users/{id}/approve - 승인 (status -> active)
  POST   /admin/users/{id}/reject  - 거부 (계정 삭제)
  POST   /admin/users/{id}/disable - 비활성화
  POST   /admin/users/{id}/enable  - 재활성화 (status -> active)
  POST   /admin/users/{id}/promote - 관리자로 지정 (role -> admin)
  POST   /admin/users/{id}/demote  - 관리자 해제 (role -> user)
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import User
from auth import require_admin, UserOut

router = APIRouter(prefix="/admin", tags=["관리자"])


@router.get("/users/pending", response_model=list[UserOut])
def list_pending(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(User).filter(User.status == "pending").order_by(User.created_at).all()


@router.get("/users", response_model=list[UserOut])
def list_all(db: Session = Depends(get_db), _=Depends(require_admin)):
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/users/{user_id}/approve")
def approve(user_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    user.status = "active"
    user.approved_at = datetime.utcnow()
    db.commit()
    return {"message": f"{user.full_name}님의 가입을 승인했습니다"}


@router.post("/users/{user_id}/reject")
def reject(user_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    db.delete(user)
    db.commit()
    return {"message": "가입 신청을 거부했습니다"}


@router.post("/users/{user_id}/disable")
def disable(user_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    user.status = "disabled"
    db.commit()
    return {"message": f"{user.full_name}님의 계정을 비활성화했습니다"}


@router.post("/users/{user_id}/enable")
def enable(user_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    user.status = "active"
    db.commit()
    return {"message": f"{user.full_name}님의 계정을 다시 활성화했습니다"}


@router.post("/users/{user_id}/promote")
def promote(user_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    user.role = "admin"
    db.commit()
    return {"message": f"{user.full_name}님을 관리자로 지정했습니다"}


@router.post("/users/{user_id}/demote")
def demote(user_id: int, db: Session = Depends(get_db), current=Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    if user.id == current.id:
        raise HTTPException(400, "본인의 관리자 권한은 해제할 수 없습니다")
    admin_count = db.query(User).filter(User.role == "admin").count()
    if admin_count <= 1:
        raise HTTPException(400, "최소 1명의 관리자가 있어야 합니다")
    user.role = "user"
    db.commit()
    return {"message": f"{user.full_name}님의 관리자 권한을 해제했습니다"}
