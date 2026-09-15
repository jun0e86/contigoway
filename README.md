# contigoway 공통 네비게이션 적용 가이드

## 문제

index.html에만 "공지관리" 메뉴를 추가했는데, diary.html / schedule.html 등 다른 페이지는
네비게이션 바가 각 파일에 하드코딩되어 있어서 반영이 안 됐던 것입니다.
지금 구조로는 메뉴 하나 바뀔 때마다 모든 html 파일을 일일이 찾아 고쳐야 하고, 이번처럼 계속 누락이 생깁니다.

## 해결

네비게이션을 `partials/nav.html` 파일 하나로 분리하고, 모든 페이지가 JS로 그 파일을 불러와
쓰도록 바꿉니다. 이후로는 `nav.html` 한 곳만 고치면 전체 페이지에 자동 반영됩니다.

## 파일 구성

```
contigoway/
├── partials/
│   └── nav.html          # 네비게이션 마크업 (메뉴 추가/삭제는 여기서만)
├── js/
│   └── include-nav.js    # nav.html을 불러와 삽입하는 스크립트
├── index.html
├── diary.html
├── schedule.html
└── ...
```

## 적용 순서

### 1) 파일 배치
`partials/nav.html`과 `js/include-nav.js`를 서버의 정적 파일 경로에 그대로 올립니다.

### 2) 기존 html 파일들 수정
각 html 파일에서 기존에 하드코딩되어 있던 `<header>...</header>` 네비게이션 블록을
아래 한 줄로 교체합니다.

```html
<div id="site-nav"></div>
```

그리고 `</body>` 바로 앞에 다음 줄을 추가합니다.

```html
<script src="/js/include-nav.js"></script>
```

(선택) 어느 페이지인지 표시해서 메뉴 활성 강조를 쓰고 싶다면 `<body>` 태그에
`data-page` 속성을 추가하세요. `partials/nav.html`의 `data-nav` 값과 맞춰야 합니다.

```html
<body data-page="diary">
```

### 3) 일괄 적용 (선택)
파일이 많으면 `apply_shared_nav.py`로 자동 치환할 수 있습니다.

```bash
pip install beautifulsoup4
python apply_shared_nav.py --root ./contigoway --dry-run   # 변경 대상만 먼저 확인
python apply_shared_nav.py --root ./contigoway             # 실제 적용 (원본은 .bak으로 백업됨)
```

파일마다 기존 header 마크업이 조금씩 다르게 틀어져 있었을 수 있으니(이번 버그의 원인 자체가
그거라서), 적용 후 페이지를 하나씩 열어서 메뉴가 잘 나오는지 확인하세요.

### 4) 확인
`partials/nav.html`에서 메뉴 하나를 추가/삭제해보고, index.html뿐 아니라 diary.html,
schedule.html 등 다른 페이지에도 바로 반영되는지 확인하면 끝입니다.

## 참고: 더 근본적인 대안

지금처럼 정적 html 여러 장을 복붙 구조로 유지보수하는 대신, 백엔드가 FastAPI라면
Jinja2 템플릿으로 전환해서 `base.html` 레이아웃 하나에 네비게이션을 두고 각 페이지가
`{% extends "base.html" %}`로 상속받는 구조로 가면 이런 종류의 누락 버그 자체가
구조적으로 발생하지 않습니다. 지금 당장 급하면 위 JS include 방식으로 먼저 막고,
여유 있을 때 템플릿 구조로 리팩터링하는 걸 추천드립니다.
