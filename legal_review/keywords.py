"""자유 문장에서 검색에 쓸 키워드(명사)를 추출한다.

AI/외부 API를 쓰지 않고 로컬 형태소 분석기(kiwipiepy)만 사용한다.
"""
from __future__ import annotations

from functools import lru_cache

# 명사이긴 하지만 법령 검색어로는 너무 일반적이어서 잡음만 늘리는 단어들
STOPWORDS = {
    "것", "수", "등", "때", "경우", "문제", "상황", "부분", "정도", "이거", "저거", "그거",
    "저희", "우리", "사람", "때문", "이유", "생각", "궁금", "질문", "얘기", "이야기", "전후",
    "관련", "처리", "진행", "여부", "가능", "불가능", "이번", "지금", "오늘", "이제", "그냥",
    "제가", "저는", "혹시", "만약", "이것", "그것", "이런", "저런", "그런",
}
_NOUN_TAGS = ("NNG", "NNP", "SL")  # 일반명사, 고유명사, 외국어(알파벳 약어 등)


@lru_cache
def _kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def extract_keywords(text: str, max_keywords: int = 6) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    tokens = _kiwi().tokenize(text)
    seen, ordered = set(), []
    for t in tokens:
        w = t.form.strip()
        if t.tag not in _NOUN_TAGS or len(w) < 2 or w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        ordered.append(w)
    return ordered[:max_keywords]
