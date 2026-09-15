# contigoway ↔ Google Calendar 양방향 동기화

기존 "URL로 구독(ICS)" 방식을 걷어내고, Google Calendar API 기반 양방향 연동으로 교체하는 구현입니다.
아이폰은 별도 작업 없이 **아이폰 설정 > 캘린더 > 계정 추가 > Google**로 같은 구글 계정을 등록하면
애플 ↔ 구글 동기화는 구글이 알아서 처리해줍니다. 따라서 contigoway는 구글 하나만 양방향으로 연결하면 됩니다.

## 1. 설치

```bash
pip install -r requirements_addition.txt
```

## 2. Google Cloud Console 설정

1. https://console.cloud.google.com 에서 프로젝트 생성
2. "API 및 서비스 > 라이브러리"에서 **Google Calendar API** 사용 설정
3. "OAuth 동의 화면" 구성 (User Type: 외부, 앱 이름/이메일 등 입력. 처음엔 "테스트" 상태로도 본인 계정은 바로 사용 가능)
4. "사용자 인증 정보 > OAuth 클라이언트 ID 만들기" (유형: 웹 애플리케이션)
   - 승인된 리디렉션 URI: `https://contigoway.com/api/google/callback`
5. 발급된 클라이언트 ID / 보안 비밀번호를 아래 `.env`에 저장

## 3. 환경변수 (.env)

```
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxx
GOOGLE_REDIRECT_URI=https://contigoway.com/api/google/callback
FERNET_KEY=xxxxx   # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 4. DB 마이그레이션

`migration_add_columns.py`의 upgrade() 내용을 alembic revision 파일에 옮겨서 실행:

```bash
alembic upgrade head
```

## 5. 파일 배치

이 폴더(google_sync/)를 기존 FastAPI 프로젝트 안에 그대로 넣고, import 경로만
프로젝트 구조(`.database`, `.auth_deps`, `.models` 등)에 맞게 수정하세요.
`models_addition.py`, `sync_service.py`, `scheduler.py` 안의 `Schedule` import 부분은
실제 기존 Schedule 모델 경로로 바꿔야 동작합니다.

## 6. main.py 연결

```python
from .google_sync.google_auth import router as google_auth_router
from .google_sync.scheduler import start_scheduler

app.include_router(google_auth_router)

@app.on_event("startup")
def on_startup():
    start_scheduler()
```

## 7. 기존 일정 CRUD에 연결

`router_schedule_integration_example.py` 참고 — 생성/수정/삭제 로직 맨 끝에
`push_to_google(db, current_user.id, schedule, action=...)` 한 줄씩만 추가하면 됩니다.

## 8. 프론트엔드 변경

- 설정 화면에 "구글 캘린더 연동하기" 버튼 → `GET /api/google/authorize`로 이동
- 기존 "캘린더 구독(ICS)" UI/API는 제거하거나, 미연동 사용자를 위한 읽기 전용 백업 옵션으로만 남겨도 됨
- 연동 완료 후 사용자에게 아이폰 안내 문구 노출:
  > "아이폰 기본 캘린더 앱에서도 보고 싶다면, [설정 > 캘린더 > 계정 추가 > Google]에서
  > 같은 구글 계정을 등록해주세요. 기존에 등록해두신 '캘린더 구독' 링크는 삭제하셔도 됩니다."

## 9. 동작 흐름 정리

- **contigoway → Google**: 일정 생성/수정/삭제 즉시 API 호출로 반영 (실시간)
- **Google → contigoway**: 5분 주기 폴링(syncToken) — 아이폰/구글 캘린더에서 만든 일정이 최대 5분 내 contigoway에 반영
- **충돌**: 동시 수정 시 나중에 반영되는 쪽이 이김 (last-write-wins). 필요하면 `last_modified_source` 컬럼으로 어느 쪽이 최종 반영인지 추적 가능
- **무한 루프 방지**: contigoway가 만든 이벤트는 `extendedProperties.private.contigoway_id`로 표시해두고, pull 시 이 값이 있으면 스킵

## 10. 추후 개선 (선택)

- 폴링 대신 `events.watch` push notification으로 전환하면 5분 지연 없이 즉시 반영 가능
  (단, 도메인 HTTPS 검증 필요 + watch 채널이 최대 7일마다 만료되어 자동 갱신 잡 추가로 필요)
- 사용자 수가 늘어나면 폴링 잡을 Celery/RQ 등 워커 큐로 분리
