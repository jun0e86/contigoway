"""국가법령정보센터(law.go.kr) 공동활용 Open API 클라이언트.

- 법령명으로 검색 → 현행 법령의 법령일련번호(MST) 확보 → 본문(JSON) 조회 → 조문 단위로 평탄화
- 하루(기본 24h) 단위로 파일 캐시해서 API 호출과 지연을 줄인다.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .config import Settings


class LawApiError(Exception):
    """법령 API 호출 실패 (사용자에게 보여줘도 되는 메시지)."""


@dataclass
class Article:
    law: str
    ref: str      # 예) "의료법 제56조", "의료법 제56조의2"
    title: str
    text: str


@dataclass
class Law:
    name: str
    mst: str
    meta: dict = field(default_factory=dict)   # 시행일자, 공포일자, 공포번호
    articles: list = field(default_factory=list)

    def toc(self) -> str:
        return "\n".join(f"[{a.ref}] {a.title}" for a in self.articles)


_TEXT_KEYS = ("조문내용", "항내용", "호내용", "목내용")
_CHILD_KEYS = ("항", "호", "목")


def _as_list(x):
    if x is None or x == "":
        return []
    return x if isinstance(x, list) else [x]


def _flatten(node) -> list:
    """조문단위 → 항 → 호 → 목 순서로 텍스트를 평탄화."""
    lines = []
    if isinstance(node, str):
        return [node.strip()] if node.strip() else []
    if isinstance(node, list):
        for n in node:
            lines.extend(_flatten(n))
        return lines
    if isinstance(node, dict):
        for k in _TEXT_KEYS:                # 본문 먼저
            if k in node:
                lines.extend(_flatten(node[k]))
        for k in _CHILD_KEYS:               # 그다음 하위 항목
            if k in node:
                lines.extend(_flatten(node[k]))
    return lines


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name)


def parse_law(data: dict, requested_name: str) -> Law:
    body = data.get("법령") or {}
    info = body.get("기본정보") or {}
    name = info.get("법령명_한글") or requested_name
    jo = body.get("조문") or {}
    units = jo if isinstance(jo, list) else _as_list(jo.get("조문단위"))

    articles = []
    for u in units:
        if not isinstance(u, dict):
            continue
        if u.get("조문여부") not in (None, "", "조문"):   # '전문'(장·절 제목) 제외
            continue
        no = str(u.get("조문번호", "")).strip()
        branch = str(u.get("조문가지번호", "") or "").strip()
        if not no:
            continue
        text = "\n".join(_flatten(u)).strip()
        title = str(u.get("조문제목", "") or "").strip()
        if not text or (not title and "삭제" in text[:40]):   # 삭제된 조문
            continue
        ref = f"{name} 제{no}조" + (f"의{branch}" if branch else "")
        articles.append(Article(law=name, ref=ref, title=title, text=text))

    meta = {k: str(info.get(k, "") or "") for k in ("시행일자", "공포일자", "공포번호")}
    return Law(name=name, mst=str(info.get("법령일련번호", "") or ""), meta=meta, articles=articles)


class LawClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cache_dir = Path(settings.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def _get_json(self, endpoint: str, params: dict) -> dict:
        if not self.s.law_oc:
            raise LawApiError("LAW_API_OC(국가법령정보 API 인증키)가 설정되지 않았습니다.")
        params = {"OC": self.s.law_oc, "type": "JSON", **params}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(f"{self.s.law_api_base}/{endpoint}", params=params)
        except httpx.HTTPError as e:
            raise LawApiError(f"법령 API에 연결하지 못했습니다. ({type(e).__name__})") from None
        text = resp.text.lstrip()
        if resp.status_code != 200 or not text.startswith("{"):
            raise LawApiError(
                "법령 API 응답이 올바르지 않습니다. OC 승인 여부와 서버 IP 등록(open.law.go.kr → OPEN API 신청)을 확인하세요."
            )
        return resp.json()

    async def _find_mst(self, name: str) -> str | None:
        data = await self._get_json("lawSearch.do", {"target": "law", "query": name, "display": 100})
        items = _as_list((data.get("LawSearch") or {}).get("law"))
        want = _norm(name)
        for it in items:  # 정확히 같은 법령명 + 현행만 채택 (비슷한 이름을 추측하지 않음)
            if _norm(it.get("법령명한글", "")) == want and it.get("현행연혁코드", "현행") in ("현행", ""):
                return str(it.get("법령일련번호"))
        return None

    def _cache_path(self, name: str) -> Path:
        return self.cache_dir / (hashlib.sha1(_norm(name).encode()).hexdigest() + ".json")

    def _read_cache(self, name: str):
        p = self._cache_path(name)
        if p.exists() and time.time() - p.stat().st_mtime < self.s.cache_ttl_hours * 3600:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return Law(d["name"], d["mst"], d["meta"], [Article(**a) for a in d["articles"]])
            except Exception:
                return None
        return None

    async def load_law(self, name: str) -> Law:
        cached = self._read_cache(name)
        if cached:
            return cached
        mst = await self._find_mst(name)
        if not mst:
            raise LawApiError(f"'{name}' 현행 법령을 찾지 못했습니다. (법령명 확인)")
        data = await self._get_json("lawService.do", {"target": "law", "MST": mst})
        law = parse_law(data, name)
        if not law.articles:
            raise LawApiError(f"'{name}' 조문을 읽지 못했습니다.")
        law.mst = law.mst or mst
        self._cache_path(name).write_text(json.dumps(asdict(law), ensure_ascii=False), encoding="utf-8")
        return law
