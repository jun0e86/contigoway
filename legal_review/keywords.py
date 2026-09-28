"""자유 문장에서 검색에 쓸 키워드(명사)를 추출한다.

AI/외부 API를 쓰지 않고 로컬 형태소 분석기(kiwipiepy)만 사용한다.

검색은 앞쪽 키워드 3개만 쓰므로 '어떤 순서로 내보내느냐'가 결과 품질을 좌우한다.
  1) 사용자가 붙여 쓴 명사는 하나의 낱말로 묶는다. (진료 + 기록 → 진료기록)
     형태소 분석기는 '진료기록'을 '진료'와 '기록'으로 쪼개지만, 법령·판례에서는 한 낱말로 쓰인다.
  2) '환자', '진료'처럼 어디에나 나오는 흔한 낱말은 뒤로 미룬다. (버리지는 않는다)
     그래야 '열람', '보호자' 같은 상황의 핵심어가 검색에 실제로 쓰인다.
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

# 의료 분야 상담에서는 거의 모든 문장에 들어가 변별력이 낮은 단어들 (검색 순서만 뒤로 밀린다)
GENERIC = {"환자", "진료", "기록", "의료", "병원", "의사", "직원", "의료기관"}

_NOUN_TAGS = ("NNG", "NNP", "SL")  # 일반명사, 고유명사, 외국어(알파벳 약어 등)
_MERGE_TAGS = ("NNG", "NNP")       # 붙여 쓴 낱말로 묶을 수 있는 품사


@lru_cache
def _kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def _adjacent_compounds(tokens):
    """글자 사이에 다른 글자 없이 붙어 있는 명사 2~3개를 하나의 낱말로 묶는다.

    (묶은 낱말 목록, 묶이면서 흡수된 조각 낱말 집합)을 돌려준다.
    """
    compounds, parts, run = [], set(), []

    def close():
        if 2 <= len(run) <= 3:
            forms = [t.form.strip() for t in run]
            word = "".join(forms)
            if 3 <= len(word) <= 8 and not any(f in STOPWORDS for f in forms):
                compounds.append(word)
                parts.update(forms)

    for t in tokens:
        if t.tag in _MERGE_TAGS and run and t.start == run[-1].start + run[-1].len:
            run.append(t)
            continue
        close()
        run = [t] if t.tag in _MERGE_TAGS else []
    close()
    return compounds, parts


def extract_keywords(text: str, max_keywords: int = 6) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    tokens = _kiwi().tokenize(text)

    compounds, parts = _adjacent_compounds(tokens)

    seen, singles = set(), []
    for t in tokens:
        w = t.form.strip()
        if t.tag not in _NOUN_TAGS or len(w) < 2 or w in STOPWORDS or w in seen or w in parts:
            continue   # 이미 묶은 낱말의 조각('진료', '기록')은 검색 자리만 차지하므로 뺀다
        seen.add(w)
        singles.append(w)

    ordered = []
    for w in compounds + singles:   # 붙여 쓴 낱말이 먼저
        if w not in ordered:
            ordered.append(w)

    specific = [w for w in ordered if w not in GENERIC]
    generic = [w for w in ordered if w in GENERIC]
    return (specific + generic)[:max_keywords]
