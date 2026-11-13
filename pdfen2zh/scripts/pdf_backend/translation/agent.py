"""Export/replay actual prepared paragraphs; never construct a provider client."""

import json
import threading
from contextlib import contextmanager
from pathlib import Path

from babeldoc.translator.translator import BaseTranslator, OpenAITranslator

from ..agent_files import FORMAT, AgentTranslationError, unit_id
from ..ai.generation import ProviderRequestError
from ..ai.usage import UsageRecorder
from ..workspace import write_json
from .openai_adapter import RequestActivity
from .quality import placeholders_differ
from .repair import looks_untranslated
from .settings import ACADEMIC_PROMPT


class FileTranslator(OpenAITranslator):
    name = "pdfen2zh-agent"

    def __init__(self, source_lang, target_lang, usage_emit):
        # Inherit only the pinned placeholder grammar, never OpenAITranslator.__init__.
        BaseTranslator.__init__(self, source_lang, target_lang, ignore_cache=True)
        self.model = "conversation-v1"
        self.activity = RequestActivity()
        self.ledger = UsageRecorder(usage_emit)

    def do_llm_translate(self, text, rate_limit_params=None):
        if text is None:  # The engine probes LLM capability with None.
            return None
        error = ProviderRequestError("AGENT_MODE_NO_API")
        self.activity.fail(error)
        raise error

    def do_translate(self, text, rate_limit_params=None):
        return self.do_llm_translate(text, rate_limit_params)


class AgentSession:
    def __init__(self, options, observer, source_hash, glossary):
        self.phase, self.observer = options["phase"], observer
        self.source_hash, self.glossary = source_hash, glossary
        self.units, self.completed, self.errors = {}, set(), []
        self.positions = {}
        self.lock = threading.RLock()
        self.expected, self.outputs = {}, {}
        if self.phase == "render":
            packet = json.loads(Path(options["packetPath"]).read_text(encoding="utf-8"))
            self.expected = {unit["id"]: unit for unit in packet["units"]}
            self.outputs = json.loads(Path(options["translationsPath"]).read_text(encoding="utf-8"))

    def summary(self):
        return {
            "mode": "agent", "phase": self.phase, "exportedUnits": len(self.units),
            "appliedUnits": len(self.completed), "apiRequests": 0,
            "conversationTokenUsage": "not_measured",
        }

    def save_packet(self, path):
        units = sorted(self.units.values(), key=lambda item: (item["page"], item["index"]))
        if not units:
            raise AgentTranslationError("没有导出可翻译单元，请检查 PDF 的文字层与处理范围")
        write_json(path, {
            "format": FORMAT, "sourceHash": self.source_hash,
            "instructions": "Translate English into Simplified Chinese. " + ACADEMIC_PROMPT,
            "glossary": self.glossary, "units": units,
        })

    @contextmanager
    def instrument(self):
        from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly

        original_translate = ILTranslatorLLMOnly.translate
        original_batch = ILTranslatorLLMOnly.translate_paragraph
        session = self

        def translate(engine, document):
            session.positions = {
                id(paragraph): (page.page_number + 1, index)
                for page in document.page for index, paragraph in enumerate(page.pdf_paragraph, 1)
            }
            result = original_translate(engine, document)
            if session.errors:
                raise AgentTranslationError("助手译文未能安全回填：" + "; ".join(session.errors[:5]))
            if session.phase == "render" and session.completed != session.expected.keys():
                raise AgentTranslationError("重新解析的待译单元与导出清单不一致，或部分单元未完成回填")
            return result

        def batch(
            engine, batch_paragraph, pbar=None, page_font_map=None, xobj_font_map=None, *args, **kwargs
        ):
            for paragraph, tracker in zip(batch_paragraph.paragraphs, batch_paragraph.trackers, strict=True):
                engine.translation_config.raise_if_cancelled()
                try:
                    session.process(engine, paragraph, tracker, page_font_map, xobj_font_map)
                except ValueError as exc:
                    with session.lock:
                        session.errors.append(str(exc))
                finally:
                    if pbar:
                        pbar.advance(1)

        ILTranslatorLLMOnly.translate = translate
        ILTranslatorLLMOnly.translate_paragraph = batch
        try:
            yield self
        finally:
            ILTranslatorLLMOnly.translate = original_translate
            ILTranslatorLLMOnly.translate_paragraph = original_batch

    def process(self, engine, paragraph, tracker, fonts, xobjs):
        text, prepared = engine.il_translator.pre_translate_paragraph(paragraph, tracker, fonts, xobjs)
        if text is None:
            return
        page, index = self.positions[id(paragraph)]
        key = unit_id(page, index, text)
        unit = {"id": key, "page": page, "index": index, "source": text, "originalText": paragraph.unicode}
        with self.lock:
            if key in self.units:
                raise ValueError(f"引擎重复处理同一待译单元：{key}")
            self.units[key] = unit
        if self.phase == "export":
            return
        if self.expected.get(key) != unit:
            raise ValueError(f"待译单元与冻结原文不匹配：{key}")
        value = self.outputs[key]
        if placeholders_differ(prepared, value) or looks_untranslated(text, value, "zh"):
            raise ValueError(f"译文占位符结构有误或长英文段落未翻译：{key}")
        tracker.new_llm_translate_tracker().set_output(value)
        applied = engine.il_translator.post_translate_paragraph(paragraph, tracker, prepared, value)
        observed = self.observer.units[id(paragraph)]
        if applied is False or observed["structure"]:
            raise ValueError(f"引擎拒绝回填译文：{key}")
        with self.lock:
            self.completed.add(key)
