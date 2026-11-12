"""Local vector repair of blank angled labels and repeated running titles.

No translator, cache or provider is used. Existing text is never painted over:
labels require an empty destination; replaced headers are actually removed.
"""

import unicodedata
from collections import defaultdict
from pathlib import Path

import pymupdf as fitz

LAYOUT_REPAIR_VERSION = "blank-labels-running-title-v1"


def text(line):
    return "".join(s["text"] for s in line["spans"])


def key(value):
    return "".join(unicodedata.normalize("NFKC", value).split())


def lines(page):
    return [line for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])]


def oblique(line):
    x, y = line["dir"]
    return (abs(y) > 0.002 or x < 0) and not (abs(x) < 0.002 and y < 0)


def regions(rectangles):
    result = []
    for rect in rectangles:
        rect = fitz.Rect(rect) + (-0.4, -0.4, 0.4, 0.4)
        while True:
            overlap = next((r for r in result if r.intersects(rect)), None)
            if overlap is None:
                break
            result.remove(overlap)
            rect |= overlap
        result.append(rect)
    return result


def empty(page, rect):
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, colorspace=fitz.csRGB, alpha=False)
    return bool(pix.samples) and min(pix.samples) >= 248


def clip_layer(document, index, rect, labels_only=False):
    """Keep only text within the clip, so PDF.js cannot find hidden copied body text."""
    layer = fitz.open()
    layer.insert_pdf(document, from_page=index, to_page=index)
    page = layer[0]
    if labels_only:
        for line in lines(page):
            if not oblique(line) and rect.intersects(fitz.Rect(line["bbox"])):
                page.add_redact_annot(line["bbox"], fill=False)
    whole = page.rect
    for outside in (
        fitz.Rect(whole.x0, whole.y0, whole.x1, rect.y0),
        fitz.Rect(whole.x0, rect.y1, whole.x1, whole.y1),
        fitz.Rect(whole.x0, rect.y0, rect.x0, rect.y1),
        fitz.Rect(rect.x1, rect.y0, whole.x1, rect.y1),
    ):
        if not outside.is_empty:
            page.add_redact_annot(outside, fill=False)
    page.apply_redactions(images=0, graphics=0, text=0)
    return layer


def clear_under_layer(page, layer, rect):
    if empty(page, rect):
        return True
    top = layer[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, colorspace=fitz.csRGB, alpha=True)
    bottom = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, colorspace=fitz.csRGB, alpha=False)
    if (top.width, top.height) != (bottom.width, bottom.height):
        return False
    rgba, rgb = top.samples, bottom.samples
    return all(
        rgba[i * 4 + 3] < 8 or min(rgb[i * 3 : i * 3 + 3]) >= 248 for i in range(top.width * top.height)
    )


