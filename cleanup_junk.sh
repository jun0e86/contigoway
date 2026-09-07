#!/bin/bash
set -e
cd /root/contigoway || exit 1

LOG=/var/log/contigoway_cleanup.log
MAIL_TO="jun0e86@gmail.com"
SERVER_NAME="contigoway ($(hostname))"
RUN_TIME=$(date '+%Y-%m-%d %H:%M:%S')

read -r USE_PCT_BEFORE AVAIL_BEFORE <<< "$(df -h / | tail -1 | awk '{print $5, $4}')"

git pull origin main --no-rebase --no-edit >> "$LOG" 2>&1

FOUND=$(find . -maxdepth 1 -type f \( \
    -name "*.patch" -o \
    -name "*.orig" -o \
    -name "*.rej" -o \
    -name "*.tar" -o \
    -name "*.tar.gz" -o \
    -name "*.zip" \
  \))

CLEANUP_SECTION=""
if [ -n "$FOUND" ]; then
    FREED=$(echo "$FOUND" | xargs du -ch 2>/dev/null | tail -1 | awk '{print $1}')
    FILE_LIST=$(echo "$FOUND" | sed 's/^/  - /')

    echo "$FOUND" | xargs rm -f
    git add -A
    git commit -m "chore: auto-remove junk files ($(date +%Y%m%d_%H%M%S))" >> "$LOG" 2>&1

    if git push origin main >> "$LOG" 2>&1; then
        PUSH_STATUS="GitHub push 성공"
    else
        PUSH_STATUS="GitHub push 실패 (다음 실행 때 재시도됨)"
    fi

    CLEANUP_SECTION="찌꺼기 파일 정리로 확보된 공간: ${FREED}

${FILE_LIST}

${PUSH_STATUS}"
else
    CLEANUP_SECTION="오늘은 삭제할 찌꺼기 파일이 없었습니다."
fi

read -r USE_PCT_AFTER AVAIL_AFTER <<< "$(df -h / | tail -1 | awk '{print $5, $4}')"

USE_NUM=$(echo "$USE_PCT_AFTER" | tr -d '%')
if [ "$USE_NUM" -ge 85 ]; then
    STATUS_LINE="⚠️ 경고 (디스크 사용률 ${USE_PCT_AFTER}, 확인 필요)"
else
    STATUS_LINE="✅ 정상"
fi

BODY="${SERVER_NAME} 디스크 정리 결과 안내입니다.

실행 시각: ${RUN_TIME}
정리 전 사용률: ${USE_PCT_BEFORE} (여유 ${AVAIL_BEFORE}) -> 정리 후: ${USE_PCT_AFTER} (여유 ${AVAIL_AFTER})
현재 상태: ${STATUS_LINE}

[삭제/정리 내역]
${CLEANUP_SECTION}

상세 로그: ${LOG}"

echo "===== ${RUN_TIME} =====" >> "$LOG"
echo "$BODY" >> "$LOG"

msmtp "$MAIL_TO" << MAIL_EOF
Subject: [contigoway] 디스크 정리 알림 - $(hostname)
To: $MAIL_TO

$BODY
MAIL_EOF
