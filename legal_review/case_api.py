"""국가법령정보 Open API의 판례(prec)·법령해석례(expc) 검색/상세 조회.

법령(law) target과 달리 prec/expc는 공식 필드명이 문서마다 조금씩 다르게 소개되어 있어
(예: 판례일련번호/precSeq), 아래는 흔히 쓰이는 후보 키를 순서대로 시도하는 방식으로
방어적으로 파싱한다. 실제 응답을 한 번 확인한 뒤 필요하면 _pick 후보만 손보면 된다.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .config import Settings

_LIST_TTL_MIN = 60  # 검색 목록 캐시(분) — 판례가 새로 나오는 주기를 고려해 짧게


class CaseApiError(Exception):
    """사용자에게 보여줘도 되는 API 오류."""


def _unwrap_obj(data) -> dict:
    """{"PrecService": {...}} 같은 한 겹 래핑을 벗겨 평평한 dict를 얻는다."""
    while isinstance(data, dict) and len(data) == 1:
        data = next(iter(data.values()))
    return data if isinstance(data, dict) else {}


def _unwrap_list(data) -> list[dict]:
    """{"PrecSearch": {"totalCnt": .., "prec": [...]}} 형태에서 dict 리스트를 찾아낸다."""
    obj = data
    if isinstance(obj, dict) and len(obj) == 1:
        obj = next(iter(obj.values()))
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return [x for x in v if isinstance(x, dict)]
        if any(k in obj for k in ("사건명", "안건명")):  # 검색결과 1건뿐이라 dict로 온 경우
            return [obj]
    return []


def _pick(d: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return default


@dataclass
class Precedent:
    id: str
    title: str
    case_no: str
    date: str
    court: str
    case_type: str
    link: str = ""
    held: str = ""       # 판시사항
    summary: str = ""     # 판결요지
    ref_laws: str = ""    # 참조조문 (원문 그대로, 파싱은 situation_search에서)
    ref_cases: str = ""   # 참조판례

    def source_label(self) -> str:
        return f"{self.court} {self.date} 선고 {self.case_no}".strip()


@dataclass
class Interpretation:
    id: str
    title: str
    date: str
    agency: str
    link: str = ""
    question: str = ""   # 질의요지
    answer: str = ""      # 회답
    reason: str = ""      # 이유
    ref_laws: str = ""    # 관련법령

    def source_label(self) -> str:
        return f"{self.agency} 법령해석 ({self.date})".strip()


def _prec_from_search(d: dict) -> Precedent:
    return Precedent(
        id=_pick(d, "판례일련번호", "precSeq"),
        title=_pick(d, "사건명"),
        case_no=_pick(d, "사건번호"),
        date=_pick(d, "선고일자"),
        court=_pick(d, "법원명"),
        case_type=_pick(d, "사건종류명", "판결유형"),
        link=_pick(d, "판례상세링크", "법령상세링크"),
    )


def _prec_from_detail(base: Precedent, d: dict) -> Precedent:
    base.held = _pick(d, "판시사항")
    base.summary = _pick(d, "판결요지")
    base.ref_laws = _pick(d, "참조조문")
    base.ref_cases = _pick(d, "참조판례")
    base.title = base.title or _pick(d, "사건명")
    base.case_no = base.case_no or _pick(d, "사건번호")
    base.date = base.date or _pick(d, "선고일자")
    base.court = base.court or _pick(d, "법원명")
    return base


def _expc_from_search(d: dict) -> Interpretation:
    return Interpretation(
        id=_pick(d, "법령해석례일련번호", "해석일련번호", "안건번호"),
        title=_pick(d, "안건명"),
        date=_pick(d, "회신일자", "해석일자"),
        agency=_pick(d, "회신기관명", "해석기관명", "질의기관명"),
        link=_pick(d, "법령해석례상세링크", "법령상세링크"),
    )


def _expc_from_detail(base: Interpretation, d: dict) -> Interpretation:
    base.question = _pick(d, "질의요지")
    base.answer = _pick(d, "회답")
    base.reason = _pick(d, "이유")
    base.ref_laws = _pick(d, "관련법령")
    base.title = base.title or _pick(d, "안건명")
    base.date = base.date or _pick(d, "회신일자", "해석일자")
    base.agency = base.agency or _pick(d, "회신기관명", "해석기관명", "질의기관명")
    return base


class CaseApiClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cache_dir = Path(settings.cache_dir) / "case"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def _get_json(self, endpoint: str, params: dict) -> dict:
        if not self.s.law_oc:
            raise CaseApiError("LAW_API_OC(국가법령정보 API 인증키)가 설정되지 않았습니다.")
        params = {"OC": self.s.law_oc, "type": "JSON", **params}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(f"{self.s.law_api_base}/{endpoint}", params=params)
        except httpx.HTTPError as e:
            raise CaseApiError(f"법령 API에 연결하지 못했습니다. ({type(e).__name__})") from None
        text = resp.text.lstrip()
        if resp.status_code != 200 or not text.startswith("{"):
            raise CaseApiError("법령 API 응답이 올바르지 않습니다.")
        try:
            return resp.json()
        except ValueError:
            raise CaseApiError("법령 API 응답을 해석하지 못했습니다.") from None

    def _cache_path(self, kind: str, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()
        return self.cache_dir / f"{kind}_{h}.json"

    def _read_cache(self, path: Path, ttl_min: int):
        if path.exists() and time.time() - path.stat().st_mtime < ttl_min * 60:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _write_cache(self, path: Path, obj) -> None:
        try:
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    async def search_precedents(self, keyword: str, display: int = 10) -> list[Precedent]:
        path = self._cache_path("prec_search", keyword)
        cached = self._read_cache(path, _LIST_TTL_MIN)
        if cached is not None:
            return [Precedent(**x) for x in cached]
        data = await self._get_json("lawSearch.do", {"target": "prec", "query": keyword, "display": display})
        items = [i for i in (_prec_from_search(d) for d in _unwrap_list(data)) if i.id]
        self._write_cache(path, [asdict(i) for i in items])
        return items

    async def search_interpretations(self, keyword: str, display: int = 10) -> list[Interpretation]:
        path = self._cache_path("expc_search", keyword)
        cached = self._read_cache(path, _LIST_TTL_MIN)
        if cached is not None:
            return [Interpretation(**x) for x in cached]
        data = await self._get_json("lawSearch.do", {"target": "expc", "query": keyword, "display": display})
        items = [i for i in (_expc_from_search(d) for d in _unwrap_list(data)) if i.id]
        self._write_cache(path, [asdict(i) for i in items])
        return items

    async def get_precedent_detail(self, base: Precedent) -> Precedent:
        path = self._cache_path("prec_detail", base.id)
        cached = self._read_cache(path, self.s.cache_ttl_hours * 60)
        if cached is not None:
            return Precedent(**cached)
        data = await self._get_json("lawService.do", {"target": "prec", "ID": base.id})
        result = _prec_from_detail(base, _unwrap_obj(data))
        self._write_cache(path, asdict(result))
        return result

    async def get_interpretation_detail(self, base: Interpretation) -> Interpretation:
        path = self._cache_path("expc_detail", base.id)
        cached = self._read_cache(path, self.s.cache_ttl_hours * 60)
        if cached is not None:
            return Interpretation(**cached)
        data = await self._get_json("lawService.do", {"target": "expc", "ID": base.id})
        result = _expc_from_detail(base, _unwrap_obj(data))
        self._write_cache(path, asdict(result))
        return result