def running_title(source, target):
    groups = defaultdict(list)
    for index, page in enumerate(source):
        for line in lines(page):
            if len(key(text(line))) >= 16 and line["bbox"][3] < page.rect.height * 0.12:
                groups[key(text(line))].append((index, line))
    eligible = [group for group in groups.values() if len({i for i, _ in group}) >= max(3, len(source) // 2)]
    if not eligible:
        return None, {}
    group = max(eligible, key=lambda group: len({i for i, _ in group}))
    matches = {}
    candidates = []
    original_key = key(text(group[0][1]))
    for index, original in group:
        band = fitz.Rect(original["bbox"]) + (-2, -3, 2, 1)
        matched = [
            line
            for line in lines(target[index])
            if abs(line["bbox"][1] - band.y0) < 6 and line["bbox"][3] <= band.y1 + 2
        ]
        if len(matched) == 1:
            matches[index] = matched[0]
            if key(text(matched[0])) != original_key:
                candidates.append((index, matched[0]))
    # One already-rendered translation, earliest physical page, for the same source
    # running title. Never pick a different section heading or invent a translation.
    return (candidates[0] if candidates else None), matches


def repair_layout(source_path, base_path, output_path, pages, *, strict=True):
    selected = set(pages)
    report = {"restoredLabels": 0, "labelPages": [], "headerPages": [], "skippedRegions": 0}
    with fitz.open(source_path) as source, fitz.open(base_path) as base, fitz.open(base_path) as result:
        if len(source) != len(base):
            raise ValueError("译文页数不一致，不能本地修复排版")
        for number in tuple(selected):
            if not 1 <= number <= len(source):
                raise ValueError("本地修复页码越界")
            a, b = source[number - 1], base[number - 1]
            if a.rotation or b.rotation or max(abs(x - y) for x, y in zip(a.rect, b.rect)) > 0.1:
                if strict:
                    raise ValueError("此页面的旋转或尺寸不支持本地排版修复")
                selected.remove(number)
        canonical, headers = running_title(source, base)
        for number in sorted(selected):
            index = number - 1
            original_lines = lines(source[index])
            tilted = [line for line in original_lines if oblique(line)]
            for rect in regions(line["bbox"] for line in tilted):
                rect &= source[index].rect
                if rect.is_empty:
                    report["skippedRegions"] += 1
                    continue
                expected = [key(text(line)) for line in tilted if rect.contains(fitz.Rect(line["bbox"]))]
                with clip_layer(source, index, rect, labels_only=True) as layer:
                    available = [key(text(line)) for line in lines(layer[0])]
                    if sorted(available) != sorted(expected) or not clear_under_layer(
                        base[index], layer, rect
                    ):
                        report["skippedRegions"] += 1
                        continue
                    result[index].show_pdf_page(rect, layer, 0, clip=rect, keep_proportion=False)
                report["restoredLabels"] += len(expected)
                report["labelPages"].append(number)
            if canonical and index in headers:
                canonical_index, canonical_line = canonical
                clip = fitz.Rect(canonical_line["bbox"]) + (-0.3, -0.3, 0.3, 0.3)
                current = headers[index]
                destination = fitz.Rect(clip)
                destination.x0 = (result[index].rect.width - clip.width) / 2
                destination.x1 = destination.x0 + clip.width
                if (
                    key(text(current)) == key(text(canonical_line))
                    and abs(current["bbox"][0] - destination.x0 - 0.3) < 0.5
                ):
                    continue
                result[index].add_redact_annot(
                    fitz.Rect(current["bbox"]) + (-0.2, -0.2, 0.2, 0.2), fill=False
                )
                result[index].apply_redactions(images=0, graphics=0, text=0)
                with clip_layer(base, canonical_index, clip) as layer:
                    result[index].show_pdf_page(destination, layer, 0, clip=clip, keep_proportion=False)
                report["headerPages"].append(number)
        report["labelPages"] = sorted(set(report["labelPages"]))
        report["changedPages"] = sorted(set(report["labelPages"]) | set(report["headerPages"]))
        report["headerText"] = text(canonical[1]) if canonical else None
        if report["changedPages"]:
            result.save(output_path, garbage=3, deflate=True)
    return report


def update_dual(base_path, mono_path, output_path, pages):
    from pypdf import PdfReader, PdfWriter

    with fitz.open(base_path) as old, fitz.open(mono_path) as mono, fitz.open() as replacement:
        if len(old) != len(mono):
            raise ValueError("双语译文不是并排逐页格式")
        for number in sorted(pages):
            index = number - 1
            original, translated = old[index].rect, mono[index].rect
            if (
                abs(original.width - translated.width * 2) > 0.1
                or abs(original.height - translated.height) > 0.1
            ):
                raise ValueError("双语译文尺寸不支持本地修复")
            page = replacement.new_page(width=original.width, height=original.height)
            left = fitz.Rect(0, 0, translated.width, original.height)
            with clip_layer(old, index, left) as original_left:
                page.show_pdf_page(left, original_left, 0, clip=left)
            page.show_pdf_page(left + (translated.width, 0, translated.width, 0), mono, index)
        data = replacement.tobytes(garbage=3, deflate=True)
    from io import BytesIO

    additions = PdfReader(BytesIO(data))
    writer = PdfWriter(clone_from=base_path)
    for position, number in reversed(list(enumerate(sorted(pages)))):
        writer.remove_page(number - 1)
        writer.insert_page(additions.pages[position], number - 1)
    with Path(output_path).open("wb") as handle:
        writer.write(handle)
