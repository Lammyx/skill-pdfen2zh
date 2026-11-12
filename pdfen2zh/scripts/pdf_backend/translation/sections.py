"""Conservative section filtering of BabelDOC 0.6.4 IR, before any term/translation calls.

Never select entire pages: the abstract and introduction often share one page.
Excluded paragraphs stay alive and are restored before typesetting, with their original
character/formula compositions. No source text is serialized in the scope report.
"""

import re
from contextlib import contextmanager
from functools import wraps

ALL_SECTIONS = ("abstract", "body", "appendix")
SECTION_VERSION = "sections-v1"


class SectionDetectionError(RuntimeError):
    """A partial request cannot be delimited safely; never silently translate everything."""


def heading_kind(paragraph):
    text = " ".join((paragraph.unicode or "").split()).strip()
    if not text:
        return None
    # Standalone labels, or an explicit colon when the label shares a paragraph.
    if re.fullmatch(r"abstract|摘\s*要", text, re.I) or re.match(r"^(abstract|摘要)\s*[:：]", text, re.I):
        return "abstract"
    title = paragraph.layout_label in ("title", "section_title", "section_header")
    if len(text) > 160 or "..." in text or re.search(r"\.{2,}|…", text):
        return None  # Table-of-contents entries are not section boundaries.
    if re.fullmatch(r"(?:\d+[.、]?\s*)?(?:introduction|引言|绪论|前言)", text, re.I):
        return "body"
    if title and re.match(r"^1[.、\s]\s*\S", text):
        return "body"
    if re.fullmatch(r"references|bibliography|参考文献", text, re.I):
        return "references"
    if re.match(r"^(?:keywords?|index terms|关键词)\s*[:：]", text, re.I):
        return "body"
    if re.fullmatch(
        r"(?:appendix|appendices|supplementary material|supplemental material|附录|附錄)", text, re.I
    ):
        return "appendix"
    if title and re.match(
        r"^(?:appendix|appendices|附录|附錄)\s*[:：A-Z一二三四五六七八九十0-9.、\s]", text, re.I
    ):
        return "appendix"
    # Many conference papers name their appendices 'A Proofs', after References.
    if title and re.match(r"^[A-Z][.、\s]\s*\S", text):
        return "lettered"
    return None


def plan_sections(document, selected):
    selected = set(selected)
    partial = selected != set(ALL_SECTIONS)
    # Use the same character/render order that BabelDOC uses for paragraph grouping.
    entries = [(index + 1, p) for index, page in enumerate(document.page) for p in page.pdf_paragraph]
    markers = [(i, heading_kind(p)) for i, (_, p) in enumerate(entries)]
    abstracts = [i for i, kind in markers if kind == "abstract"]
    references = next((i for i, kind in markers if kind == "references"), None)
    appendix = next(
        (
            i
            for i, kind in markers
            if kind == "appendix" or (kind == "lettered" and references is not None and i > references)
        ),
        None,
    )
    abstract = abstracts[0] if abstracts else None
    body = next((i for i, kind in markers if kind == "body" and (abstract is None or i > abstract)), None)
    if partial:
        if len(abstracts) > 1 or (
            abstract is not None and (body is None or (appendix is not None and body >= appendix))
        ):
            raise SectionDetectionError("摘要与正文边界无法可靠识别，请改选全部三个部分。")
        if "abstract" in selected and abstract is None:
            raise SectionDetectionError("未识别到独立摘要标题，请改选全部三个部分。")
        if "appendix" in selected and appendix is None:
            raise SectionDetectionError("未识别到附录标题，请取消附录或改选全部三个部分。")
        if "body" in selected and body is None:
            raise SectionDetectionError("未识别到正文起点，请改选全部三个部分。")
        if abstract is not None and appendix is not None and appendix <= abstract:
            raise SectionDetectionError("章节顺序不明确，请改选全部三个部分。")
    assignments = {}
    counts = {name: {"paragraphs": 0, "pages": set()} for name in ALL_SECTIONS}
    for i, (page, paragraph) in enumerate(entries):
        section = (
            "appendix"
            if appendix is not None and i >= appendix
            else "abstract"
            if abstract is not None and body is not None and abstract <= i < body
            else "body"
        )
        assignments[id(paragraph)] = section
        counts[section]["paragraphs"] += 1
        counts[section]["pages"].add(page)
    included = {key for key, section in assignments.items() if section in selected}
    if partial and not included:
        raise SectionDetectionError("所选部分没有可处理的段落，请改选全部三个部分。")
    return included, {
        "version": SECTION_VERSION,
        "selected": [name for name in ALL_SECTIONS if name in selected],
        "partial": partial,
        "selectedParagraphs": len(included),
        "excludedParagraphs": len(entries) - len(included),
        "sections": {
            name: {"paragraphs": data["paragraphs"], "pages": sorted(data["pages"])}
            for name, data in counts.items()
        },
    }


class SectionFilter:
    def __init__(self, selected):
        self.selected = selected
        self.report = None
        self.excluded = []

    def prepare(self, document):
        included, self.report = plan_sections(document, self.selected)
        if not self.report["partial"]:
            return
        for page in document.page:
            original = list(page.pdf_paragraph)
            excluded = [p for p in original if id(p) not in included]
            self.excluded.append((page, original, excluded))
            page.pdf_paragraph = [p for p in original if id(p) in included]

    def restore(self):
        for page, original, excluded in self.excluded:
            # The translator may create additional split paragraphs. Preserve those, too.
            current = {id(p) for p in page.pdf_paragraph}
            missing = {id(p) for p in excluded}
            original_ids = {id(p) for p in original}
            extras = [p for p in page.pdf_paragraph if id(p) not in original_ids]
            page.pdf_paragraph = [p for p in original if id(p) in current or id(p) in missing] + extras
        self.excluded.clear()

    @contextmanager
    def instrument(self):
        from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
        from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly
        from babeldoc.format.pdf.document_il.midend.styles_and_formulas import StylesAndFormulas

        original_process = StylesAndFormulas.process
        original_batch = ILTranslatorLLMOnly.translate
        original_single = ILTranslator.translate
        scope = self

        @wraps(original_process)
        def process(engine, document, *args, **kwargs):
            from .title_fragments import repair_title_fragments

            scope.title_aliases = [
                p.unicode
                for page in document.page
                for p in page.pdf_paragraph
                if p.layout_label == "title" and p.unicode
            ][:1]
            repaired = repair_title_fragments(document)
            result = original_process(engine, document, *args, **kwargs)
            scope.prepare(document)
            scope.report["headingFragmentsMerged"] = repaired
            return result

        def translate(original):
            @wraps(original)
            def wrapped(engine, document, *args, **kwargs):
                try:
                    return original(engine, document, *args, **kwargs)
                finally:
                    scope.restore()

            return wrapped

        StylesAndFormulas.process = process
        ILTranslatorLLMOnly.translate = translate(original_batch)
        ILTranslator.translate = translate(original_single)
        try:
            yield self
        finally:
            self.restore()
            StylesAndFormulas.process = original_process
            ILTranslatorLLMOnly.translate = original_batch
            ILTranslator.translate = original_single
