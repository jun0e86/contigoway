#!/bin/bash
set -e
cd /root/contigoway
git add -A
git commit -m "update $(date +%Y%m%d_%H%M%S)" || echo "커밋할 변경사항 없음"
git push origin main
docker compose up -d --build
echo "완료: GitHub 반영 + 서버 재기동"
