"""
서버에서 실행:
    cd /root/contigoway && python3 apply_ann_patches.py     # models.py 패치 (여기서 실행)
    cd /var/www/contigoway && python3 /root/contigoway/apply_ann_patches.py --html-only  # html 패치 (여기서 실행)

정확한 문자열 매칭으로만 수정하고, 매칭 실패 시 그 파일은 건드리지 않고 경고만 출력한다.
원본은 각각 .bak으로 백업.
"""

import argparse
from pathlib import Path

# ── models.py 패치 (실행 위치: /root/contigoway) ──
MODELS_PATCH = (
    "class Announcement(Base):\n"
    '    """로그인 전/후 화면에 뜨는 공지 팝업. 한 번에 하나만 활성화됨."""\n'
    "\n"
    '    __tablename__ = "announcements"\n'
    "\n"
    "    id = Column(Integer, primary_key=True, index=True)\n"
    "    title = Column(String(200), nullable=False)\n"
    "    content = Column(Text, nullable=False)\n"
    "    is_active = Column(Boolean, nullable=False, default=True)\n"
    '    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)\n'
    "    created_at = Column(DateTime(timezone=True), server_default=func.now())\n"
    "\n"
    '    author = relationship("User")',

    "class Announcement(Base):\n"
    '    """로그인 전/후 화면에 뜨는 공지 팝업. start_at~end_at 기간과 is_active를 함께 만족해야 노출됨."""\n'
    "\n"
    '    __tablename__ = "announcements"\n'
    "\n"
    "    id = Column(Integer, primary_key=True, index=True)\n"
    "    title = Column(String(200), nullable=False)\n"
    "    content = Column(Text, nullable=False)\n"
    "    is_active = Column(Boolean, nullable=False, default=True)\n"
    "    start_at = Column(Date, nullable=True)\n"
    "    end_at = Column(Date, nullable=True)\n"
    '    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)\n'
    "    created_at = Column(DateTime(timezone=True), server_default=func.now())\n"
    "    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)\n"
    "\n"
    '    author = relationship("User")',
)

# ── index.html 패치 (실행 위치: /var/www/contigoway) ──
INDEX_HTML_OLD = """async function loadAnnAdminPanel(){
  const box = document.getElementById('annAdminPanel');
  let current = null;
  try{
    const r = await authFetch('/announcements/active');
    current = await r.json();
  }catch(e){}

  box.innerHTML = `
    <div class="ann-admin-panel">
      <h4>📢 공지 팝업 관리</h4>
      ${current ? `
        <div class="row" style="margin-bottom:10px">
          <span class="current">현재 게시 중: <b>${current.title}</b></span>
          <span class="hide-btn" onclick="hideAnnouncement(${current.id})">숨기기</span>
        </div>` : `<div class="current" style="margin-bottom:10px">현재 게시 중인 공지 없음</div>`}
      <input type="text" id="annNewTitle" placeholder="제목">
      <textarea id="annNewContent" placeholder="내용"></textarea>
      <div class="row"><span></span><button onclick="postAnnouncement()">게시하기</button></div>
    </div>
  `;
}

async function postAnnouncement(){
  const title = document.getElementById('annNewTitle').value.trim();
  const content = document.getElementById('annNewContent').value.trim();
  if(!title || !content){ alert('제목과 내용을 입력해주세요'); return; }
  const r = await authFetch('/announcements', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({title, content})
  });
  if(!r.ok){ alert('게시 실패'); return; }
  await loadAnnAdminPanel();
  alert('공지가 게시되었습니다. 다음 방문(또는 새로고침) 시 팝업으로 보여요.');
}

async function hideAnnouncement(id){
  if(!confirm('이 공지를 숨길까요?')) return;
  await authFetch('/announcements/'+id+'/deactivate', {method:'POST'});
  loadAnnAdminPanel();
}"""

