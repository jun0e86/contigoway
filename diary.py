"""
diary.py

비밀일기 API. 로그인 토큰과는 별개로 PIN(4자리 또는 6자리) 검증을 통과해야
일기 내용을 읽거나 쓸 수 있다.

흐름:
  1) POST /diary/pin/setup   - 최초 PIN 설정 (아직 설정 안 한 사용자만)
  2) POST /diary/pin/change  - 기존 PIN 확인 후 새 PIN으로 변경
  3) POST /diary/pin/verify  - PIN 확인 -> 30분짜리 diary 전용 토큰 발급
  4) 이후 모든 /diary/entries* 요청은 헤더 X-Diary-Token 에 위 토큰을 담아 보내야 함

미디어 업로드:
  - 이미지: jpg/jpeg/png/heic/heif/dng -> 썸네일(200x200) 자동 생성
    · heic/heif(아이폰 기본 사진 포맷)는 브라우저가 직접 표시하지 못하므로 업로드 시 JPEG로 변환해서 저장
    · dng(RAW)는 원본이 너무 크고 브라우저에서 아예 열리지 않으므로, 파일에 내장된 미리보기(JPEG)를
      추출해서 저장 (RAW 원본 데이터 자체는 보관하지 않음)
  - 동영상: mov -> 원본만 저장 (썸네일은 프론트에서 <video> 태그로 대체 가능)
"""

import io
import os
import uuid
import requests
from datetime import date as date_cls, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import DiaryEntry, DiaryMedia, DiaryComment, User
from auth import get_current_user, verify_password, get_password_hash, SECRET_KEY, ALGORITHM

router = APIRouter(prefix="/diary", tags=["비밀일기"])

DIARY_TOKEN_EXPIRE_MINUTES = 30
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/app/uploads")
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".dng"}
# 브라우저가 직접 렌더링하지 못해 업로드 시 JPEG로 변환해야 하는 포맷
CONVERT_TO_JPEG_EXT = {".heic", ".heif"}
RAW_EXT = {".dng"}
ALLOWED_VIDEO_EXT = {".mov"}
MAX_FILE_SIZE_MB = int(os.environ.get("DIARY_MAX_FILE_SIZE_MB", "100"))

# AI 응원 코멘트 생성 (현재는 이름+작성 횟수 인사말만. API 연동은 추후 추가 예정)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")


def generate_ai_comment(content: str, full_name: str, entry_count: int) -> Optional[str]:
    """일기 저장 시 보여줄 인사말 생성. (AI 분석 연동은 나중에 이 함수 안에 추가 예정)"""
    return f"{full_name}님, 오늘 하루도 정리하시느라 수고하셨어요! 이번이 {entry_count}번째 일기예요 🎉"


# 그 날짜의 실제 날씨 조회 (Open-Meteo Archive API, 무료/키 불필요)
# 기본 위치: 서울 (서울시청 기준)
WEATHER_LAT = float(os.environ.get("DIARY_WEATHER_LAT", "37.5665"))
WEATHER_LON = float(os.environ.get("DIARY_WEATHER_LON", "126.9780"))


