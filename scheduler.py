from apscheduler.schedulers.background import BackgroundScheduler

from database import SessionLocal
from models_addition import GoogleAccount
from sync_service import pull_from_google

_scheduler = BackgroundScheduler(timezone="Asia/Seoul")


def _poll_all_users():
    db = SessionLocal()
    try:
        accounts = db.query(GoogleAccount).all()
        for account in accounts:
            try:
                pull_from_google(db, account.user_id)
            except Exception as e:
                print(f"[google-sync] user_id={account.user_id} pull 실패: {e}")
    finally:
        db.close()


def start_scheduler():
    _scheduler.add_job(_poll_all_users, "interval", minutes=5, id="google_calendar_pull", replace_existing=True)
    _scheduler.start()
