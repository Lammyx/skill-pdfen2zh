"""Lossless fallback for angles the pinned BabelDOC projector silently drops.

Keep these glyphs out of paragraph finding / horizontal typesetting, then restore
them before font collection and PDF writing. This is preservation, not translation.
All hooks are process-local and restored when the isolated worker exits its scope.
"""

import math
from contextlib import contextmanager
from functools import wraps

ROTATED_TEXT_VERSION = "retain-affine-glyphs-v1"
IDENTITY = (1, 0, 0, 1, 0, 0)


def unsupported_angle(matrix):
    angle = math.degrees(math.atan2(matrix[1], matrix[0]))
    return not (-0.1 <= angle <= 0.1 or 89.9 <= angle <= 90.1)


class RotatedTextGuard:
    def __init__(self):
        self.pages = {}
        self.restored = {}

    def capture(self, engine, char):
        from babeldoc.format.pdf.document_il import il_version_1 as il
        from babeldoc.format.pdf.document_il.utils.type3_font_metrics import effective_type3_font_size

        if char.aw_font_id is None:
            return
        # AWLTChar discards these three constructor values. Our scoped constructor
        # hook saves the exact values; deriving them from an axis-aligned bbox
        # changes font size, rise and horizontal scaling at oblique angles.
        size, scaling, rise = char._reader_text_parameters
        a, b, c, d, e, f = char.matrix
        values = (size, scaling, rise, a, b, c, d, e, f)
        if not all(math.isfinite(v) for v in values) or size == 0 or a * d == b * c:
            raise ValueError("斜排文字的变换无效，已停止生成以避免内容丢失")
        state = engine.create_graphic_state(char.graphicstate, include_clipping=True, target_ctm=IDENTITY)
        # Clipping is first expressed in page/XObject output coordinates. Apply
        # the glyph transform afterwards, inside CharacterRenderUnit's q/Q pair.
        matrix = (a * scaling, b * scaling, c, d, e + c * rise, f + d * rise)
        instruction = " ".join(f"{value:.12g}" for value in matrix) + " cm"
        render_mode = getattr(char, "render_mode", 0)
        if render_mode not in range(8):
            raise ValueError("斜排文字的绘制模式无效")
        # Text rendering mode is separate from the projector's graphic-state
        # snapshot. In particular, mode 3 must stay invisible (e.g. OCR layers).
        instruction += f" BT {render_mode} Tr ET"
        scope = engine.xobj_map.get(char.xobj_id, engine.current_page)
        font = next((f for f in scope.pdf_font if f.font_id == char.aw_font_id), None)
        glyph = il.PdfCharacter(
            pdf_style=il.PdfStyle(
                font_id=char.aw_font_id,
                font_size=effective_type3_font_size(font, size),
                graphic_state=il.GraphicState(
                    passthrough_per_char_instruction=f"{state.passthrough_per_char_instruction} {instruction}"
                ),
            ),
            box=il.Box(x=0, y=0, x2=char.adv, y2=size),
            pdf_character_id=char.cid,
            char_unicode=char.get_text(),
            advance=char.adv,
            vertical=False,
            xobj_id=char.xobj_id,
            render_order=char.render_order,
            sub_render_order=0,
        )
        page = engine.current_page
        self.pages.setdefault(id(page), (page, []))[1].append(glyph)

    def restore(self, document):
        for page in document.page:
            entry = self.pages.get(id(page))
            if entry is None:
                continue
            present = {id(c) for c in page.pdf_character}
            page.pdf_character.extend(c for c in entry[1] if id(c) not in present)
            self.restored[page.page_number + 1] = len(entry[1])

    def summary(self):
        return {
            "policy": "retain_original",
            "characters": sum(self.restored.values()),
            "pages": sorted(self.restored),
        }

    def quality(self, quality, pages=None):
        restored = {p: n for p, n in self.restored.items() if pages is None or p in pages}
        if not restored:
            return quality
        return {
            **quality,
            "status": "needs_review",
            "reviewPages": sorted(set(quality.get("reviewPages", [])) | restored.keys()),
            "rotatedText": {
                "policy": "retain_original",
                "characters": sum(restored.values()),
                "pages": sorted(restored),
            },
        }

    @contextmanager
    def instrument(self):
        from babeldoc.format.pdf.awlt_char import AWLTChar
        from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
        from babeldoc.format.pdf.document_il.frontend.il_creater import ILCreater
        from babeldoc.format.pdf.document_il.frontend.il_creater_active import ActiveILCreater

        original_init = AWLTChar.__init__
        original_active = ActiveILCreater.project_native_char
        original_legacy = ILCreater.on_lt_char
        original_write = PDFCreater.write
        guard = self

        @wraps(original_init)
        def init(char, matrix, font, fontsize, scaling, rise, *args, **kwargs):
            original_init(char, matrix, font, fontsize, scaling, rise, *args, **kwargs)
            char._reader_text_parameters = (fontsize, scaling, rise)

        def project(original):
            @wraps(original)
            def wrapped(engine, char):
                if unsupported_angle(char.matrix):
                    return guard.capture(engine, char)
                return original(engine, char)

            return wrapped

        @wraps(original_write)
        def write(engine, *args, **kwargs):
            guard.restore(engine.docs)
            return original_write(engine, *args, **kwargs)

        AWLTChar.__init__ = init
        ActiveILCreater.project_native_char = project(original_active)
        ILCreater.on_lt_char = project(original_legacy)
        PDFCreater.write = write
        try:
            yield self
        finally:
            AWLTChar.__init__ = original_init
            ActiveILCreater.project_native_char = original_active
            ILCreater.on_lt_char = original_legacy
            PDFCreater.write = original_write
