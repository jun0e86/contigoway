/**
 * 모든 페이지의 <div id="site-nav"></div> 자리에 partials/nav.html을 fetch해서 삽입한다.
 * body에 data-page="diary" 처럼 현재 페이지를 표시해두면, 해당 메뉴에 active 클래스를 붙여
 * 어느 페이지에 있는지도 시각적으로 표시해준다 (선택 사항).
 *
 * 모든 html 파일의 <body> 여는 태그 바로 아래에 아래 한 줄을 넣고,
 * </body> 직전에 <script src="/js/include-nav.js"></script> 를 넣으면 된다.
 *   <div id="site-nav"></div>
 */
(async function loadSiteNav() {
  const mountPoint = document.getElementById("site-nav");
  if (!mountPoint) {
    console.warn("[include-nav] #site-nav 요소를 찾을 수 없습니다. 이 페이지에는 공통 네비게이션이 삽입되지 않습니다.");
    return;
  }

  try {
    const res = await fetch("/partials/nav.html", { cache: "no-store" });
    if (!res.ok) throw new Error(`nav.html 로드 실패: ${res.status}`);
    const html = await res.text();
    mountPoint.outerHTML = html; // 플레이스홀더를 실제 nav 마크업으로 교체

    // 현재 페이지에 해당하는 메뉴 강조 표시
    const currentPage = document.body.dataset.page;
    if (currentPage) {
      const activeLink = document.querySelector(`[data-nav="${currentPage}"]`);
      if (activeLink) activeLink.classList.add("active");
    }

    // 로그아웃 버튼 동작은 기존 프로젝트의 로그아웃 로직을 그대로 연결
    const logoutLink = document.getElementById("logout-link");
    if (logoutLink && typeof window.handleLogout === "function") {
      logoutLink.addEventListener("click", (e) => {
        e.preventDefault();
        window.handleLogout();
      });
    }
  } catch (err) {
    console.error("[include-nav] 네비게이션 로드 중 오류:", err);
  }
})();
