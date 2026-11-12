"""Pinned-engine repair: only selected pages, replay good units, bounded requests for misses."""

import json
import re
import threading
from contextlib import contextmanager

from .quality import placeholders_differ
from .repair_cache import RepairCache, parse_outputs


def looks_untranslated(source, output, target):
    if source != output:
        return False
    plain = re.sub(r"<[^>]+>|\{v\d+\}", "", source)
    return bool(
        target == "zh"
        and len(re.findall(r"[A-Za-z]+", plain)) >= 5
        or target == "en"
        and len(re.findall(r"[\u3400-\u9fff]", plain)) >= 10
    )


class RepairSession:
    def __init__(self, options, scope, observer, provider, target_lang, manifest=None):
        self.options, self.scope, self.observer = options, scope, observer
        self.provider, self.target = provider, target_lang
        self.pages = set(options["pages"])
        self.manifest = manifest
        self.lock = threading.RLock()
        self.blocked, self.applied = set(), set()
        self.reused = self.requested = self.missing = self.rejected = 0
        self.cache = None
        self.permission = threading.local()
        self.neighbors = {}

    def check_request(self):
        if not getattr(self.permission, "allowed", False):
            raise ValueError("局部修复已阻止未授权的模型请求")
        self.permission.allowed = False

    def summary(self):
        with self.lock:
            return {
                "baseJobId": self.options["baseJobId"],
                "requestedPages": sorted(self.pages),
                "replacedPages": sorted(self.applied - self.blocked),
                "keptPages": sorted(self.pages - (self.applied - self.blocked)),
                "reusedUnits": self.reused,
                "newRequests": self.requested,
                "missingUnits": self.missing,
                "rejectedUnits": self.rejected,
                "allowNewRequests": self.options["allowNewRequests"],
                "maxRequests": self.options["maxRequests"],
            }

    @contextmanager
    def instrument(self):
        from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly

        original_translate = ILTranslatorLLMOnly.translate
        original_batch = ILTranslatorLLMOnly.translate_paragraph
        session = self

        def translate(engine, document):
            from babeldoc.format.pdf.document_il.midend.il_translator import ParagraphTranslateTracker

            titles = list(getattr(session.scope, "title_aliases", []))
            title = engine.find_title_paragraph(document)
            if title and title.unicode:
                titles.append(title.unicode)
                shared = engine.translation_config.shared_context_cross_split_part
                if not shared.first_paragraph:
                    shared.first_paragraph = shared.snapshot_title_paragraph(title)
                    shared.recent_title_paragraph = shared.snapshot_title_paragraph(title)
            session.cache = RepairCache(
                session.provider, engine.translation_config.lang_in, session.target, titles, session.manifest
            )
            for index, page in enumerate(document.page, 1):
                if index in session.pages:
                    fonts, xobjs = engine._build_font_maps(page)
                    texts = set()
                    for paragraph in page.pdf_paragraph:
                        _, prepared = engine.il_translator.pre_translate_paragraph(
                            paragraph, ParagraphTranslateTracker(), fonts, xobjs
                        )
                        if prepared:
                            texts.add(prepared.unicode)
                    session.neighbors[index] = texts
            # Keep every physical page in the document so nonadjacent selected pages cannot merge.
            hidden = []
            for index, page in enumerate(document.page, 1):
                if index not in session.pages:
                    hidden.append((page, page.pdf_paragraph))
                    page.pdf_paragraph = []
            try:
                return original_translate(engine, document)
            finally:
                for page, paragraphs in hidden:
                    page.pdf_paragraph = paragraphs

        def batch(
            engine,
            batch_paragraph,
            pbar=None,
            page_font_map=None,
            xobj_font_map=None,
            title_paragraph=None,
            local_title_paragraph=None,
            executor=None,
            paragraph_token_count=0,
            mp_id=0,
        ):
            for paragraph, tracker, page in zip(
                batch_paragraph.paragraphs, batch_paragraph.trackers, batch_paragraph.pages, strict=True
            ):
                engine.translation_config.raise_if_cancelled()
                try:
                    session.process(
                        engine,
                        paragraph,
                        tracker,
                        page_font_map,
                        xobj_font_map,
                        title_paragraph,
                        local_title_paragraph,
                        page.page_number + 1,
                    )
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

    def process(self, engine, paragraph, tracker, fonts, xobjs, title, local_title, page):
        text, prepared = engine.il_translator.pre_translate_paragraph(paragraph, tracker, fonts, xobjs)
        if text is None:
            return
        item = {"id": 0, "input": text, "layout_label": paragraph.layout_label}
        prompt = engine._build_llm_prompt(json.dumps([item], ensure_ascii=False), title, local_title, text)
        llm_tracker = tracker.new_llm_translate_tracker()
        llm_tracker.set_input(prompt)
        candidates = self.cache.candidates(prompt, text, self.neighbors.get(page))
        valid = [
            value
            for value in candidates
            if value.strip()
            and not placeholders_differ(prepared, value)
            and sorted(re.findall(r"\{v\d+\}", prepared.unicode)) == sorted(re.findall(r"\{v\d+\}", value))
            and not looks_untranslated(text, value, self.target)
        ]
        value = valid[0] if len(valid) == 1 else None
        reused = value is not None
        translator = engine.translate_engine
        if value is None:
            with self.lock:
                permitted = self.options["allowNewRequests"] and self.requested < self.options["maxRequests"]
                if permitted:
                    self.requested += 1
            if permitted:
                # One explicit attempt per unit; never send a whole batch or enter upstream fallback.
                try:
                    self.permission.allowed = True
                    response = translator.do_llm_translate(prompt, {"request_json_mode": True})
                    parsed = parse_outputs(response)
                    value = parsed.get(0) if set(parsed) == {0} else None
                except (ValueError, TypeError, KeyError):
                    value = None
                finally:
                    self.permission.allowed = False
            if value is None:
                with self.lock:
                    self.missing += 1
                    self.blocked.add(page)
                return
        llm_tracker.set_output(value)
        applied = engine.il_translator.post_translate_paragraph(paragraph, tracker, prepared, value)
        unit = getattr(self.observer, "units", {}).get(id(paragraph), {})
        if applied is False or unit.get("structure") or looks_untranslated(text, value, self.target):
            with self.lock:
                self.rejected += 1
                self.blocked.add(page)
            return
        with self.lock:
            if value != text:
                self.applied.add(page)
            self.reused += reused
        if reused:
            translator.ledger.cache_hit()
        else:
            translator.cache.set(prompt, json.dumps([{"id": 0, "output": value}], ensure_ascii=False))