def fetch_weather(entry_date: date_cls) -> Optional[dict]:
    """해당 날짜의 실제 최고/최저기온과 날씨코드를 가져옴. 실패하거나 데이터가 없으면 None."""
    try:
        date_str = entry_date.isoformat()
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "start_date": date_str,
                "end_date": date_str,
                "daily": "weathercode,temperature_2m_max,temperature_2m_min",
                "timezone": "Asia/Seoul",
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        codes = daily.get("weathercode", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        if not codes or codes[0] is None:
            return None
        return {
            "code": int(codes[0]),
            "temp_max": round(tmax[0]) if tmax and tmax[0] is not None else None,
            "temp_min": round(tmin[0]) if tmin and tmin[0] is not None else None,
        }
    except Exception:
        return None


# 사용자가 일기 작성 시 참고할 수 있는 예시 문구
DIARY_TEMPLATE_EXAMPLES = [
    "오늘 가장 기억에 남는 순간은 ○○였다. 그때 나는 △△한 기분이 들었다.",
    "오늘의 날씨: ☀️ / 오늘의 기분: 😊\n\n오늘 있었던 일: ...\n내일 하고 싶은 것: ...",
    "감사했던 일 3가지\n1. \n2. \n3. ",
]
MOOD_EMOJIS = ["😊", "😢", "😡", "😴", "😍", "😭", "🥳", "😐", "🤒", "🥰"]


def _validate_pin(pin: str):
    if not (pin.isdigit() and len(pin) in (4, 6)):
        raise HTTPException(400, "PIN은 4자리 또는 6자리 숫자여야 합니다")


class PinSetup(BaseModel):
    pin: str


class PinChange(BaseModel):
    current_pin: str
    new_pin: str


class PinVerify(BaseModel):
    pin: str


class DiaryEntryOut(BaseModel):
    id: int
    title: Optional[str]
    content: str
    mood_emoji: Optional[str]
    entry_date: date_cls
    media: list

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# PIN 설정/변경/검증
# ---------------------------------------------------------------------------
@router.post("/pin/setup")
def setup_pin(
    data: PinSetup,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.diary_pin_hash:
        raise HTTPException(400, "이미 PIN이 설정되어 있습니다. 변경은 /diary/pin/change를 사용하세요")
    _validate_pin(data.pin)
    current_user.diary_pin_hash = get_password_hash(data.pin)
    db.commit()
    return {"message": "일기 PIN이 설정되었습니다"}


@router.post("/pin/change")
def change_pin(
    data: PinChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.diary_pin_hash:
        raise HTTPException(400, "설정된 PIN이 없습니다. 먼저 /diary/pin/setup을 사용하세요")
    if not verify_password(data.current_pin, current_user.diary_pin_hash):
        raise HTTPException(401, "현재 PIN이 올바르지 않습니다")
    _validate_pin(data.new_pin)
    current_user.diary_pin_hash = get_password_hash(data.new_pin)
    db.commit()
    return {"message": "PIN이 변경되었습니다"}


@router.get("/pin/status")
def pin_status(current_user: User = Depends(get_current_user)):
    return {"pin_set": bool(current_user.diary_pin_hash)}


@router.post("/pin/verify")
def verify_pin(
    data: PinVerify,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.diary_pin_hash:
        raise HTTPException(400, "먼저 PIN을 설정해주세요")
    if not verify_password(data.pin, current_user.diary_pin_hash):
        raise HTTPException(401, "PIN이 올바르지 않습니다")

    expire = datetime.utcnow() + timedelta(minutes=DIARY_TOKEN_EXPIRE_MINUTES)
    token = jwt.encode(
        {"sub": current_user.username, "scope": "diary", "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    return {"diary_token": token, "expires_in_minutes": DIARY_TOKEN_EXPIRE_MINUTES}


def require_diary_access(
    x_diary_token: Optional[str] = Header(default=None),
    current_user: User = Depends(get_current_user),
) -> User:
    # 관리자는 PIN 없이 자신의 일기 API에 바로 접근 가능 (일반 사용자만 PIN 필요)
    if current_user.role == "admin":
        return current_user

    if not x_diary_token:
        raise HTTPException(401, "PIN 인증이 필요합니다 (X-Diary-Token 헤더 누락)")
    try:
        payload = jwt.decode(x_diary_token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("scope") != "diary" or payload.get("sub") != current_user.username:
            raise HTTPException(401, "유효하지 않은 PIN 인증 토큰입니다")
    except JWTError:
        raise HTTPException(401, "PIN 인증 토큰이 만료되었거나 유효하지 않습니다")
    return current_user


@router.get("/template")
def get_template():
    return {"examples": DIARY_TEMPLATE_EXAMPLES, "mood_emojis": MOOD_EMOJIS}


# ---------------------------------------------------------------------------
# 일기 CRUD (모두 require_diary_access 필요)
# ---------------------------------------------------------------------------
def _serialize_entry(entry: DiaryEntry, viewer: Optional[User] = None, db: Optional[Session] = None) -> dict:
    entry_number = None
    if db is not None:
        # entry_date 기준 오래된 순서로 번호를 매김 (같은 날짜면 먼저 작성한 것이 앞번호).
        # id 순서가 아니라 entry_date 순서로 매겨야, 목록을 최신순(entry_date desc)으로
        # 보여줄 때 번호가 4,3,2,1처럼 자연스럽게 내려가고 뒤죽박죽으로 보이지 않는다.
        entry_number = (
            db.query(DiaryEntry)
            .filter(
                DiaryEntry.user_id == entry.user_id,
                (DiaryEntry.entry_date < entry.entry_date)
                | (
                    (DiaryEntry.entry_date == entry.entry_date)
                    & (DiaryEntry.id <= entry.id)
                ),
            )
            .count()
        )
    return {
        "id": entry.id,
        "title": entry.title,
        "content": entry.content,
        "mood_emoji": entry.mood_emoji,
        "entry_date": entry.entry_date,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "ai_comment": entry.ai_comment,
        "entry_number": entry_number,
        "weather": (
            {
                "code": entry.weather_code,
                "temp_max": entry.weather_temp_max,
                "temp_min": entry.weather_temp_min,
            }
            if entry.weather_code is not None
            else None
        ),
        "media": [
            {
                "id": m.id,
                "media_type": m.media_type,
                "url": f"/uploads/{m.file_path}",
                "thumbnail_url": f"/uploads/{m.thumbnail_path}" if m.thumbnail_path else None,
                "original_filename": m.original_filename,
            }
            for m in entry.media
        ],
        "comments": [
            {
                "id": c.id,
                "content": c.content,
                "author_name": c.author.full_name,
                "is_admin": c.author.role == "admin",
                "is_mine": viewer is not None and c.user_id == viewer.id,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in entry.comments
        ],
    }


@router.get("/entries")
def list_entries(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    entries = (
        db.query(DiaryEntry)
        .filter(DiaryEntry.user_id == current_user.id)
        # 같은 entry_date에 여러 개를 쓴 경우까지 항상 같은 순서로 나오도록 id를 2차 정렬 기준으로 추가
        .order_by(DiaryEntry.entry_date.desc(), DiaryEntry.id.desc())
        .all()
    )
    return [_serialize_entry(e, current_user, db) for e in entries]


@router.get("/entries/{entry_id}")
def get_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    entry = (
        db.query(DiaryEntry)
        .filter(DiaryEntry.id == entry_id, DiaryEntry.user_id == current_user.id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "일기를 찾을 수 없습니다")
    return _serialize_entry(entry, current_user, db)


def _extract_dng_preview_bytes(abs_path: str) -> bytes:
    """DNG(RAW) 파일에서 내장 미리보기 이미지를 추출해 JPEG 바이트로 반환.
    RAW 픽셀을 직접 디코딩하지 않고 파일 안에 이미 들어있는 미리보기(보통 풀 해상도 JPEG)를
    꺼내오는 방식이라 빠르고 서버 부하가 적음. 미리보기가 없거나 추출 실패 시 예외 발생."""
    import rawpy
    from PIL import Image

    with rawpy.imread(abs_path) as raw:
        thumb = raw.extract_thumb()

    if thumb.format == rawpy.ThumbFormat.JPEG:
        return thumb.data
    elif thumb.format == rawpy.ThumbFormat.BITMAP:
        buf = io.BytesIO()
        Image.fromarray(thumb.data).convert("RGB").save(buf, "JPEG", quality=92)
        return buf.getvalue()
    else:
        raise ValueError("DNG 파일에서 미리보기를 추출할 수 없습니다")


def _save_media_file(upload: UploadFile, user_id: int) -> DiaryMedia:
    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext in ALLOWED_IMAGE_EXT:
        media_type = "image"
    elif ext in ALLOWED_VIDEO_EXT:
        media_type = "video"
    else:
        raise HTTPException(
            400,
            f"지원하지 않는 파일 형식입니다: {ext} (jpg/jpeg/png/heic/heif/dng, mov만 가능)",
        )

    user_dir = os.path.join(UPLOAD_DIR, "diary", str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    # 저장용 임시 파일명. heic/heif/dng는 최종적으로 JPEG로 변환해서 저장하므로
    # 실제 디스크에 남는 확장자는 아래에서 media_type에 따라 결정됨.
    temp_filename = f"{uuid.uuid4().hex}{ext}"
    temp_abs_path = os.path.join(user_dir, temp_filename)

    size = 0
    with open(temp_abs_path, "wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE_MB * 1024 * 1024:
                out.close()
                os.remove(temp_abs_path)
                raise HTTPException(400, f"파일 용량은 {MAX_FILE_SIZE_MB}MB를 넘을 수 없습니다")
            out.write(chunk)

    filename = temp_filename
    abs_path = temp_abs_path
    thumb_rel_path = None

    if media_type == "image":
        try:
            from PIL import Image

            if ext in CONVERT_TO_JPEG_EXT:
                # heic/heif -> pillow-heif로 디코딩 후 브라우저가 바로 볼 수 있는 JPEG로 저장
                import pillow_heif

                pillow_heif.register_heif_opener()
                jpeg_filename = f"{uuid.uuid4().hex}.jpg"
                jpeg_abs_path = os.path.join(user_dir, jpeg_filename)
                with Image.open(abs_path) as img:
                    img.convert("RGB").save(jpeg_abs_path, "JPEG", quality=92)
                os.remove(abs_path)  # 원본 heic는 용량만 차지하므로 변환 후 삭제
                filename = jpeg_filename
                abs_path = jpeg_abs_path

            elif ext in RAW_EXT:
                # dng(RAW) -> 내장 미리보기를 꺼내서 JPEG로 저장 (RAW 원본은 보관하지 않음)
                preview_bytes = _extract_dng_preview_bytes(abs_path)
                os.remove(abs_path)  # 용량이 큰 RAW 원본은 미리보기 추출 후 삭제
                jpeg_filename = f"{uuid.uuid4().hex}.jpg"
                jpeg_abs_path = os.path.join(user_dir, jpeg_filename)
                with open(jpeg_abs_path, "wb") as f:
                    f.write(preview_bytes)
                filename = jpeg_filename
                abs_path = jpeg_abs_path

            thumb_filename = f"thumb_{filename}"
            thumb_abs_path = os.path.join(user_dir, thumb_filename)
            with Image.open(abs_path) as img:
                img.convert("RGB").thumbnail((200, 200))
                img.save(thumb_abs_path, "JPEG", quality=85)
            thumb_rel_path = os.path.join("diary", str(user_id), thumb_filename)
        except HTTPException:
            raise
        except Exception:
            if ext in CONVERT_TO_JPEG_EXT or ext in RAW_EXT:
                # heic/dng 변환 자체에 실패하면 볼 수 없는 파일만 남으므로 업로드를 통째로 실패시킴
                if os.path.exists(abs_path):
                    os.remove(abs_path)
                raise HTTPException(
                    400,
                    f"{ext} 파일을 처리하지 못했습니다. 파일이 손상되었거나 지원하지 않는 형식일 수 있습니다",
                )
            # jpg/png 등 원본 형식은 썸네일 생성만 실패해도 업로드 자체는 유지
            thumb_rel_path = None

    rel_path = os.path.join("diary", str(user_id), filename)

    return DiaryMedia(
        media_type=media_type,
        file_path=rel_path,
        thumbnail_path=thumb_rel_path,
        original_filename=upload.filename,
    )


@router.post("/entries", status_code=201)
def create_entry(
    title: Optional[str] = Form(None),
    content: str = Form(...),
    mood_emoji: Optional[str] = Form(None),
    entry_date: date_cls = Form(...),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    if not content.strip():
        raise HTTPException(400, "내용을 입력해주세요")

    entry = DiaryEntry(
        user_id=current_user.id,
        title=title,
        content=content,
        mood_emoji=mood_emoji,
        entry_date=entry_date,
    )
    db.add(entry)
    db.flush()  # entry.id 확보

    weather = fetch_weather(entry_date)
    if weather:
        entry.weather_code = weather["code"]
        entry.weather_temp_max = weather["temp_max"]
        entry.weather_temp_min = weather["temp_min"]

    for f in files:
        if f and f.filename:
            media = _save_media_file(f, current_user.id)
            media.diary_entry_id = entry.id
            db.add(media)

    entry_count = db.query(DiaryEntry).filter(DiaryEntry.user_id == current_user.id).count()
    entry.ai_comment = generate_ai_comment(content, current_user.full_name, entry_count)

    db.commit()
    db.refresh(entry)
    return _serialize_entry(entry, current_user, db)


@router.put("/entries/{entry_id}")
def update_entry(
    entry_id: int,
    title: Optional[str] = Form(None),
    content: str = Form(...),
    mood_emoji: Optional[str] = Form(None),
    entry_date: date_cls = Form(...),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    entry = (
        db.query(DiaryEntry)
        .filter(DiaryEntry.id == entry_id, DiaryEntry.user_id == current_user.id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "일기를 찾을 수 없습니다")
    if not content.strip():
        raise HTTPException(400, "내용을 입력해주세요")

    entry.title = title
    entry.content = content
    entry.mood_emoji = mood_emoji
    if entry.entry_date != entry_date:
        # 날짜가 바뀐 경우에만 날씨 재조회 (같은 날짜면 굳이 다시 안 부름)
        weather = fetch_weather(entry_date)
        if weather:
            entry.weather_code = weather["code"]
            entry.weather_temp_max = weather["temp_max"]
            entry.weather_temp_min = weather["temp_min"]
    entry.entry_date = entry_date

    for f in files:
        if f and f.filename:
            media = _save_media_file(f, current_user.id)
            media.diary_entry_id = entry.id
            db.add(media)

    entry_count = db.query(DiaryEntry).filter(DiaryEntry.user_id == current_user.id).count()
    entry.ai_comment = generate_ai_comment(content, current_user.full_name, entry_count)

    db.commit()
    db.refresh(entry)
    return _serialize_entry(entry, current_user, db)


@router.delete("/entries/{entry_id}")
def delete_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    entry = (
        db.query(DiaryEntry)
        .filter(DiaryEntry.id == entry_id, DiaryEntry.user_id == current_user.id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "일기를 찾을 수 없습니다")

    # 실제 파일도 함께 삭제
    for m in entry.media:
        for p in (m.file_path, m.thumbnail_path):
            if p:
                abs_p = os.path.join(UPLOAD_DIR, p)
                if os.path.exists(abs_p):
                    os.remove(abs_p)

    db.delete(entry)
    db.commit()
    return {"message": "일기가 삭제되었습니다"}


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다")
    return current_user


@router.post("/admin/backfill-weather")
def backfill_weather(
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """관리자 전용: 모든 사용자의 일기 날씨를 현재 설정된 좌표(WEATHER_LAT/WEATHER_LON)
    기준으로 다시 조회해서 갱신. 날씨 기준 위치를 바꾼 뒤(예: 군포 -> 서울) 그 전에
    이미 저장돼 있던 옛날 일기들의 날씨 값도 새 위치 기준으로 맞추고 싶을 때 한 번 호출."""
    entries = db.query(DiaryEntry).all()
    updated = 0
    for entry in entries:
        weather = fetch_weather(entry.entry_date)
        if weather:
            entry.weather_code = weather["code"]
            entry.weather_temp_max = weather["temp_max"]
            entry.weather_temp_min = weather["temp_min"]
            updated += 1
    db.commit()
    return {"message": f"{updated}개 일기의 날씨를 갱신했습니다", "total": len(entries), "updated": updated}


@router.get("/admin/users/{user_id}/entries")
def admin_list_entries(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """관리자 전용: PIN 없이 특정 사용자의 일기 전체를 조회."""
    entries = (
        db.query(DiaryEntry)
        .filter(DiaryEntry.user_id == user_id)
        .order_by(DiaryEntry.entry_date.desc(), DiaryEntry.id.desc())
        .all()
    )
    return [_serialize_entry(e, admin_user, db) for e in entries]


@router.get("/admin/users/{user_id}/entries/{entry_id}")
def admin_get_entry(
    user_id: int,
    entry_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """관리자 전용: PIN 없이 특정 사용자의 일기 단건 조회."""
    entry = (
        db.query(DiaryEntry)
        .filter(DiaryEntry.id == entry_id, DiaryEntry.user_id == user_id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "일기를 찾을 수 없습니다")
    return _serialize_entry(entry, admin_user, db)


@router.delete("/entries/{entry_id}/media/{media_id}")
def delete_media(
    entry_id: int,
    media_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    media = (
        db.query(DiaryMedia)
        .join(DiaryEntry)
        .filter(
            DiaryMedia.id == media_id,
            DiaryMedia.diary_entry_id == entry_id,
            DiaryEntry.user_id == current_user.id,
        )
        .first()
    )
    if not media:
        raise HTTPException(404, "첨부파일을 찾을 수 없습니다")

    for p in (media.file_path, media.thumbnail_path):
        if p:
            abs_p = os.path.join(UPLOAD_DIR, p)
            if os.path.exists(abs_p):
                os.remove(abs_p)

    db.delete(media)
    db.commit()
    return {"message": "첨부파일이 삭제되었습니다"}


# ---------------------------------------------------------------------------
# 댓글 (일기 항목에 남기는 응원/답글)
# ---------------------------------------------------------------------------
class CommentCreate(BaseModel):
    content: str


def _get_entry_for_comment(entry_id: int, db: Session, current_user: User) -> DiaryEntry:
    """본인 소유 일기이거나(PIN 통과 후), 관리자면 어떤 사용자의 일기든 접근 가능."""
    q = db.query(DiaryEntry).filter(DiaryEntry.id == entry_id)
    if current_user.role != "admin":
        q = q.filter(DiaryEntry.user_id == current_user.id)
    entry = q.first()
    if not entry:
        raise HTTPException(404, "일기를 찾을 수 없습니다")
    return entry


@router.post("/entries/{entry_id}/comments", status_code=201)
def add_comment(
    entry_id: int,
    data: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    """본인 일기에 댓글 작성. 관리자는 require_diary_access를 PIN 없이 통과하지만,
    자기 소유가 아닌 일기에는 이 엔드포인트로 댓글을 달 수 없음(아래 admin 전용 엔드포인트 사용)."""
    entry = _get_entry_for_comment(entry_id, db, current_user)
    if not data.content.strip():
        raise HTTPException(400, "댓글 내용을 입력해주세요")
    comment = DiaryComment(diary_entry_id=entry.id, user_id=current_user.id, content=data.content.strip())
    db.add(comment)
    db.commit()
    db.refresh(entry)
    return _serialize_entry(entry, current_user, db)


@router.delete("/entries/{entry_id}/comments/{comment_id}")
def delete_comment(
    entry_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_diary_access),
):
    comment = (
        db.query(DiaryComment)
        .filter(DiaryComment.id == comment_id, DiaryComment.diary_entry_id == entry_id)
        .first()
    )
    if not comment:
        raise HTTPException(404, "댓글을 찾을 수 없습니다")
    # 댓글 작성자 본인이거나 관리자만 삭제 가능
    if comment.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(403, "본인이 작성한 댓글만 삭제할 수 있습니다")
    db.delete(comment)
    db.commit()
    return {"message": "댓글이 삭제되었습니다"}


@router.post("/admin/users/{user_id}/entries/{entry_id}/comments", status_code=201)
def admin_add_comment(
    user_id: int,
    entry_id: int,
    data: CommentCreate,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """관리자 전용: PIN 없이 다른 사용자의 일기에 댓글(응원/답글) 작성."""
    entry = (
        db.query(DiaryEntry)
        .filter(DiaryEntry.id == entry_id, DiaryEntry.user_id == user_id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "일기를 찾을 수 없습니다")
    if not data.content.strip():
        raise HTTPException(400, "댓글 내용을 입력해주세요")
    comment = DiaryComment(diary_entry_id=entry.id, user_id=admin_user.id, content=data.content.strip())
    db.add(comment)
    db.commit()
    db.refresh(entry)
    return _serialize_entry(entry, admin_user, db)
