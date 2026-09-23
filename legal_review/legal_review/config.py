"""환경변수 기반 설정. 값은 .env / Docker 환경변수로만 주입한다 (코드·GitHub에 키를 넣지 말 것)."""
import os
from dataclasses import dataclass
from functools import lru_cache


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _list(name: str, default: str) -> tuple:
    raw = os.getenv(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    law_oc: str                 # 국가법령정보 공동활용 OC(인증키, 신청 시 입력한 이메일 ID)
    law_api_base: str
    aws_region: str
    bedrock_model_id: str       # 모델 ID 또는 추론 프로파일 ID (회사에서 허용된 값)
    max_files: int
    max_file_mb: int
    max_doc_chars: int          # 검토 LLM에 넣을 문서 텍스트 총량 상한
    max_articles: int           # 검토에 사용할 조문 수 상한
    max_concurrent: int         # 동시 검토 수 (비용·부하 제어)
    cache_dir: str
    cache_ttl_hours: int
    default_laws: tuple
    search_max_keywords: int      # 문장에서 추출할 최대 키워드 수
    search_per_keyword: int       # 키워드 1개당 판례/해석례 검색 결과 수
    search_top_n: int             # 최종적으로 보여줄 판례/해석례 수 (각각)
    search_top_refs: int          # 원문까지 보여줄 관련 조문 수
    search_rate_per_min: int      # IP당 분당 허용 요청 수 (0=제한 없음)
    search_domain_anchor: str     # 이 문자열이 제목에 있는 결과를 우선함 (빈 문자열이면 우선순위 없음)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        law_oc=os.getenv("LAW_API_OC", ""),
        law_api_base=os.getenv("LAW_API_BASE", "https://www.law.go.kr/DRF").rstrip("/"),
        aws_region=os.getenv("AWS_REGION", "ap-northeast-2"),
        bedrock_model_id=os.getenv("BEDROCK_MODEL_ID", ""),
        max_files=_int("LEGAL_MAX_FILES", 5),
        max_file_mb=_int("LEGAL_MAX_FILE_MB", 10),
        max_doc_chars=_int("LEGAL_MAX_DOC_CHARS", 80000),
        max_articles=_int("LEGAL_MAX_ARTICLES", 30),
        max_concurrent=_int("LEGAL_MAX_CONCURRENT", 2),
        cache_dir=os.getenv("LEGAL_CACHE_DIR", "/tmp/legal_review_cache"),
        cache_ttl_hours=_int("LEGAL_CACHE_TTL_HOURS", 24),
        default_laws=_list(
            "LEGAL_DEFAULT_LAWS",
            "의료법,의료법 시행령,의료법 시행규칙,개인정보 보호법",
        ),
        search_max_keywords=_int("LEGAL_SEARCH_MAX_KEYWORDS", 5),
        search_per_keyword=_int("LEGAL_SEARCH_PER_KEYWORD", 8),
        search_top_n=_int("LEGAL_SEARCH_TOP_N", 5),
        search_top_refs=_int("LEGAL_SEARCH_TOP_REFS", 3),
        search_rate_per_min=_int("LEGAL_SEARCH_RATE_PER_MIN", 15),
        search_domain_anchor=os.getenv("LEGAL_SEARCH_DOMAIN_ANCHOR", "의료"),
    )


# 화면에서 고를 수 있는 법령 묶음(이름은 국가법령정보센터의 법령명과 같아야 조회됨).
LAW_PRESETS = {
    "의료 일반·의료광고": ["의료법", "의료법 시행령", "의료법 시행규칙"],
    "진료정보·개인정보": ["의료법", "의료법 시행규칙", "개인정보 보호법", "개인정보 보호법 시행령"],
    "의료기기": ["의료기기법", "의료기기법 시행령", "의료기기법 시행규칙"],
    "의약품·약국": ["약사법", "약사법 시행령", "약사법 시행규칙"],
    "연구·임상(생명윤리)": ["생명윤리 및 안전에 관한 법률", "생명윤리 및 안전에 관한 법률 시행규칙", "개인정보 보호법"],
    "응급·감염병": ["응급의료에 관한 법률", "감염병의 예방 및 관리에 관한 법률"],
    "건강보험·비급여": ["국민건강보험법", "국민건강보험법 시행규칙"],
}

ACCEPT_EXTENSIONS = [".txt", ".md", ".csv", ".docx", ".xlsx", ".xlsm", ".pptx", ".pdf", ".hwpx", ".hwp"]