INDEX_HTML_NEW = """let annEditingId = null;

async function loadAnnAdminPanel(){
  const box = document.getElementById('annAdminPanel');
  let current = null;
  let allAnns = [];
  try{
    const r = await authFetch('/announcements/active');
    current = await r.json();
  }catch(e){}
  try{
    const r2 = await authFetch('/announcements');
    allAnns = await r2.json();
  }catch(e){}
  window._annAdminCache = allAnns;

  box.innerHTML = `
    <div class="ann-admin-panel">
      <h4>📢 공지 팝업 관리</h4>
      ${current ? `
        <div class="row" style="margin-bottom:10px">
          <span class="current">현재 노출 중: <b>${current.title}</b></span>
          <span class="hide-btn" onclick="hideAnnouncement(${current.id})">숨기기</span>
        </div>` : `<div class="current" style="margin-bottom:10px">현재 노출 중인 공지 없음</div>`}

      <input type="text" id="annNewTitle" placeholder="제목">
      <textarea id="annNewContent" placeholder="내용"></textarea>
      <div class="row" style="gap:8px;align-items:center;justify-content:flex-start">
        <label style="font-size:.78rem;color:var(--gray)">노출 시작일
          <input type="date" id="annNewStart" style="margin-left:4px">
        </label>
        <label style="font-size:.78rem;color:var(--gray)">노출 종료일
          <input type="date" id="annNewEnd" style="margin-left:4px">
        </label>
      </div>
      <div style="font-size:.72rem;color:var(--gray);margin:-4px 0 8px">기간을 비워두면 숨기기 전까지 계속 노출됩니다.</div>
      <div class="row">
        <span id="annFormModeLabel" style="font-size:.78rem;color:var(--gray)"></span>
        <span>
          <button id="annCancelEditBtn" type="button" style="display:none;background:#fff;color:var(--black);border:1px solid var(--line);margin-right:6px" onclick="cancelAnnEdit()">취소</button>
          <button id="annSubmitBtn" type="button" onclick="submitAnnouncement()">게시하기</button>
        </span>
      </div>

      <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
        <h4 style="margin-bottom:10px">전체 공지 이력</h4>
        ${allAnns.length ? allAnns.map(a => `
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:.85rem">${a.title} ${a.is_active ? '<span style="color:#0C7A3D;font-size:.72rem">(활성)</span>' : '<span style="color:var(--gray-light);font-size:.72rem">(숨김)</span>'}</div>
              <div style="font-size:.75rem;color:var(--gray);margin-top:2px">${a.start_at || '제한없음'} ~ ${a.end_at || '제한없음'}</div>
            </div>
            <div style="display:flex;gap:6px;flex-shrink:0">
              <span class="hide-btn" style="color:var(--blue);border-color:#C9E2FF;background:#F0F7FF" onclick="startAnnEdit(${a.id})">수정</span>
              ${a.is_active
                ? `<span class="hide-btn" onclick="hideAnnouncement(${a.id})">숨기기</span>`
                : `<span class="hide-btn" style="color:#0C7A3D;border-color:#C8E6D5;background:#EFFAF3" onclick="reactivateAnnouncement(${a.id})">다시게시</span>`}
              <span class="hide-btn" onclick="deleteAnnouncement(${a.id})">삭제</span>
            </div>
          </div>
        `).join('') : '<div class="current">등록된 공지가 없습니다</div>'}
      </div>
    </div>
  `;
}

function startAnnEdit(id){
  const a = (window._annAdminCache || []).find(x => x.id === id);
  if(!a) return;
  annEditingId = id;
  document.getElementById('annNewTitle').value = a.title;
  document.getElementById('annNewContent').value = a.content;
  document.getElementById('annNewStart').value = a.start_at || '';
  document.getElementById('annNewEnd').value = a.end_at || '';
  document.getElementById('annFormModeLabel').textContent = '공지 수정 중';
  document.getElementById('annSubmitBtn').textContent = '수정 완료';
  document.getElementById('annCancelEditBtn').style.display = 'inline-block';
  document.getElementById('annNewTitle').scrollIntoView({behavior:'smooth', block:'center'});
}

function cancelAnnEdit(){
  annEditingId = null;
  loadAnnAdminPanel();
}

async function submitAnnouncement(){
  const title = document.getElementById('annNewTitle').value.trim();
  const content = document.getElementById('annNewContent').value.trim();
  const start_at = document.getElementById('annNewStart').value || null;
  const end_at = document.getElementById('annNewEnd').value || null;
  if(!title || !content){ alert('제목과 내용을 입력해주세요'); return; }
  if(start_at && end_at && start_at > end_at){ alert('시작일이 종료일보다 늦을 수 없습니다'); return; }

  const body = JSON.stringify({title, content, start_at, end_at});
  let r;
  if(annEditingId){
    r = await authFetch('/announcements/'+annEditingId, {
      method:'PUT', headers:{'Content-Type':'application/json'}, body
    });
  } else {
    r = await authFetch('/announcements', {
      method:'POST', headers:{'Content-Type':'application/json'}, body
    });
  }
  if(!r.ok){ alert('저장 실패'); return; }
  const wasEdit = !!annEditingId;
  annEditingId = null;
  await loadAnnAdminPanel();
  alert(wasEdit ? '공지가 수정되었습니다.' : '공지가 게시되었습니다. 노출 기간 내에 팝업으로 보여요.');
}

async function hideAnnouncement(id){
  if(!confirm('이 공지를 숨길까요?')) return;
  await authFetch('/announcements/'+id+'/deactivate', {method:'POST'});
  loadAnnAdminPanel();
}

async function reactivateAnnouncement(id){
  if(!confirm('이 공지를 다시 게시할까요?')) return;
  await authFetch('/announcements/'+id+'/activate', {method:'POST'});
  loadAnnAdminPanel();
}

async function deleteAnnouncement(id){
  if(!confirm('이 공지를 완전히 삭제할까요? 되돌릴 수 없습니다.')) return;
  await authFetch('/announcements/'+id, {method:'DELETE'});
  loadAnnAdminPanel();
}"""

