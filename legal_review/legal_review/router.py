"""FastAPI 라우터.

    from legal_review import build_router
    app.include_router(build_router(get_current_user))   # ← 기존 JWT 로그인 의존성을 넘긴다

인증 의존성을 필수 인자로 둔 이유: 회사 Bedrock 키로 비용이 나가는 엔드포인트가
실수로 공개되는 것을 막기 위해서다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .case_api import CaseApiError
from .config import ACCEPT_EXTENSIONS, LAW_PRESETS, get_settings
from .extractors import ExtractError, extract
from .law_api import LawApiError
from .llm import LlmError
from .pipeline import ReviewError, run_review
from .report import build_docx
from .situation_search import SituationSearchError, search_situation

log = logging.getLogger("legal_review")
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def build_router(auth_dependency, prefix: str = "/api/legal-review") -> APIRouter:
    if auth_dependency is None:
        raise ValueError("auth_dependency(로그인 검증 의존성)가 필요합니다.")
    router = APIRouter(prefix=prefix, tags=["legal-review"], dependencies=[Depends(auth_dependency)])
    settings = get_settings()
    gate = asyncio.Semaphore(settings.max_concurrent)

    @router.get("/options")
    async def options():
        s = get_settings()
        return {
            "presets": LAW_PRESETS,
            "default_laws": list(s.default_laws),
            "accept": ACCEPT_EXTENSIONS,
            "max_files": s.max_files,
            "max_file_mb": s.max_file_mb,
        }

    @router.post("/review")
    async def review(
        request_text: str = Form(""),
        laws: str = Form(""),                 # 쉼표 구분. 비우면 기본 법령
        output: str = Form("docx"),           # docx | json
        files: list[UploadFile] = File(default=[]),
    ):
        s = get_settings()
        files = [f for f in files if f.filename]     # 파일 미선택 시 브라우저가 보내는 빈 파트 제거
        request_text = request_text.strip()
        if len(request_text) > 5000:
            raise HTTPException(422, "요청 내용은 5,000자 이내로 입력해주세요.")
        if not request_text and not files:
            raise HTTPException(422, "검토 요청 내용을 입력하거나 문서를 업로드해주세요.")
        if len(files) > s.max_files:
            raise HTTPException(422, f"문서는 최대 {s.max_files}개까지 올릴 수 있습니다.")
        law_names = [x.strip() for x in laws.split(",") if x.strip()] or list(s.default_laws)
        if len(law_names) > 8:
            raise HTTPException(422, "법령은 최대 8개까지 선택할 수 있습니다.")

        if gate.locked():
            raise HTTPException(429, "다른 검토가 진행 중입니다. 잠시 후 다시 시도해주세요.")
        async with gate:
            try:
                docs = []
                limit = s.max_file_mb * 1024 * 1024
                for f in files:
                    data = await f.read(limit + 1)
                    if len(data) > limit:
                        raise ExtractError(f"{f.filename}: 파일이 {s.max_file_mb}MB를 넘습니다.")
                    docs.append(await asyncio.to_thread(extract, f.filename or "unnamed", data))
                    del data  # 원본은 즉시 버림 (서버에 저장하지 않음)
                if not request_text and not any(d.text for d in docs):
                    raise ExtractError("업로드한 문서에서 읽을 수 있는 텍스트가 없습니다.")
                result = await run_review(s, request_text, docs, law_names)
            except (ExtractError, ReviewError) as e:
                raise HTTPException(422, str(e))
            except (LawApiError, LlmError) as e:
                log.warning("legal_review upstream error: %s", e)
                raise HTTPException(502, str(e))
            except Exception:
                log.exception("legal_review unexpected error")   # 문서 내용은 로그에 남기지 않음
                raise HTTPException(500, "검토 중 오류가 발생했습니다.")

        log.info("legal_review done files=%d laws=%d items=%d verdict=%s",
                 len(docs), len(result.laws), len(result.items), result.overall_verdict)
        if output == "json":
            return JSONResponse(result.to_json())
        content = await asyncio.to_thread(build_docx, result)
        filename = f"법령검토결과_{datetime.now().strftime('%Y%m%d_%H%M')}.docx"
        return Response(
            content=content,
            media_type=DOCX_MIME,
            headers={"Content-Disposition": f"attachment; filename=\"legal_review.docx\"; filename*=UTF-8''{quote(filename)}"},
        )

    return router


class _RateLimiter:
    """IP당 분당 요청 수를 제한하는 아주 단순한 메모리 리미터.

    여러 워커/컨테이너로 수평 확장하면 워커별로 따로 세므로 느슨하게 동작한다.
    (국가법령정보 API가 '짧은 시간 내 과도한 호출'을 이상 접근으로 간주해 차단할 수 있어 넣었다.)
    """

    def __init__(self, limit_per_min: int):
        self.limit = limit_per_min
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def build_search_router(prefix: str = "/api/legal-review", auth_dependency=None) -> APIRouter:
    """'상황 입력 → 판례/법령해석례/관련 조문' 검색 라우터. AI를 쓰지 않는다.

    문서 검토(build_router)와 달리 회사 Bedrock 접근이 필요 없어 공개 웹사이트에 그대로
    노출해도 된다. 로그인 사용자만 쓰게 하려면 auth_dependency를 넘기면 된다.
    """
    settings = get_settings()
    deps = [Depends(auth_dependency)] if auth_dependency else []
    router = APIRouter(prefix=prefix, tags=["legal-search"], dependencies=deps)
    limiter = _RateLimiter(settings.search_rate_per_min)

    @router.get("/situation-search")
    async def situation_search(request: Request, q: str):
        s = get_settings()
        q = q.strip()
        if not q:
            raise HTTPException(422, "상황을 입력해주세요.")
        if len(q) > 500:
            raise HTTPException(422, "500자 이내로 입력해주세요.")
        client_ip = request.client.host if request.client else "unknown"
        if not limiter.check(client_ip):
            raise HTTPException(429, "요청이 많습니다. 잠시 후 다시 시도해주세요.")
        try:
            result = await search_situation(
                s, q,
                max_keywords=s.search_max_keywords, per_keyword=s.search_per_keyword,
                top_n=s.search_top_n, top_refs=s.search_top_refs,
            )
        except SituationSearchError as e:
            raise HTTPException(422, str(e))
        except CaseApiError as e:
            log.warning("situation_search upstream error: %s", e)
            raise HTTPException(502, str(e))
        except Exception:
            log.exception("situation_search unexpected error")
            raise HTTPException(500, "검색 중 오류가 발생했습니다.")

        return JSONResponse({
            "keywords": result.keywords,
            "precedents": [
                {**asdict(p), "source_label": p.source_label()} for p in result.precedents
            ],
            "interpretations": [
                {**asdict(e), "source_label": e.source_label()} for e in result.interpretations
            ],
            "top_refs": [asdict(r) for r in result.top_refs],
            "ref_articles": [
                {"ref": a.ref, "title": a.title, "text": a.text} for a in result.ref_articles
            ],
            "notices": result.ref_article_errors,
            "disclaimer": "이 결과는 법률 자문이 아닙니다. 정확한 판단은 변호사 등 전문가와 상담하세요.",
        })

    return router
