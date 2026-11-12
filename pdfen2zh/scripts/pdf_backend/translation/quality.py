"""Version-pinned, process-local BabelDOC observations, without text or prompt logging.

Units are actual engine fragments, including merged/split fragments, not original paragraphs.
An applied unit confirms postprocessing returned successfully, never semantic correctness.
"""

import re
import threading
from contextlib import contextmanager
from functools import wraps


class QualityObserver:
    def __init__(self):
        self.lock = threading.RLock()
        self.units = {}
        self.pages = {}
        self.observed = False

    def register_document(self, document):
        with self.lock:
            self.observed = True
            for index, page in enumerate(document.page):
                for paragraph in page.pdf_paragraph:
                    if paragraph.debug_id:
                        self.pages.setdefault(paragraph.debug_id, index + 1)

    def record(self, paragraph, state=None, fallback=False, structure=None, reason=None):
        with self.lock:
            key = id(paragraph)
            # Retain the object for this process so Python cannot recycle its identity.
            unit = self.units.setdefault(
                key,
                {
                    "object": paragraph,
                    "state": "pending",
                    "fallback": False,
                    "structure": False,
                    "formula": False,
                    "reason": None,
                },
            )
            if state:
                unit["state"] = state
                unit["reason"] = reason
            unit["fallback"] |= fallback
            if structure is not None:
                unit["structure"] = structure

    def summary(self):
        with self.lock:
            units = list(self.units.values())
            counts = {
                state: sum(u["state"] == state for u in units)
                for state in ("applied", "retained", "skipped", "pending")
            }
            review = [u for u in units if u["state"] in ("pending", "retained") or u["structure"]]
            pages = sorted(
                {self.pages[p.debug_id] for u in review if (p := u["object"]).debug_id in self.pages}
            )
            status = "needs_review" if review else "checks_passed" if counts["applied"] else "not_checked"
            return {
                "version": "babeldoc-0.6.4-units-v3",
                "scope": "engine_fragments",
                "status": status,
                "observed": self.observed,
                "processedUnits": len(units),
                "appliedUnits": counts["applied"],
                "retainedUnits": counts["retained"],
                "skippedUnits": counts["skipped"],
                "unresolvedUnits": counts["pending"],
                "fallbackUnits": sum(u["fallback"] for u in units),
                "reviewPages": pages,
                "structureWarningUnits": sum(u["structure"] for u in units),
                "formulaProtectedUnits": sum(u["formula"] and u["state"] == "retained" for u in units),
                "unlocatedReviewUnits": sum(u["object"].debug_id not in self.pages for u in review),
                "retainedReasons": {
                    reason: sum(u["state"] == "retained" and u["reason"] == reason for u in units)
                    for reason in sorted(
                        {u["reason"] for u in units if u["state"] == "retained" and u["reason"]}
                    )
                },
            }

    def saved_units(self):
        """Local task-side evidence for repair; never included in public metrics or stdout."""
        with self.lock:
            return [
                {
                    "input": u["input"],
                    "output": u["output"],
                    "state": u["state"],
                    "page": self.pages.get(u["object"].debug_id),
                }
                for u in self.units.values()
                if "input" in u and "output" in u
            ]

    @contextmanager
    def instrument(self):
        from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
        from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly

        original_pre = ILTranslator.pre_translate_paragraph
        original_post = ILTranslator.post_translate_paragraph
        original_single = ILTranslator.translate_paragraph
        original_translate = ILTranslatorLLMOnly.translate
        observer = self

        @wraps(original_translate)
        def translate(engine, document, *args, **kwargs):
            observer.register_document(document)
            return original_translate(engine, document, *args, **kwargs)

        @wraps(original_pre)
        def pre(engine, paragraph, *args, **kwargs):
            observer.record(paragraph, "pending")
            result = original_pre(engine, paragraph, *args, **kwargs)
            if result[0] is None:
                observer.record(paragraph, "skipped")
            return result

        @wraps(original_post)
        def post(engine, paragraph, tracker, translate_input, translated_text):
            observer.record(paragraph, "pending")
            with observer.lock:
                observer.units[id(paragraph)].update(
                    input=getattr(translate_input, "unicode", paragraph.unicode), output=translated_text
                )
            unchanged = translated_text == paragraph.unicode
            warning = placeholders_differ(translate_input, translated_text)
            formula_pattern = getattr(engine, "_formula_placeholder_pattern", None)
            if formula_pattern is not None:
                # Include invented marker IDs, not only markers known to this input.
                warning |= sorted(formula_pattern.findall(getattr(translate_input, "unicode", ""))) != sorted(
                    formula_pattern.findall(translated_text)
                )
            original_text = paragraph.unicode
            original_compositions = getattr(paragraph, "pdf_paragraph_composition", None)
            formulas = [
                p.formula for p in getattr(translate_input, "placeholders", []) if hasattr(p, "formula")
            ]

            def retain(reason):
                paragraph.unicode = original_text
                if original_compositions is not None:
                    paragraph.pdf_paragraph_composition = original_compositions
                observer.record(paragraph, "retained", structure=True, reason=reason)
                with observer.lock:
                    observer.units[id(paragraph)]["formula"] = bool(formulas)
                return False

            if warning:
                return retain("placeholder_structure")
            removed_before = sum(getattr(tracker, "removed_hallucinated_placeholders", {}).values())
            try:
                result = original_post(engine, paragraph, tracker, translate_input, translated_text)
            except Exception:
                paragraph.unicode = original_text
                if original_compositions is not None:
                    paragraph.pdf_paragraph_composition = original_compositions
                raise
            restored = [
                c.pdf_formula for c in getattr(paragraph, "pdf_paragraph_composition", []) if c.pdf_formula
            ]
            # A marker nested inside a style tag can be consumed by upstream's rich-text
            # parser. Verify the actual restored formula objects, not just marker counts.
            expected = [p.formula for p in ordered_formula_placeholders(translate_input, translated_text)]
            if [id(f) for f in restored] != [id(f) for f in expected]:
                return retain("formula_restore")
            removed_after = sum(getattr(tracker, "removed_hallucinated_placeholders", {}).values())
            observer.record(
                paragraph,
                "retained" if result is False or unchanged else "applied",
                structure=warning or removed_after > removed_before,
                reason="unchanged_output" if unchanged else "engine_retained" if result is False else None,
            )
            return result

        @wraps(original_single)
        def single(engine, paragraph, *args, **kwargs):
            observer.record(paragraph, "pending", fallback=engine.use_as_fallback)
            # The original catches some failures. Lack of a completed post remains pending.
            return original_single(engine, paragraph, *args, **kwargs)

        # Only used in a single-job subprocess; restore even on cancellation or failure.
        ILTranslator.pre_translate_paragraph = pre
        ILTranslator.post_translate_paragraph = post
        ILTranslator.translate_paragraph = single
        ILTranslatorLLMOnly.translate = translate
        try:
            yield self
        finally:
            ILTranslator.pre_translate_paragraph = original_pre
            ILTranslator.post_translate_paragraph = original_post
            ILTranslator.translate_paragraph = original_single
            ILTranslatorLLMOnly.translate = original_translate


