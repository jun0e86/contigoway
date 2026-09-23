"""의료 관련 법령 검토 모듈 (문서 업로드 → 법령 API 조회 → Bedrock 검토 → Word 결과)."""
from .router import build_router, build_search_router

__all__ = ["build_router", "build_search_router"]
