"""
사용법:
    pip install beautifulsoup4
    python apply_shared_nav.py --dry-run   # 먼저 어떤 파일이 바뀌는지만 확인
    python apply_shared_nav.py             # 실제 적용

동작:
1) 프로젝트 루트(기본값: 현재 폴더)의 모든 *.html 파일을 순회한다.
2) <header ...> ... </header> 블록(기존에 복붙되어 있던 네비게이션)을 찾아
   <div id="site-nav"></div> 플레이스홀더로 교체한다.
3) </body> 직전에 <script src="/js/include-nav.js"></script> 가 없으면 추가한다.
4) 원본은 .bak 파일로 백업해둔다.

주의: 파일마다 header 마크업이 서로 다르게 틀어져 있었다면(이번 버그의 원인처럼) 자동 치환이
100% 깔끔하지 않을 수 있으니, --dry-run으로 먼저 변경 대상 파일 목록을 확인하고
적용 후에는 diff나 실제 페이지 렌더링으로 꼭 확인하세요.
"""

import argparse
import re
from pathlib import Path

HEADER_PATTERN = re.compile(r"<header\b.*?</header>", re.DOTALL | re.IGNORECASE)
PLACEHOLDER = '<div id="site-nav"></div>'
SCRIPT_TAG = '<script src="/js/include-nav.js"></script>'


def process_file(path: Path, dry_run: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    original = text

    if "<header" in text.lower():
        text = HEADER_PATTERN.sub(PLACEHOLDER, text, count=1)

    if SCRIPT_TAG not in text and "</body>" in text:
        text = text.replace("</body>", f"  {SCRIPT_TAG}\n</body>")

    changed = text != original
    if changed and not dry_run:
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
        path.write_text(text, encoding="utf-8")

    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="html 파일들이 있는 루트 경로")
    parser.add_argument("--dry-run", action="store_true", help="실제로 파일을 바꾸지 않고 대상만 출력")
    args = parser.parse_args()

    root = Path(args.root)
    html_files = sorted(root.rglob("*.html"))

    changed_files = []
    for f in html_files:
        if process_file(f, dry_run=args.dry_run):
            changed_files.append(f)

    print(f"검사한 파일: {len(html_files)}개")
    print(f"{'변경 예정' if args.dry_run else '변경 완료'}: {len(changed_files)}개")
    for f in changed_files:
        print(f"  - {f}")


if __name__ == "__main__":
    main()
