import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str

    def __post_init__(self):
        normalized = self.base_url.strip().rstrip("/")
        # Accept a copied Chat Completions URL without appending that path twice.
        if normalized.endswith("/chat/completions"):
            normalized = normalized.removesuffix("/chat/completions").rstrip("/")
        object.__setattr__(self, "base_url", normalized)

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("PDF_TRANSLATE_BASE_URL", "").rstrip("/"),
            os.getenv("PDF_TRANSLATE_MODEL", ""),
            os.getenv("PDF_TRANSLATE_API_KEY", ""),
        )

    def validate(self):
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not self.model:
            raise ValueError(
                "请设置 PDF_TRANSLATE_BASE_URL、PDF_TRANSLATE_MODEL 和所需的 PDF_TRANSLATE_API_KEY"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Provider URL 不允许嵌入认证信息、查询或片段")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("远程 Provider 必须使用 HTTPS")

    def identity(self) -> dict:
        return {"endpoint": self.base_url, "model": self.model}