# ── todo.html / diary.html / schedule.html 공통 패치 (실행 위치: /var/www/contigoway) ──
NAV_LINK_OLD = '    <div class="nav-links">\n'
NAV_LINK_NEW = (
    '    <div class="nav-links">\n'
    '      <a href="/index.html" id="annAdminToggle" style="display:none">📢 공지관리</a>\n'
)

GREET_OLD = "    document.getElementById('greetName').textContent = me.full_name + '님';\n"
GREET_NEW = (
    "    document.getElementById('greetName').textContent = me.full_name + '님';\n"
    "    if(me.role === 'admin'){\n"
    "      const annToggle = document.getElementById('annAdminToggle');\n"
    "      if(annToggle) annToggle.style.display = 'inline';\n"
    "    }\n"
)


def patch_file(path: Path, patches, label: str):
    if not path.exists():
        print(f"[스킵] {path} 없음")
        return
    text = path.read_text(encoding="utf-8")
    original = text
    changed = False
    for old, new in patches:
        if new in text:
            continue
        if old not in text:
            print(f"[경고] {path.name}: {label} 매칭 실패 (이미 손대신 파일이거나 내용이 다릅니다)")
            continue
        text = text.replace(old, new, 1)
        changed = True
    if changed:
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        print(f"[완료] {path} 패치 적용됨 (백업: {path.name}.bak)")
    else:
        print(f"[변경 없음] {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html-only", action="store_true", help="html 파일만 패치 (현재 폴더가 /var/www/contigoway일 때)")
    parser.add_argument("--models-only", action="store_true", help="models.py만 패치 (현재 폴더가 /root/contigoway일 때)")
    args = parser.parse_args()

    if args.models_only:
        patch_file(Path("models.py"), [MODELS_PATCH], "Announcement 컬럼 추가")
        return

    if args.html_only:
        patch_file(Path("index.html"), [(INDEX_HTML_OLD, INDEX_HTML_NEW)], "공지관리 패널 확장")
        for fname in ["todo.html", "diary.html", "schedule.html"]:
            patch_file(Path(fname), [(NAV_LINK_OLD, NAV_LINK_NEW), (GREET_OLD, GREET_NEW)], "공지관리 nav 버튼 추가")
        return

    print("사용법: --models-only 또는 --html-only 옵션을 지정하세요.")


if __name__ == "__main__":
    main()
