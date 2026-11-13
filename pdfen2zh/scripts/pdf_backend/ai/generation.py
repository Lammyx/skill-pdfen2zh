"""Translation generation settings, shared by PDF and Markdown providers."""

import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class GenerationPolicy:
    request_timeout: float = 90
    max_output_tokens: int = 8192
    max_retries: int = 2
    thinking: str = "auto"

    @classmethod
    def from_env(cls):
        policy = cls(
            request_timeout=float(os.getenv("PDF_TRANSLATE_REQUEST_TIMEOUT_SECONDS", "90")),
            max_output_tokens=int(os.getenv("PDF_TRANSLATE_MAX_OUTPUT_TOKENS", "8192")),
            max_retries=int(os.getenv("PDF_TRANSLATE_MAX_RETRIES", "2")),
            thinking=os.getenv("PDF_TRANSLATE_THINKING", "auto").strip().lower(),
        )
        if not 5 <= policy.request_timeout <= 600 or not 128 <= policy.max_output_tokens <= 65536:
            raise ValueError("模型请求超时或输出预算超出允许范围")
        if not 0 <= policy.max_retries <= 5 or policy.thinking not in ("auto", "enabled", "disabled"):
            raise ValueError("模型重试次数或翻译思考设置无效")
        return policy

    def thinking_for(self, base_url: str) -> str | None:
        if self.thinking != "auto":
            return self.thinking
        return "disabled" if urlparse(base_url).hostname == "api.deepseek.com" else None

    def identity(self, base_url: str) -> dict:
        """Only fields that affect a successful completion belong in its cache identity."""
        return {
            "policy": "translation-v2",
            "thinking": self.thinking_for(base_url),
            "maxOutputTokens": self.max_output_tokens,
        }

    def legacy_identities(self, base_url: str) -> list[dict]:
        # Old reader keys were hashes without stored configuration. Probe only exact,
        # verifiable keys for current/default runtime settings, never guess a language/model.
        identities = {}
        for timeout, retries in ((self.request_timeout, self.max_retries), (90.0, 2)):
            values = [timeout, int(timeout)] if float(timeout).is_integer() else [timeout]
            for value in values:
                identity = {**self.identity(base_url), "requestTimeout": value, "maxRetries": retries}
                identities[json.dumps(identity, sort_keys=True)] = identity
        return list(identities.values())

    def extra_body(self, base_url: str) -> dict:
        thinking = self.thinking_for(base_url)
        return {"thinking": {"type": thinking}} if thinking else {}


class ProviderRequestError(RuntimeError):
    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status
        messages = {
            "AGENT_MODE_NO_API": "助手文件模式已阻止翻译服务调用；请检查导出/回填流程。",
            "EMPTY_COMPLETION": "模型没有返回译文，任务已停止；空结果不会写入缓存。",
            "OUTPUT_TRUNCATED": "模型输出达到上限，任务已停止；请增加输出预算或减小翻译范围。",
            "REQUEST_TIMEOUT": "模型请求超时且已达到重试上限，请检查接口或稍后重试。",
            "CONNECTION_FAILED": "模型连接失败且已达到重试上限，请检查网络及代理。",
            "HTTP_ERROR": f"模型接口返回 HTTP {status}，任务已停止；请检查模型、密钥、配额及限流。",
        }
        super().__init__(messages.get(code, "模型响应格式不兼容，任务已停止。"))


def completion_text(response) -> str:
    choices = response.choices
    if not choices:
        raise ProviderRequestError("INVALID_COMPLETION")
    choice = choices[0]
    if choice.finish_reason == "length":
        raise ProviderRequestError("OUTPUT_TRUNCATED")
    text = choice.message.content
    if not isinstance(text, str) or not text.strip():
        raise ProviderRequestError("EMPTY_COMPLETION")
    return text.strip()
