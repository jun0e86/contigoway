// contigoway 공통 방문자 카운터 (누적/오늘)
// 조회만 합니다. 방문 집계(/site/hit)는 index.html에서만 기록합니다.
(function () {
  // index.html처럼 브레드크럼 안에 이미 카운터가 있으면 건너뜀.
  // 예전 템플릿에서 복사된 빈 카운터(브레드크럼 밖)가 있으면 지우고 새로 붙임.
  var old = document.getElementById('visitCounter');
  if (old) {
    if (old.closest('.breadcrumb')) return;
    old.parentNode.removeChild(old);
  }

  var style = document.createElement('style');
  style.textContent =
    '.visit-counter{margin-left:auto;display:flex;gap:12px;align-items:center;white-space:nowrap;' +
    'font-size:.74rem;font-weight:500;color:#6E6E73}' +
    '.visit-counter:empty{display:none}' +
    '.visit-counter b{font-weight:700}' +
    '.visit-counter .vc-total b{color:#1D1D1F}' +
    '.visit-counter .vc-today b{color:#0071E3}' +
    '.visit-counter.vc-float{position:fixed;left:16px;bottom:16px;z-index:60;background:#fff;' +
    'border:1px solid #D2D2D7;border-radius:980px;padding:6px 12px;box-shadow:0 4px 14px rgba(0,0,0,.08)}' +
    '@media(max-width:400px){.visit-counter .vc-total{display:none}}';
  document.head.appendChild(style);

  function mount() {
    var el = document.createElement('div');
    el.className = 'visit-counter';
    el.id = 'visitCounter';

    var crumb = document.querySelector('.breadcrumb');
    if (crumb) {
      crumb.style.display = 'flex';
      crumb.style.alignItems = 'center';
      crumb.appendChild(el);
    } else {
      el.classList.add('vc-float'); // 브레드크럼이 없는 페이지는 좌측 하단에 작게 표시
      document.body.appendChild(el);
    }

    fetch('/api/site/counter')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        el.innerHTML =
          '<span class="vc-total">누적 <b>' + Number(d.total).toLocaleString() + '</b></span>' +
          '<span class="vc-today">오늘 <b>' + Number(d.today).toLocaleString() + '</b></span>';
      })
      .catch(function () {});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
