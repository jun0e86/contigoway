"""
photos.py

사진모음 API. diary.py의 미디어 업로드(HEIC/HEIF/DNG -> JPEG 변환, 썸네일)와
날씨 조회(Open-Meteo) 로직을 그대로 재사용하고, 여기에 추가로:
  - exiftool로 촬영일시(taken_at)와 GPS 좌표를 추출
  - GPS 좌표 -> 장소명 변환 (OpenStreetMap Nominatim, 무료/키 불필요)
  - 촬영 좌표·날짜 기준 실제 날씨 조회 (diary.py와 달리 매번 그 사진의 좌표를 씀)
  - 업로드 시 사용자가 직접 입력하는 occasion(태그/메모) 저장

지원 포맷은 diary.py와 동일: jpg/jpeg/png/heic/heif/dng(이미지), mov(동영상).
exiftool 설치 필요: apt-get install libimage-exiftool-perl (Dockerfile에 추가됨).
"""

import json
import os
import subprocess
import uuid
from datetime import datetime, date as date_cls
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Photo, PhotoComment, User
from auth import get_current_user

router = APIRouter(prefix="/photos", tags=["사진모음"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/app/uploads")
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".dng"}
CONVERT_TO_JPEG_EXT = {".heic", ".heif"}
RAW_EXT = {".dng"}
ALLOWED_VIDEO_EXT = {".mov"}
MAX_FILE_SIZE_MB = int(os.environ.get("PHOTOS_MAX_FILE_SIZE_MB", "300"))  # 동영상 고려해 diary보다 넉넉하게

# Nominatim 사용 정책상 식별 가능한 User-Agent가 필요 (이메일 등 개인정보는 환경변수로만, 코드에 하드코딩하지 않음)
NOMINATIM_CONTACT = os.environ.get("NOMINATIM_CONTACT", "contigoway-app")

# 좌표를 못 찾았을 때 날씨 조회에 쓸 기본 위치 (diary.py와 동일 기본값: 서울시청)
DEFAULT_WEATHER_LAT = float(os.environ.get("DIARY_WEATHER_LAT", "37.5665"))
DEFAULT_WEATHER_LON = float(os.environ.get("DIARY_WEATHER_LON", "126.9780"))


# ── 메타데이터(촬영일시/GPS) 추출 ────────────────────────────────
def _extract_metadata(abs_path: str) -> dict:
    """exiftool로 촬영일시/GPS를 뽑는다. 실패하거나 정보가 없으면 빈 값들을 반환."""
    result = {"taken_at": None, "latitude": None, "longitude": None}
    try:
        proc = subprocess.run(
            [
                "exiftool", "-j", "-n",
                "-DateTimeOriginal", "-CreateDate",
                "-GPSLatitude", "-GPSLongitude",
                abs_path,
            ],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(proc.stdout)[0] if proc.stdout.strip() else {}
    except Exception:
        return result

    date_str = data.get("DateTimeOriginal") or data.get("CreateDate")
    if date_str:
        try:
            # exiftool 날짜 형식: "2024:07:15 10:23:45" (타임존 정보는 생략된 경우가 많아 로컬시각으로 취급)
            result["taken_at"] = datetime.strptime(date_str.split("+")[0].split(".")[0].strip(), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass

    lat, lon = data.get("GPSLatitude"), data.get("GPSLongitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        result["latitude"], result["longitude"] = float(lat), float(lon)

    return result


def _reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """GPS 좌표 -> 장소명. 실패하면 None (업로드 자체는 계속 진행).

    검색 기능에서 '노원'(구/동 단위)뿐 아니라 '일본'(국가명)으로도 찾을 수 있도록
    구/동 + 시/도 + 국가까지 이어 붙인다.
    """
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon, "accept-language": "ko", "zoom": 16},
            headers={"User-Agent": f"{NOMINATIM_CONTACT} photo-location-lookup"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        addr = data.get("address", {})
        parts = [
            addr.get("city_district") or addr.get("borough") or addr.get("suburb") or addr.get("town") or addr.get("village"),
            addr.get("city") or addr.get("county"),
            addr.get("state") if addr.get("country") and addr.get("country") != "대한민국" else None,
            addr.get("country"),
        ]
        name = " ".join(dict.fromkeys(p for p in parts if p)) or data.get("display_name")
        return name[:255] if name else None
    except Exception:
        return None


def _fetch_weather(for_date: date_cls, lat: float, lon: float) -> Optional[dict]:
    """diary.py의 fetch_weather와 동일한 API를 쓰되, 사진 자체의 좌표를 사용."""
    try:
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": for_date.isoformat(), "end_date": for_date.isoformat(),
                "daily": "weathercode,temperature_2m_max,temperature_2m_min",
                "timezone": "Asia/Seoul",
            },
            timeout=8,
        )
        resp.raise_for_status()
        daily = resp.json().get("daily", {})
        codes = daily.get("weathercode", [])
        if not codes or codes[0] is None:
            return None
        tmax, tmin = daily.get("temperature_2m_max", []), daily.get("temperature_2m_min", [])
        return {
            "code": int(codes[0]),
            "temp_max": round(tmax[0]) if tmax and tmax[0] is not None else None,
            "temp_min": round(tmin[0]) if tmin and tmin[0] is not None else None,
        }
    except Exception:
        return None


# ── 파일 저장 (diary.py의 _save_media_file과 동일한 변환 로직) ────
def _save_photo_file(upload: UploadFile, user_id: int, occasion: Optional[str], category: Optional[str]) -> Photo:
    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext in ALLOWED_IMAGE_EXT:
        media_type = "image"
    elif ext in ALLOWED_VIDEO_EXT:
        media_type = "video"
    else:
        raise HTTPException(400, f"지원하지 않는 파일 형식입니다: {ext} (jpg/jpeg/png/heic/heif/dng, mov만 가능)")

    user_dir = os.path.join(UPLOAD_DIR, "photos", str(user_id))
    os.makedirs(user_dir, exist_ok=True)

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

    # 메타데이터(촬영일시/GPS)는 변환 전 원본에서 뽑는 게 가장 정확함
    meta = _extract_metadata(temp_abs_path)

    filename, abs_path, thumb_rel_path = temp_filename, temp_abs_path, None

    if media_type == "image":
        try:
            from PIL import Image

            if ext in CONVERT_TO_JPEG_EXT:
                import pillow_heif
                pillow_heif.register_heif_opener()
                jpeg_filename = f"{uuid.uuid4().hex}.jpg"
                jpeg_abs_path = os.path.join(user_dir, jpeg_filename)
                with Image.open(abs_path) as img:
                    img.convert("RGB").save(jpeg_abs_path, "JPEG", quality=92)
                os.remove(abs_path)
                filename, abs_path = jpeg_filename, jpeg_abs_path
            elif ext in RAW_EXT:
                import rawpy
                with rawpy.imread(abs_path) as raw:
                    preview_bytes = raw.extract_thumb().data
                os.remove(abs_path)
                jpeg_filename = f"{uuid.uuid4().hex}.jpg"
                jpeg_abs_path = os.path.join(user_dir, jpeg_filename)
                with open(jpeg_abs_path, "wb") as f:
                    f.write(preview_bytes)
                filename, abs_path = jpeg_filename, jpeg_abs_path

            thumb_filename = f"thumb_{filename}"
            thumb_abs_path = os.path.join(user_dir, thumb_filename)
            with Image.open(abs_path) as img:
                img.convert("RGB").thumbnail((320, 320))
                img.save(thumb_abs_path, "JPEG", quality=85)
            thumb_rel_path = os.path.join("photos", str(user_id), thumb_filename)
        except HTTPException:
            raise
        except Exception:
            if ext in CONVERT_TO_JPEG_EXT or ext in RAW_EXT:
                if os.path.exists(abs_path):
                    os.remove(abs_path)
                raise HTTPException(400, f"{ext} 파일을 처리하지 못했습니다. 파일이 손상되었거나 지원하지 않는 형식일 수 있습니다")
            thumb_rel_path = None

    # 위치/날씨는 GPS가 있을 때만 조회 (없으면 조용히 비워둠 — 업로드 자체는 항상 성공)
    location_name, weather = None, None
    taken_date = (meta["taken_at"] or datetime.utcnow()).date()
    if meta["latitude"] is not None and meta["longitude"] is not None:
        location_name = _reverse_geocode(meta["latitude"], meta["longitude"])
        weather = _fetch_weather(taken_date, meta["latitude"], meta["longitude"])
    else:
        weather = _fetch_weather(taken_date, DEFAULT_WEATHER_LAT, DEFAULT_WEATHER_LON)

    rel_path = os.path.join("photos", str(user_id), filename)
    return Photo(
        uploader_id=user_id,
        media_type=media_type,
        file_path=rel_path,
        thumbnail_path=thumb_rel_path,
        original_filename=upload.filename,
        taken_at=meta["taken_at"],
        latitude=meta["latitude"],
        longitude=meta["longitude"],
        location_name=location_name,
        weather_code=weather["code"] if weather else None,
        weather_temp_max=weather["temp_max"] if weather else None,
        weather_temp_min=weather["temp_min"] if weather else None,
        occasion=(occasion or "").strip()[:500] or None,
        category=(category or "").strip()[:50] or None,
    )


# ── 응답 스키마 ────────────────────────────────────────────────
class CommentOut(BaseModel):
    id: int
    content: str
    created_at: datetime
    author_name: Optional[str] = None
    user_id: int

    class Config:
        from_attributes = True


class PhotoOut(BaseModel):
    id: int
    media_type: str
    file_path: str
    thumbnail_path: Optional[str]
    original_filename: Optional[str]
    taken_at: Optional[datetime]
    latitude: Optional[float]
    longitude: Optional[float]
    location_name: Optional[str]
    weather_code: Optional[int]
    weather_temp_max: Optional[int]
    weather_temp_min: Optional[int]
    occasion: Optional[str]
    category: Optional[str]
    uploader_id: int
    uploader_name: Optional[str] = None
    comments: list[CommentOut] = []

    class Config:
        from_attributes = True


def _to_photo_out(photo: Photo, uploader_names: dict) -> PhotoOut:
    item = PhotoOut.model_validate(photo)
    item.uploader_name = uploader_names.get(photo.uploader_id)
    item.comments = [
        CommentOut(
            id=c.id, content=c.content, created_at=c.created_at,
            user_id=c.user_id, author_name=uploader_names.get(c.user_id),
        )
        for c in sorted(photo.comments, key=lambda c: c.created_at)
    ]
    return item


# ── 엔드포인트 ────────────────────────────────────────────────
@router.post("", status_code=201, response_model=list[PhotoOut])
def upload_photos(
    files: list[UploadFile] = File(...),
    occasion: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not files:
        raise HTTPException(400, "업로드할 파일이 없습니다")
    if len(files) > 30:
        raise HTTPException(400, "한 번에 최대 30개까지 업로드할 수 있습니다")

    saved = []
    for f in files:
        photo = _save_photo_file(f, user.id, occasion, category)
        db.add(photo)
        saved.append(photo)
    db.commit()
    uploader_names = {u.id: u.full_name for u in db.query(User).all()}
    out = []
    for p in saved:
        db.refresh(p)
        out.append(_to_photo_out(p, uploader_names))
    return out


@router.get("/categories", response_model=list[str])
def list_categories(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """업로드에 실제로 쓰인 카테고리 목록(중복 제거, 알파벳/가나다 정렬 아님 - 최신순)."""
    rows = (
        db.query(Photo.category)
        .filter(Photo.category.isnot(None))
        .order_by(Photo.created_at.desc())
        .all()
    )
    seen, out = set(), []
    for (c,) in rows:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


@router.get("", response_model=list[PhotoOut])
def list_photos(
    limit: int = 60,
    offset: int = 0,
    q: Optional[str] = None,        # 장소/태그 검색어 (예: "노원", "일본")
    category: Optional[str] = None,  # 카테고리 필터 (예: "여름휴가")
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    limit = min(max(limit, 1), 200)
    query = db.query(Photo)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter((Photo.location_name.ilike(like)) | (Photo.occasion.ilike(like)))
    if category and category.strip():
        query = query.filter(Photo.category == category.strip())
    rows = (
        query.order_by(Photo.taken_at.desc().nullslast(), Photo.created_at.desc())
        .offset(offset).limit(limit).all()
    )
    uploader_names = {u.id: u.full_name for u in db.query(User).all()}
    return [_to_photo_out(p, uploader_names) for p in rows]


class PhotoUpdate(BaseModel):
    occasion: Optional[str] = None
    category: Optional[str] = None


@router.patch("/{photo_id}", response_model=PhotoOut)
def update_photo(
    photo_id: int, body: PhotoUpdate,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "사진을 찾을 수 없습니다")
    if photo.uploader_id != user.id and user.role != "admin":
        raise HTTPException(403, "본인이 올린 사진만 수정할 수 있습니다")
    if body.occasion is not None:
        photo.occasion = body.occasion.strip()[:500] or None
    if body.category is not None:
        photo.category = body.category.strip()[:50] or None
    db.commit()
    db.refresh(photo)
    uploader_names = {u.id: u.full_name for u in db.query(User).all()}
    return _to_photo_out(photo, uploader_names)


@router.delete("/{photo_id}", status_code=204)
def delete_photo(
    photo_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "사진을 찾을 수 없습니다")
    if photo.uploader_id != user.id and user.role != "admin":
        raise HTTPException(403, "본인이 올린 사진만 삭제할 수 있습니다")
    for rel in (photo.file_path, photo.thumbnail_path):
        if rel:
            abs_path = os.path.join(UPLOAD_DIR, rel)
            if os.path.exists(abs_path):
                os.remove(abs_path)
    db.delete(photo)
    db.commit()
    return None


# ── 댓글 ─────────────────────────────────────────────────────
class CommentIn(BaseModel):
    content: str


def _get_photo_or_404(photo_id: int, db: Session) -> Photo:
    photo = db.query(Photo).filter(Photo.id == photo_id).first()
    if not photo:
        raise HTTPException(404, "사진을 찾을 수 없습니다")
    return photo


@router.post("/{photo_id}/comments", status_code=201, response_model=PhotoOut)
def add_comment(
    photo_id: int, body: CommentIn,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    photo = _get_photo_or_404(photo_id, db)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "댓글 내용을 입력해주세요")
    db.add(PhotoComment(photo_id=photo.id, user_id=user.id, content=content))
    db.commit()
    db.refresh(photo)
    uploader_names = {u.id: u.full_name for u in db.query(User).all()}
    return _to_photo_out(photo, uploader_names)


@router.delete("/{photo_id}/comments/{comment_id}", response_model=PhotoOut)
def delete_comment(
    photo_id: int, comment_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    comment = (
        db.query(PhotoComment)
        .filter(PhotoComment.id == comment_id, PhotoComment.photo_id == photo_id)
        .first()
    )
    if not comment:
        raise HTTPException(404, "댓글을 찾을 수 없습니다")
    if comment.user_id != user.id and user.role != "admin":
        raise HTTPException(403, "본인이 남긴 댓글만 삭제할 수 있습니다")
    db.delete(comment)
    db.commit()
    photo = _get_photo_or_404(photo_id, db)
    uploader_names = {u.id: u.full_name for u in db.query(User).all()}
    return _to_photo_out(photo, uploader_names)
