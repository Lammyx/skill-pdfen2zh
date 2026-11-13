"""Safe engine failures: diagnostics contain locations and codes, never provider payloads."""

import json
import traceback
from pathlib import Path

from ..agent_files import AgentTranslationError
from ..ai.generation import ProviderRequestError
from .sections import SectionDetectionError


class AssetSetupError(RuntimeError):
    """The PDF engine failed while obtaining or loading its layout resources."""


def error_report(exc: BaseException) -> dict:
    chain = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    frames = [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for error in chain
        for frame in traceback.extract_tb(error.__traceback__)[-12:]
    ]
    asset_failure = any(isinstance(error, AssetSetupError) for error in chain) or any(
        frame["file"] in ("assets.py", "base_doclayout.py", "doclayout.py") for frame in frames
    )
    if any(isinstance(error, AgentTranslationError) for error in chain):
        code = "AGENT_TRANSLATION_REJECTED"
        message = str(next(error for error in chain if isinstance(error, AgentTranslationError)))
    elif any(isinstance(error, SectionDetectionError) for error in chain):
        code = "SECTION_DETECTION_FAILED"
        message = str(next(error for error in chain if isinstance(error, SectionDetectionError)))
    elif asset_failure:
        code = "ASSET_SETUP_FAILED"
        message = (
            "BabelDOC 版面模型或字体资源准备失败；尚不能开始翻译。"
            "请检查 HTTP_PROXY / HTTPS_PROXY，并运行 "
            "python launch.py warmup；资源校验通过后重试。"
        )
    elif any(isinstance(error, ProviderRequestError) for error in chain):
        provider_error = next(error for error in chain if isinstance(error, ProviderRequestError))
        code = provider_error.code
        message = str(provider_error)
    elif any(isinstance(error, json.JSONDecodeError) for error in chain):
        code = "ENGINE_DATA_PARSE_FAILED"
        message = (
            "翻译引擎收到无法解析的数据，任务已停止；没有自动重试。此错误本身不能确定是否由模型响应造成。"
        )
    else:
        code = "TRANSLATION_FAILED"
        message = f"BabelDOC {type(exc).__name__}：翻译失败。请检查 Provider 连通性或 PDF 是否受支持。"
    return {
        "type": "error",
        "message": message,
        "diagnostic": {
            "code": code,
            "errorType": type(exc).__name__,
            "causeTypes": [type(error).__name__ for error in chain[1:]],
            "frames": frames,
        },
    }
