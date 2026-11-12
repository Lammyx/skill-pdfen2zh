"""Join collinear heading edge fragments before translation, retaining original glyph objects."""

import re

TITLE_REPAIR_VERSION = "collinear-title-edges-v1"


def characters(paragraph):
    result = []
    for composition in paragraph.pdf_paragraph_composition:
        if composition.pdf_character:
            result.append(composition.pdf_character)
        for name in ("pdf_line", "pdf_same_style_characters", "pdf_formula"):
            if item := getattr(composition, name, None):
                result.extend(item.pdf_character)
    return [c for c in result if c.char_unicode and not c.char_unicode.isspace()]


def line_signature(paragraph):
    chars = characters(paragraph)
    if not chars or any(not c.box or not c.pdf_style for c in chars):
        return None
    style = chars[0].pdf_style
    size = style.font_size
    if not size or any(
        c.pdf_style.font_id != style.font_id or abs((c.pdf_style.font_size or 0) - size) > 0.1 for c in chars
    ):
        return None
    if max(c.box.y for c in chars) - min(c.box.y for c in chars) > 0.5:
        return None  # No multiline or superscript/author-affiliation merges.
    return (style.font_id, size, chars[0].box.y, min(c.box.x for c in chars), max(c.box.x2 for c in chars))


def repair_title_fragments(document):
    from babeldoc.format.pdf.document_il import Box

    merged = 0
    for page in document.page:
        removed = set()
        for heading in list(page.pdf_paragraph):
            if id(heading) in removed or heading.layout_label not in {
                "title",
                "section_title",
                "section_header",
            }:
                continue
            signature = line_signature(heading)
            if not signature:
                continue
            parts = [heading]
            while True:
                found = None
                left = min(line_signature(p)[3] for p in parts)
                right = max(line_signature(p)[4] for p in parts)
                for candidate in page.pdf_paragraph:
                    if (
                        id(candidate) in removed
                        or any(candidate is p for p in parts)
                        or candidate.layout_label != "fallback_line"
                        or candidate.xobj_id != heading.xobj_id
                        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 '\-.,:]*", candidate.unicode or "")
                    ):
                        continue
                    other = line_signature(candidate)
                    if not other or other[:2] != signature[:2] or abs(other[2] - signature[2]) > 0.5:
                        continue
                    gap = max(other[3] - right, left - other[4])
                    if -0.5 <= gap <= signature[1] * 0.35:
                        found = candidate
                        break
                if found is None:
                    break
                parts.append(found)
            if len(parts) == 1:
                continue
            parts.sort(key=lambda p: line_signature(p)[3])
            text = parts[0].unicode or ""
            for previous, part in zip(parts, parts[1:]):
                gap = line_signature(part)[3] - line_signature(previous)[4]
                separator = " " if gap > signature[1] * 0.18 and not text.endswith(" ") else ""
                text += separator + (part.unicode or "")
            heading.pdf_paragraph_composition = [c for p in parts for c in p.pdf_paragraph_composition]
            heading.unicode = text
            boxes = [p.box for p in parts]
            heading.box = Box(
                min(b.x for b in boxes),
                min(b.y for b in boxes),
                max(b.x2 for b in boxes),
                max(b.y2 for b in boxes),
            )
            for part in parts:
                if part is not heading:
                    removed.add(id(part))
                    merged += 1
        page.pdf_paragraph = [p for p in page.pdf_paragraph if id(p) not in removed]
    return merged
