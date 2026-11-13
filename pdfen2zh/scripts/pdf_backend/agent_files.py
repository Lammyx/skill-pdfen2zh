"""File contract between a conversation translator and the PDF worker."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .ai.provider import ProviderConfig
from .workspace import digest, task_file, write_json

FORMAT = "pdfen2zh-agent-v1"
AGENT_PROVIDER = ProviderConfig("agent://local", "conversation-v1", "")
TOKENS = re.compile(r"\{v\d+\}|<style id='\d+'>|</style>")


class AgentTranslationError(ValueError):
    """File translations cannot be safely applied to the prepared PDF units."""


def unit_id(page, index, source):
    signature = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return f"p{page:04d}-u{index:04d}-{signature}"


def load_packet(manifest_path):
    path = Path(manifest_path).expanduser().resolve(strict=True)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest["schemaVersion"] != 1 or manifest["kind"] != "agent-export"
        or manifest["status"] != "awaiting_translation"
        or manifest["engine"] != "babeldoc==0.6.4"
        or manifest["adapterVersion"] != "pdf-en-to-zh-v1"
    ):
        raise ValueError("须提供本技能 agent-export 的待翻译任务清单")
    if digest(path.parent / "input.pdf") != manifest["source"]["sha256"]:
        raise ValueError("待翻译任务的输入副本已变化")
    packet_path = task_file(path.parent, manifest["agentSource"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if packet["format"] != FORMAT or packet["sourceHash"] != manifest["source"]["sha256"]:
        raise ValueError("待译单元的版本或原文散列不匹配")
    return path, manifest, packet, digest(packet_path)


def collect_translations(packet, packet_hash, paths):
    expected = {unit["id"]: unit for unit in packet["units"]}
    outputs, verbatim = {}, {}
    for path in paths:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
        if (
            not isinstance(value, dict) or value.get("format") != FORMAT
            or value.get("sourceHash") != packet["sourceHash"] or value.get("packetHash") != packet_hash
            or not isinstance(value.get("translations"), list)
        ):
            raise ValueError("译文文件须包含匹配的 format、sourceHash、packetHash 和 translations 数组")
        for row in value["translations"]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ValueError("译文条目须包含字符串 id 与 output")
            key, output = row["id"], row.get("output")
            if key not in expected:
                raise ValueError(f"译文包含未知单元：{key}")
            if key in outputs:
                raise ValueError(f"译文单元重复：{key}")
            if not isinstance(output, str):
                raise ValueError(f"译文 output 须为字符串：{key}")
            if output.strip() and (
                Counter(TOKENS.findall(output)) != Counter(TOKENS.findall(expected[key]["source"]))
            ):
                raise ValueError(f"译文公式或样式占位符不匹配：{key}")
            reason = row.get("verbatimReason")
            if reason is not None:
                if reason not in ("code", "identifier", "bibliography") or output != expected[key]["source"]:
                    raise ValueError(f"原样保留须使用有效原因且 output 与 source 完全相同：{key}")
                verbatim[key] = reason
            outputs[key] = output
    missing = [key for key in expected if not outputs.get(key, "").strip()]
    complete = {key: output for key, output in outputs.items() if output.strip()}
    return complete, {
        "totalUnits": len(expected), "translatedUnits": len(complete) - len(verbatim),
        "filledUnits": len(complete), "verbatimUnits": verbatim, "missingIds": missing,
    }


def write_batches(root, packet, packet_hash, batch_chars):
    batches, current, size = [], [], 0
    for unit in packet["units"]:
        length = len(json.dumps(unit, ensure_ascii=False))
        if current and size + length > batch_chars:
            batches.append(current)
            current, size = [], 0
        current.append(unit)
        size += length
    if current:
        batches.append(current)
    folder = root / "batches"
    folder.mkdir()
    for index, units in enumerate(batches, 1):
        header = {"format": FORMAT, "sourceHash": packet["sourceHash"], "packetHash": packet_hash}
        write_json(folder / f"{index:04d}.source.json", {
            **header, "instructions": packet["instructions"], "glossary": packet["glossary"], "units": units,
        })
        write_json(folder / f"{index:04d}.translations.json", {
            **header, "translations": [{"id": unit["id"], "output": ""} for unit in units],
        })
    return len(batches)
