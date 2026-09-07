#!/bin/bash
# /root/contigoway/cleanup_junk.sh
# 매일 새벽 3시 cron으로 실행. 저장소 루트에 쌓이는 임시/찌꺼기 파일을 찾아서
# 삭제 + git commit + push까지 자동으로 처리한다.
# 하위 폴더(frontend/ 등)는 건드리지 않고 저장소 최상위 폴더만 스캔한다.

set -e
cd /root/contigoway || exit 1

LOG=/var/log/contigoway_cleanup.log
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 정리 시작 =====" >> "$LOG"

git pull origin main --no-rebase --no-edit >> "$LOG" 2>&1

FOUND=$(find . -maxdepth 1 -type f \( \
    -name "*.patch" -o \
    -name "*.orig" -o \
    -name "*.rej" -o \
    -name "*.tar" -o \
    -name "*.tar.gz" -o \
    -name "*.zip" \
  \))

if [ -n "$FOUND" ]; then
    echo "삭제 대상:" >> "$LOG"
    echo "$FOUND" >> "$LOG"
    echo "$FOUND" | xargs rm -f
    git add -A
    git commit -m "chore: auto-remove junk files ($(date +%Y%m%d_%H%M%S))" >> "$LOG" 2>&1
    if git push origin main >> "$LOG" 2>&1; then
        echo "push 성공" >> "$LOG"
    else
        echo "push 실패 - 다음 실행 때 재시도됨 (git pull이 먼저 돌기 때문)" >> "$LOG"
    fi
else
    echo "삭제할 파일 없음" >> "$LOG"
fi

echo "===== 정리 종료 =====" >> "$LOG"