def placeholders_differ(prepared, translated: str) -> bool:
    """Compare only engine-generated marker shapes, using upstream's own regex patterns."""
    for placeholder in getattr(prepared, "placeholders", []):
        patterns = (
            [placeholder.regex_pattern]
            if hasattr(placeholder, "regex_pattern")
            else [placeholder.left_regex_pattern, placeholder.right_regex_pattern]
        )
        for pattern in patterns:
            if len(re.findall(pattern, prepared.unicode, re.IGNORECASE)) != len(
                re.findall(pattern, translated, re.IGNORECASE)
            ):
                return True
        if len(patterns) == 2 and not re.search(
            f"{patterns[0]}.*?{patterns[1]}", translated, re.IGNORECASE | re.DOTALL
        ):
            return True
    # Grammar can reorder independent inline variables (e.g. r and o_j). Keep
    # complex formulas as fixed boundaries; variables may not cross them.
    return formula_segments(prepared, getattr(prepared, "unicode", "")) != formula_segments(
        prepared, translated
    )


def ordered_formula_placeholders(prepared, text):
    formulas = [p for p in getattr(prepared, "placeholders", []) if hasattr(p, "formula")]
    return [
        formulas[index]
        for _, index in sorted(
            (match.start(), index)
            for index, p in enumerate(formulas)
            for match in re.finditer(p.regex_pattern, text, re.I)
        )
    ]


def is_inline_variable(formula):
    # Accept plain, primed and subscript identifiers, never operators, fractions,
    # powers or drawn formulas. Missing character metadata stays conservative.
    chars = list(getattr(formula, "pdf_character", []))
    # The PDF parser can include a surrounding space in a variable object.
    # Ignore it for classification only; the original object is never edited.
    while chars and (getattr(chars[0], "char_unicode", "") or "").isspace():
        chars.pop(0)
    while chars and (getattr(chars[-1], "char_unicode", "") or "").isspace():
        chars.pop()
    text = "".join(getattr(c, "char_unicode", "") or "" for c in chars)
    if getattr(formula, "pdf_curve", []) or not re.fullmatch(
        r"[A-Za-z\u0370-\u03ff][A-Za-z0-9\u0370-\u03ff'′″‴]*", text
    ):
        return False
    # Superscripts carry mathematical meaning; subscripts in identifiers may move
    # with their original formula object, but x squared must not be treated as x2.
    baseline = chars[0].box
    return (
        baseline is not None
        and baseline.y2 is not None
        and all(
            c.box is not None
            and c.box.y2 is not None
            and (c.char_unicode in "'′″‴" or c.box.y2 <= baseline.y2 + 0.5)
            for c in chars[1:]
        )
    )


def formula_segments(prepared, text):
    segments = []
    variables = []
    for placeholder in ordered_formula_placeholders(prepared, text):
        if is_inline_variable(placeholder.formula):
            variables.append(placeholder.regex_pattern)
        else:
            segments.extend([tuple(sorted(variables)), placeholder.regex_pattern])
            variables = []
    segments.append(tuple(sorted(variables)))
    return segments
