"""Compatibility boundary for the pinned BabelDOC 0.6.4 cache representation."""

import copy
import json
import threading

from babeldoc.translator.cache import TranslationCache, _TranslationCache


def content_params(params: dict) -> dict:
    result = copy.deepcopy(params)
    generation = result.get("reader_generation")
    if isinstance(generation, dict):
        generation.pop("requestTimeout", None)
        generation.pop("maxRetries", None)
    return result


class ReaderTranslationCache(TranslationCache):
    """Read old successful entries with arbitrary recorded timeouts; never mutate old rows."""

    def __init__(self, original: TranslationCache, on_hit=None):
        super().__init__(original.translate_engine, copy.deepcopy(original.params))
        self._legacy_key = None
        self._legacy = []
        self._legacy_lock = threading.Lock()
        self.on_hit = on_hit or (lambda: None)

    def get(self, original_text: str) -> str | None:
        cached = super().get(original_text)
        if cached and cached.strip():
            self.on_hit()
            return cached
        with self._legacy_lock:
            if self._legacy_key != self.translate_engine_params:
                legacy = []
                wanted = content_params(self.params)
                rows = (
                    _TranslationCache.select(_TranslationCache.translate_engine_params)
                    .where(_TranslationCache.translate_engine == self.translate_engine)
                    .distinct()
                )
                for row in rows:
                    try:
                        params = json.loads(row.translate_engine_params)
                        if params != self.params and content_params(params) == wanted:
                            legacy.append(TranslationCache(self.translate_engine, params))
                    except (TypeError, ValueError, AttributeError):
                        continue
                self._legacy = legacy
                self._legacy_key = self.translate_engine_params
            candidates = self._legacy
        for candidate in candidates:
            cached = candidate.get(original_text)
            if cached and cached.strip():
                self.set(original_text, cached)
                self.on_hit()
                return cached
        return None
