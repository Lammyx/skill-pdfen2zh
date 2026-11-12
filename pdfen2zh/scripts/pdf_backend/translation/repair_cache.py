"""Read individual translations across old BabelDOC batch boundaries, without modifying old cache rows."""

import json
import re
from collections import defaultdict

INPUT_MARKER = "## Here is the input:"


def parse_prompt(prompt):
    prefix, marker, body = prompt.rpartition(INPUT_MARKER)
    if not marker:
        return None
    items = json.loads(body.strip())
    if not isinstance(items, list) or any(not isinstance(x.get("input"), str) for x in items):
        return None
    title = re.search(r"First title in full text: ([^\n]+)", prefix)
    # Only title context may change after repairing layout. Rules, glossary and role must match.
    context = re.sub(r"## Contextual Hints for Better Translation\n.*?(?=\n## |\Z)", "", prefix, flags=re.S)
    return " ".join(context.split()), title.group(1).strip() if title else None, items


def parse_outputs(text):
    clean = text.strip()
    for left, right in (("```json", "```"), ("```", "```"), ("<json>", "</json>")):
        if clean.startswith(left) and clean.endswith(right):
            clean = clean[len(left) : -len(right)].strip()
    rows = json.loads(clean)
    if isinstance(rows, dict):
        rows = [rows]
    result = {}
    for item in rows:
        key, output = int(item["id"]), item.get("output", item.get("input"))
        if key in result or not isinstance(output, str):
            raise ValueError("Ambiguous cached output")
        result[key] = output
    return result


class RepairCache:
    def __init__(self, provider, source_lang, target_lang, aliases, manifest=None, rows=None):
        self.provider = provider
        self.source_lang, self.target_lang = source_lang, target_lang
        self.aliases = set(aliases)
        self.by_unit = defaultdict(set)
        self.by_prompt = defaultdict(set)
        self.by_group = defaultdict(list)
        if manifest:
            for unit in manifest:
                if isinstance(unit.get("input"), str) and isinstance(unit.get("output"), str):
                    self.by_unit[unit["input"]].add(unit["output"])
        if rows is None:
            from babeldoc.translator.cache import _TranslationCache

            rows = _TranslationCache.select().where(_TranslationCache.translate_engine == "openai").iterator()
        for row in rows:
            try:
                params = json.loads(row.translate_engine_params)
                if any(
                    params.get(k) != v
                    for k, v in {
                        "reader_endpoint": provider.base_url,
                        "model": provider.model,
                        "lang_in": source_lang,
                        "lang_out": target_lang,
                    }.items()
                ):
                    continue
                # Cheap filter before parsing any unrelated paper's cache content.
                if not any(
                    f"First title in full text: {title}\n" in row.original_text for title in self.aliases
                ):
                    continue
                parsed = parse_prompt(row.original_text)
                if not parsed or parsed[1] not in self.aliases:
                    continue
                context, _, items = parsed
                outputs = parse_outputs(row.translation)
                batch_texts = frozenset(item["input"] for item in items)
                for item in items:
                    if (output := outputs.get(int(item["id"]))) is not None:
                        self.by_prompt[(context, item["input"])].add(output)
                        self.by_group[(context, item["input"])].append((output, batch_texts))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue

    def candidates(self, prompt, text, neighbors=None):
        parsed = parse_prompt(prompt)
        candidates = self.by_unit.get(text)
        if not candidates and parsed:
            key = (parsed[0], text)
            candidates = self.by_prompt.get(key)
            if neighbors and candidates and len(candidates) > 1:
                scored = [(len((group - {text}) & neighbors), value) for value, group in self.by_group[key]]
                best = max(score for score, _ in scored)
                if best > 0:
                    candidates = {value for score, value in scored if score == best}
        return sorted(candidates or [])
