"""
사용법:
  1) /root/contigoway 에서:  python3 apply_flight_status_patch.py --backend
  2) /var/www/contigoway 에서: python3 apply_flight_status_patch.py --frontend

--backend: main.py에 flights 라우터 등록
--frontend: index.html에 실시간 운항 상태 카드 + JS 추가 (삿포로 패치가 이미 적용된 상태를 전제로 함)

정확한 문자열 매칭으로만 수정, 실패 시 경고만 출력하고 해당 파일은 건드리지 않음.
"""

import argparse
from pathlib import Path

# ── main.py (backend) ──
MAIN_IMPORT_OLD = (
    "from google_auth import router as google_auth_router\n"
    "from scheduler import start_scheduler\n"
)
MAIN_IMPORT_NEW = (
    "from google_auth import router as google_auth_router\n"
    "from scheduler import start_scheduler\n"
    "from flights import router as flights_router\n"
)

MAIN_INCLUDE_OLD = "app.include_router(google_auth_router)\n"
MAIN_INCLUDE_NEW = (
    "app.include_router(google_auth_router)\n"
    "app.include_router(flights_router)\n"
)

# ── index.html (frontend) ──
HTML_OLD = """         네이버 항공권에서 요금 비교 →
      </a>
    </div>
  </div>

  <div style="margin-top:28px">
    <div class="plan-header"><h2>인천 → 신치토세 이동</h2></div>"""

HTML_NEW = """         네이버 항공권에서 요금 비교 →
      </a>
    </div>
  </div>

  <div class="tile-grid" style="margin-top:10px;grid-template-columns:1fr">
    <div class="tile" style="background:#F5F5F7">
      <div class="tile-kicker">인천 → 신치토세 오늘의 실시간 운항 상태</div>
      <div class="tile-sub" style="margin-top:6px">여행 당일 기준 정보예요(실시간 API 특성상 미래 날짜 스케줄은 안 나와요). 참고용으로 봐주세요.</div>
      <div id="flightStatusList" style="margin-top:10px;font-size:.82rem;color:var(--gray)">불러오는 중...</div>
    </div>
  </div>

  <div style="margin-top:28px">
    <div class="plan-header"><h2>인천 → 신치토세 이동</h2></div>"""

JS_OLD = """(async function loadFxRate(){
  try{
    const r = await fetch('https://api.frankfurter.app/latest?from=JPY&to=KRW');
    const d = await r.json();
    const rate = d.rates.KRW;
    document.getElementById('fxRate').textContent = '100엔 = ' + Math.round(rate*100).toLocaleString() + '원';
    document.getElementById('fxUpdated').textContent = d.date + ' 기준 (ECB)';
  }catch(e){
    document.getElementById('fxUpdated').textContent = '환율 정보를 불러오지 못했어요';
  }
})();"""

JS_NEW = """(async function loadFxRate(){
  try{
    const r = await fetch('https://api.frankfurter.app/latest?from=JPY&to=KRW');
    const d = await r.json();
    const rate = d.rates.KRW;
    document.getElementById('fxRate').textContent = '100엔 = ' + Math.round(rate*100).toLocaleString() + '원';
    document.getElementById('fxUpdated').textContent = d.date + ' 기준 (ECB)';
  }catch(e){
    document.getElementById('fxUpdated').textContent = '환율 정보를 불러오지 못했어요';
  }
})();

// ── 인천→신치토세 실시간 운항 상태 (백엔드가 aviationstack 프록시) ──
async function loadFlightStatus(){
  const box = document.getElementById('flightStatusList');
  try{
    const r = await authFetch('/flights/sapporo-status');
    const d = await r.json();
    if(!d.flights || !d.flights.length){
      box.textContent = '오늘 조회되는 인천→신치토세 운항 정보가 없어요.';
      return;
    }
    box.innerHTML = d.flights.map(f => `
      <div style="padding:6px 0;border-bottom:1px solid var(--line)">
        <b>${f.airline || ''} ${f.flight_number || ''}</b> · ${f.status || '-'}
        <div style="font-size:.75rem">
          출발예정 ${f.dep_scheduled ? f.dep_scheduled.replace('T',' ').slice(0,16) : '-'}
          → 도착예정 ${f.arr_scheduled ? f.arr_scheduled.replace('T',' ').slice(0,16) : '-'}
        </div>
      </div>`).join('');
  }catch(e){
    box.textContent = '운항 정보를 불러오지 못했어요.';
  }
}
loadFlightStatus();"""


def patch_file(path: Path, patches, label_prefix: str):
    if not path.exists():
        print(f"[스킵] {path} 없음")
        return
    text = path.read_text(encoding="utf-8")
    original = text
    changed = False
    for label, old, new in patches:
        if new in text:
            print(f"[스킵] {label_prefix} - {label}: 이미 적용됨")
            continue
        if old not in text:
            print(f"[경고] {label_prefix} - {label}: 매칭 실패")
            continue
        text = text.replace(old, new, 1)
        changed = True
        print(f"[완료] {label_prefix} - {label}")
    if changed:
        path.with_suffix(path.suffix + ".bak2").write_text(original, encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        print(f"저장 완료: {path} (백업: {path.name}.bak2)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", action="store_true", help="main.py 패치 (/root/contigoway 에서 실행)")
    parser.add_argument("--frontend", action="store_true", help="index.html 패치 (/var/www/contigoway 에서 실행)")
    args = parser.parse_args()

    if args.backend:
        patch_file(
            Path("main.py"),
            [
                ("import 추가", MAIN_IMPORT_OLD, MAIN_IMPORT_NEW),
                ("router 등록", MAIN_INCLUDE_OLD, MAIN_INCLUDE_NEW),
            ],
            "main.py",
        )
    elif args.frontend:
        patch_file(
            Path("index.html"),
            [
                ("실시간 운항 카드 + JS", HTML_OLD, HTML_NEW),
                ("loadFlightStatus JS", JS_OLD, JS_NEW),
            ],
            "index.html",
        )
    else:
        print("사용법: --backend 또는 --frontend 옵션을 지정하세요.")


if __name__ == "__main__":
    main()
