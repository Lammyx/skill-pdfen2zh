import json
from pathlib import Path

from .ai.generation import GenerationPolicy
from .ai.provider import ProviderConfig
from .translation.rotated_text import ROTATED_TEXT_VERSION
from .translation.sections import SECTION_VERSION
from .translation.settings import ACADEMIC_PROMPT_VERSION
from .translation.title_fragments import TITLE_REPAIR_VERSION


def provider_from_args(args):
    saved = ProviderConfig.from_env()
    provider = ProviderConfig(args.base_url or saved.base_url, args.model or saved.model, saved.api_key)
    provider.validate()
    return provider


def load_glossary(path):
    if path is None:
        return None
    values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(values, list):
        raise ValueError('术语文件必须是 [{"source":"...","target":"..."}] JSON 数组')
    entries = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or not all(
            isinstance(value.get(k), str) and value[k].strip() for k in ("source", "target")
        ):
            raise ValueError("术语 source/target 必须是非空字符串")
        source, target = value["source"].strip(), value["target"].strip()
        key = source.casefold()
        if key in seen:
            raise ValueError("术语原词重复，请先合并冲突译法")
        seen.add(key)
        entries.append({"source": source, "target": target})
    return {"entries": entries}


def translation_settings(args):
    return {
        "sourceLang": "en",
        "academicStyle": True,
        "autoExtractGlossary": False,
        "glossary": load_glossary(args.glossary),
        "sections": args.sections,
        "documentMode": args.document_mode,
        "minTextLength": 5,
        "formulaMode": "auto",
        "promptVersion": ACADEMIC_PROMPT_VERSION,
        "formulaProtection": "markers-and-compositions-v4-primed-identifiers",
        "headingRepair": TITLE_REPAIR_VERSION,
        "rotatedTextPolicy": ROTATED_TEXT_VERSION,
        "runningTitlePolicy": "repeated-title-v1",
        "sectionPolicy": SECTION_VERSION,
    }


def generation_snapshot():
    from dataclasses import asdict

    return asdict(GenerationPolicy.from_env())


def restore_generation(values):
    import os

    for field, suffix in {
        "request_timeout": "REQUEST_TIMEOUT_SECONDS",
        "max_output_tokens": "MAX_OUTPUT_TOKENS",
        "max_retries": "MAX_RETRIES",
        "thinking": "THINKING",
    }.items():
        os.environ["PDF_TRANSLATE_" + suffix] = str(values[field])


def parse_pages(text, count):
    pages = set()
    try:
        for value in text.split(","):
            bounds = value.strip().split("-")
            if len(bounds) == 1:
                pages.add(int(bounds[0]))
            elif len(bounds) == 2:
                first, last = map(int, bounds)
                if not 1 <= first <= last <= count:
                    raise ValueError
                pages.update(range(first, last + 1))
            else:
                raise ValueError
    except ValueError:
        raise ValueError("页码须为 1,3-5 形式，且在原文物理页数内") from None
    if not pages or min(pages) < 1 or max(pages) > count:
        raise ValueError("修复页码超出原文物理页数")
    return sorted(pages)
