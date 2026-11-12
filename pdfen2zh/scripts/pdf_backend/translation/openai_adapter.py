"""Small provider adapter; BabelDOC still owns parsing, layout and paragraph translation."""

import threading
import time
from uuid import uuid4

import httpx
import openai
from babeldoc.translator.translator import OpenAITranslator

from ..ai.generation import GenerationPolicy, ProviderRequestError, completion_text
from ..ai.provider import ProviderConfig
from ..ai.usage import UsageRecorder
from .cache import ReaderTranslationCache


class RequestActivity:
    def __init__(self):
        self.lock = threading.Lock()
        self.active: dict[int, float] = {}
        self.started = self.completed = self.failed = self.retries = 0
        self.error: ProviderRequestError | None = None

    def begin(self):
        with self.lock:
            self.started += 1
            number = self.started
            self.active[number] = time.monotonic()
            return number

    def end(self, number: int, failed: bool):
        with self.lock:
            self.active.pop(number, None)
            self.failed += failed
            self.completed += not failed

    def retry(self):
        with self.lock:
            self.retries += 1

    def fail(self, error: ProviderRequestError):
        with self.lock:
            self.error = self.error or error

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "started": self.started,
                "completed": self.completed,
                "failed": self.failed,
                "retries": self.retries,
                "active": len(self.active),
                "oldestActiveSeconds": round(time.monotonic() - min(self.active.values()), 1)
                if self.active
                else 0,
            }


class ReaderOpenAITranslator(OpenAITranslator):
    def __init__(
        self,
        provider: ProviderConfig,
        target_lang: str,
        policy: GenerationPolicy,
        source_lang="en",
        usage_emit=None,
    ):
        super().__init__(
            lang_in=source_lang,
            lang_out=target_lang,
            model=provider.model,
            base_url=provider.base_url,
            api_key=provider.api_key or "local",
            thinking=policy.thinking_for(provider.base_url),
        )
        self.policy = policy
        self.activity = RequestActivity()
        self.ledger = UsageRecorder(usage_emit)
        # Disable the SDK's hidden retries; all retries below are bounded and counted.
        self.client = self.client.with_options(
            timeout=httpx.Timeout(policy.request_timeout, connect=min(15, policy.request_timeout)),
            max_retries=0,
        )
        self.add_cache_impact_parameters("reader_endpoint", provider.base_url)
        self.add_cache_impact_parameters("reader_prompt_version", "pdf-v2")
        self.add_cache_impact_parameters("reader_generation", policy.identity(provider.base_url))
        self.cache = ReaderTranslationCache(self.cache, on_hit=self.ledger.cache_hit)

    def _request(self, messages, rate_limit_params=None):
        if gate := getattr(self, "request_gate", None):
            gate()
        if self.activity.error:
            raise self.activity.error
        options = dict(self.options) if self.send_temperature else {}
        if self.enable_json_mode_if_requested and (rate_limit_params or {}).get("request_json_mode"):
            options["response_format"] = {"type": "json_object"}
        operation_id = str(uuid4())
        for attempt in range(self.policy.max_retries + 1):
            if self.activity.error:
                raise self.activity.error
            number = self.activity.begin()
            usage_id = self.ledger.begin(operation_id, attempt)
            usage = None
            failure = None
            retryable = False
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.policy.max_output_tokens,
                    extra_body=self.extra_body,
                    **options,
                )
                usage = response.usage
                self.update_token_count(response)
                text = completion_text(response)
                self.ledger.finish(usage_id, "succeeded", usage)
                self.activity.end(number, failed=False)
                return text
            except openai.APITimeoutError:
                failure = ProviderRequestError("REQUEST_TIMEOUT")
                retryable = True
            except openai.APIConnectionError:
                failure = ProviderRequestError("CONNECTION_FAILED")
                retryable = True
            except openai.APIStatusError as exc:
                if isinstance(exc.body, dict):
                    usage = exc.body.get("usage")
                failure = ProviderRequestError("HTTP_ERROR", exc.status_code)
                retryable = exc.status_code in (408, 409, 429) or exc.status_code >= 500
            except ProviderRequestError as exc:
                failure = exc
            except Exception:
                failure = ProviderRequestError("INVALID_COMPLETION")
            self.activity.end(number, failed=True)
            self.ledger.finish(usage_id, "failed", usage, failure.code)
            if retryable and attempt < self.policy.max_retries:
                self.activity.retry()
                time.sleep(min(2**attempt, 4))
                continue
            self.activity.fail(failure)
            raise failure

    # Override only provider calls, retaining BabelDOC's prompts, placeholders and text cache.
    def do_llm_translate(self, text, rate_limit_params=None):
        if text is None:
            return None
        return self._request([{"role": "user", "content": text}], rate_limit_params)

    def do_translate(self, text, rate_limit_params=None):
        return self._request(self.prompt(text), rate_limit_params)
