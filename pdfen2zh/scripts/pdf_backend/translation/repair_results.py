"""Replace only complete repair pages, retaining the existing mono/dual as immutable bases."""

from pathlib import Path

from pypdf import PdfReader, PdfWriter


def patch_repair_pages(base, generated, output, pages, source_count):
    old, new = PdfReader(base), PdfReader(generated)
    # Native 0.6.4 side-by-side dual has one PDF page per source page.
    if len(old.pages) != source_count or len(new.pages) != source_count:
        raise ValueError("此译文不是逐页或并排双语格式，不能安全局部修复")
    writer = PdfWriter(clone_from=old)
    for number in sorted(pages, reverse=True):
        if not 1 <= number <= source_count:
            raise ValueError("修复页码越界")
        if tuple(old.pages[number - 1].mediabox) != tuple(new.pages[number - 1].mediabox):
            raise ValueError("修复页尺寸变化，已保留旧译文")
        writer.remove_page(number - 1)
        writer.insert_page(new.pages[number - 1], number - 1)
    with Path(output).open("wb") as handle:
        writer.write(handle)
