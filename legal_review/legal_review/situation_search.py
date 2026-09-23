"""'상황 문장' → 키워드 추출 → 판례·법령해석례 검색 → 관련 조문 추출.

AI(LLM)를 쓰지 않는다. 판례·법령해석례의 공식 '참조조문/관련법령' 필드를
그대로 활용해 조문을 연결한다는 점이 핵심이다.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from .case_api import CaseApiClient, Interpretation, Precedent
from .config import Settings
from .keywords import extract_keywords
from .law_api import Article, LawApiError, LawClient

_ARTICLE_RE = re.compile(r"제(\d+)조(?:의(\d+))?")
_LAW_NAME_RE = re.compile(r"^([가-힣][가-힣ㆍ·A-Za-z0-9\s]{0,25}?법(?:\s*시행령|\s*시행규칙)?)\s*(.*)$")


class SituationSearchError(Exception):
    """사용자에게 보여줘도 되는 오류."""


@dataclass
class RefHit:
    ref: str       # "의료법 제19조" 형태
    count: int      # 몇 건의 판례·해석례에서 인용됐는지


@dataclass
class SituationResult:
    keywords: list
    precedents: list = field(default_factory=list)     # list[Precedent]
    interpretations: list = field(default_factory=list)  # list[Interpretation]
    top_refs: list = field(default_factory=list)         # list[RefHit]
    ref_articles: list = field(default_factory=list)     # list[Article] (원문 조회 성공한 것만)
    ref_article_errors: list = field(default_factory=list)  # 조회 실패 사유(참고용)


def _parse_refs(text: str) -> list[str]:
    """'의료법 제19조, 제33조, 의료법 시행령 제20조' 같은 문자열에서 조문 참조를 뽑는다.

    법령명이 생략된 연속 조문(같은 법 제N조)은 직전에 나온 법령명을 그대로 적용한다.
    완벽하지 않은 휴리스틱이므로 참고용으로만 쓴다.
    """
    refs, current_law = [], None
    for piece in re.split(r"[,、;]", text or ""):
        piece = piece.strip()
        if not piece:
            continue
        m = _LAW_NAME_RE.match(piece)
        if m:
            current_law, rest = m.group(1).strip(), m.group(2)
            current_law = re.sub(r"^구\s+", "", current_law)  # "구 의료법" → "의료법" (개정 전 조문 인용은 현행법으로 조회)
        else:
            rest = piece
        if not current_law:
            continue
        for jo, branch in _ARTICLE_RE.findall(rest):
            refs.append(f"{current_law} 제{jo}조" + (f"의{branch}" if branch else ""))
    return refs


_RELATION_MARK_RE = re.compile(r"<\s*관\s*계\s*법\s*령\s*>|<\s*관\s*련\s*법\s*령\s*>")
_BRACKET_LAW_RE = re.compile(r"「([^」]{2,30}?)」")


def _parse_relation_section(text: str) -> list[str]:
    """법령해석례 '이유' 필드는 참조조문이 별도 필드가 아니라, 본문 끝의 '<관계 법령>' 표시 뒤에
    조문 원문이 그대로 붙어 있는 형태다. 「법령명」이 새로 등장하는 지점을 법령 경계로 보고,
    그 뒤(최대 800자) 안에서 '제N조'를 조문 참조로 추출한다. 판례의 '참조조문'만큼 깔끔하지
    않은 휴리스틱이므로 참고용으로만 쓴다.
    """
    m = _RELATION_MARK_RE.search(text or "")
    if not m:
        return []
    parts = _BRACKET_LAW_RE.split(text[m.end():])
    refs = []
    for i in range(1, len(parts), 2):  # parts = [머리말, 법령명, 본문, 법령명, 본문, ...]
        current_law = parts[i].strip()
        chunk = parts[i + 1][:800] if i + 1 < len(parts) else ""
        for jo, branch in _ARTICLE_RE.findall(chunk):
            ref = f"{current_law} 제{jo}조" + (f"의{branch}" if branch else "")
            if ref not in refs:
                refs.append(ref)
    return refs[:8]


async def _search_one(client: CaseApiClient, keyword: str, per_type: int):
    precs, expcs = await asyncio.gather(
        client.search_precedents(keyword, display=per_type),
        client.search_interpretations(keyword, display=per_type),
        return_exceptions=True,
    )
    precs = precs if isinstance(precs, list) else []
    expcs = expcs if isinstance(expcs, list) else []
    return precs, expcs


def _rank(items_by_keyword: list[list], top_n: int, anchor: str = "") -> list:
    """여러 키워드 검색 결과를 합쳐, 여러 키워드에 걸쳐 나온(=더 관련 있어 보이는) 항목을 우선한다.

    anchor(도메인 키워드, 기본 '의료')가 제목 맨 앞에 오는 항목("의료법위반[...]")은
    괄호 설명 등 제목 뒷부분에 우연히 섞여 들어간 항목보다 더 가중치를 준다.
    """
    score, order, first_seen = {}, {}, {}
    for kw_idx, items in enumerate(items_by_keyword):
        for pos, item in enumerate(items):
            bonus = 0
            if anchor:
                idx = (item.title or "").find(anchor)
                bonus = 3 if idx == 0 else (1 if 0 < idx <= 8 else 0)
            score[item.id] = score.get(item.id, 0) + max(3 - pos // 3, 1) + bonus
            if item.id not in first_seen:
                first_seen[item.id] = item
                order[item.id] = (kw_idx, pos)
    ranked_ids = sorted(score, key=lambda i: (-score[i], order[i]))
    return [first_seen[i] for i in ranked_ids[:top_n]]


async def search_situation(s: Settings, situation_text: str, max_keywords: int = 5,
                            per_keyword: int = 8, top_n: int = 5, top_refs: int = 3) -> SituationResult:
    keywords = extract_keywords(situation_text, max_keywords=max_keywords)
    if not keywords:
        raise SituationSearchError("상황을 조금 더 구체적으로 입력해주세요. (예: '환자 동의 없이 진료기록을 공유했어요')")

    client = CaseApiClient(s)
    # API 호출량을 고려해 상위 3개 키워드로만 검색
    search_keywords = keywords[:3]
    try:
        pairs = await asyncio.gather(*(_search_one(client, kw, per_keyword) for kw in search_keywords))
    except Exception as e:
        raise SituationSearchError(f"검색 중 오류가 발생했습니다. ({type(e).__name__})") from None

    anchor = s.search_domain_anchor
    def _domain_filter(items):
        """제목에 anchor(기본 '의료')가 없는 결과를 걸러낸다.

        '동의', '공유'처럼 흔한 낱말은 건축법·형사소송법 등 무관한 분야에서도 많이 잡히므로,
        이 도구의 용도(의료법 상담)에 맞게 제목 기준으로 우선순위를 준다.
        """
        if not anchor:
            return items
        return [i for i in items if anchor in (i.title or "")]

    filtered_pairs = [(_domain_filter(p), _domain_filter(e)) for p, e in pairs]
    precs = _rank([p for p, _ in filtered_pairs], top_n, anchor)
    expcs = _rank([e for _, e in filtered_pairs], top_n, anchor)
    if not precs and not expcs:
        # 도메인 필터링으로 전부 걸러졌을 수 있으니, 필터 없이 한 번 더 시도(완전히 빈 결과보다는 낫다)
        precs = _rank([p for p, _ in pairs], top_n, anchor)
        expcs = _rank([e for _, e in pairs], top_n, anchor)
    if not precs and not expcs:
        raise SituationSearchError(
            "관련 판례·법령해석례를 찾지 못했습니다. 다른 표현으로 다시 입력해보세요. "
            f"(추출된 키워드: {', '.join(keywords)})"
        )

    precs, expcs = await asyncio.gather(
        asyncio.gather(*(client.get_precedent_detail(p) for p in precs), return_exceptions=True),
        asyncio.gather(*(client.get_interpretation_detail(e) for e in expcs), return_exceptions=True),
    )
    precs = [p for p in precs if isinstance(p, Precedent)]
    expcs = [e for e in expcs if isinstance(e, Interpretation)]

    ref_count: dict[str, int] = {}
    for idx, src in enumerate(precs):
        w = max(3 - idx, 1)  # 더 관련도 높게 랭크된 판례의 조문을 우선
        for ref in set(_parse_refs(src.ref_laws)):
            ref_count[ref] = ref_count.get(ref, 0) + w
    for idx, src in enumerate(expcs):
        w = max(3 - idx, 1)
        # 법령해석례는 참조조문이 별도 필드로 오지 않으므로 '이유' 본문에서 추출한다.
        for ref in set(_parse_relation_section(src.reason)):
            ref_count[ref] = ref_count.get(ref, 0) + w
    ranked_refs = sorted(ref_count.items(), key=lambda kv: -kv[1])[:top_refs]

    law_client, articles, errors, law_cache = LawClient(s), [], [], {}
    for ref, count in ranked_refs:
        m = re.match(r"^(.*?)\s*제(\d+)조(?:의(\d+))?$", ref)
        if not m:
            continue
        law_name, jo, branch = m.group(1), m.group(2), m.group(3)
        want = f"{law_name} 제{jo}조" + (f"의{branch}" if branch else "")
        try:
            if law_name not in law_cache:
                law_cache[law_name] = await law_client.load_law(law_name)
            article = next((a for a in law_cache[law_name].articles if a.ref == want), None)
            (articles if article else errors).append(article or f"{want}: 조문을 찾지 못했습니다.")
        except LawApiError as e:
            errors.append(f"{law_name}: {e}")

    return SituationResult(
        keywords=keywords,
        precedents=precs,
        interpretations=expcs,
        top_refs=[RefHit(ref=r, count=c) for r, c in ranked_refs],
        ref_articles=[a for a in articles if isinstance(a, Article)],
        ref_article_errors=[a for a in articles if isinstance(a, str)] + errors,
    )
