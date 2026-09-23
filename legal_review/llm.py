"""AWS Bedrock Converse API 래퍼.

- 도구 호출(tool use)을 강제해 항상 스키마에 맞는 JSON을 받는다.
- 자격 증명은 boto3 기본 체인 사용:
    · IAM 키:  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (/ AWS_SESSION_TOKEN)
    · Bedrock API 키: AWS_BEARER_TOKEN_BEDROCK (최신 boto3 필요)
    · IAM Role (EC2/ECS 등)
"""
from __future__ import annotations

import asyncio

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings


class LlmError(Exception):
    """Bedrock 호출 실패 (사용자에게 보여줘도 되는 메시지)."""


_client = None


def _get_client(s: Settings):
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime",
            region_name=s.aws_region,
            config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 3, "mode": "standard"}),
        )
    return _client


def _call_sync(s: Settings, system: str, user: str, tool_name: str, tool_desc: str,
               schema: dict, max_tokens: int) -> dict:
    if not s.bedrock_model_id:
        raise LlmError("BEDROCK_MODEL_ID가 설정되지 않았습니다.")
    try:
        resp = _get_client(s).converse(
            modelId=s.bedrock_model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
            toolConfig={
                "tools": [{"toolSpec": {"name": tool_name, "description": tool_desc, "inputSchema": {"json": schema}}}],
                "toolChoice": {"tool": {"name": tool_name}},
            },
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        hint = {
            "AccessDeniedException": "Bedrock 모델 액세스/IAM 권한(bedrock:InvokeModel)을 확인하세요.",
            "ThrottlingException": "요청이 몰렸습니다. 잠시 후 다시 시도하세요.",
            "ValidationException": "모델 ID·리전 또는 요청 형식을 확인하세요.",
            "ResourceNotFoundException": "모델 ID(또는 추론 프로파일)와 리전이 맞는지 확인하세요.",
        }.get(code, "")
        raise LlmError(f"Bedrock 호출 실패 ({code}). {hint}".strip()) from None
    except BotoCoreError as e:
        raise LlmError(f"Bedrock 연결 실패 ({type(e).__name__}). 자격 증명/리전을 확인하세요.") from None

    if resp.get("stopReason") == "max_tokens":
        raise LlmError("AI 응답이 길이 제한에 걸려 잘렸습니다. 문서/조문 범위를 줄여 다시 시도하세요.")
    for block in resp.get("output", {}).get("message", {}).get("content", []):
        if "toolUse" in block:
            return block["toolUse"]["input"]
    raise LlmError("AI 응답에서 결과를 찾지 못했습니다.")


async def call_tool(s: Settings, system: str, user: str, tool_name: str, tool_desc: str,
                    schema: dict, max_tokens: int = 8000) -> dict:
    return await asyncio.to_thread(_call_sync, s, system, user, tool_name, tool_desc, schema, max_tokens)
