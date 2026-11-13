import asyncio
import copy
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from .agent_files import AGENT_PROVIDER, collect_translations, load_packet, write_batches
from .ai.generation import GenerationPolicy
from .ai.provider import ProviderConfig
from .ai.usage import UsageRepository
from .config import (
    generation_snapshot,
    parse_pages,
    provider_from_args,
    restore_generation,
    translation_settings,
)
from .translation.babeldoc import BabelDocEngine
from .workspace import Workspace, digest, load_base, task_file, write_json


def cache_directory(override=None):
    return (
        Path(override or os.environ.get("PDF_TRANSLATE_CACHE_DIR") or Path.home() / ".cache/babeldoc")
        .expanduser()
        .resolve()
    )


def postprocess(work, mono, dual, pages, strict=False):
    from .translation.layout_repair import repair_layout, update_dual

    fixed_mono, fixed_dual = work.output / "layout-mono.pdf", work.output / "layout-dual.pdf"
    layout = repair_layout(work.source, mono, fixed_mono, pages, strict=strict)
    if layout["changedPages"]:
        update_dual(dual, fixed_mono, fixed_dual, layout["changedPages"])
        return fixed_mono, fixed_dual, layout
    return mono, dual, layout


def units_path(base_path, base):
    return task_file(base_path.parent, base["units"])


async def execute(
    source,
    output,
    provider,
    settings,
    cache,
    timeout,
    *,
    base_data=None,
    pages=None,
    max_new_requests=0,
    layout_only=False,
    agent=None,
    engine=None,
    emit=None,
):
    kind = (
        "agent-" + agent["phase"] if agent else "layout-repair" if layout_only
        else "repair" if base_data else "translation"
    )
    generation = asdict(GenerationPolicy()) if provider == AGENT_PROVIDER else generation_snapshot()
    work = Workspace(source, output, kind, settings, provider, generation, cache)
    ledger = UsageRepository(work.root / "usage.sqlite3", recover=False)
    ledger.create(work.root.name, kind, provider.model)
    engine = engine or BabelDocEngine(work.root / "usage.sqlite3")
    progress = emit or (lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    final = None
    layout = None
    status = "failed"
    error = None
    try:
        if agent and agent["phase"] == "render":
            write_json(work.root / "agent-source.json", agent["packet"])
            write_json(work.root / "agent-translations.json", agent["outputs"])
            for name, filename in (
                ("agentSource", "agent-source.json"), ("agentTranslations", "agent-translations.json"),
            ):
                work.manifest[name] = {"path": filename, "sha256": digest(work.root / filename)}
            work.manifest["agentExport"] = agent["origin"]
            work.save()
        if base_data:
            base_path, base, old_paths = base_data
            work.manifest["baseManifest"] = {"path": str(base_path), "sha256": digest(base_path)}
            work.manifest["baseManifest"]["cacheDirectoryAtCreation"] = base["cacheDirectory"]
            work.manifest["requestedPages"] = pages
            work.manifest["maxNewRequests"] = max_new_requests
            work.save()
        async with asyncio.timeout(timeout):
            if layout_only:
                mono, dual, layout = await asyncio.to_thread(
                    postprocess, work, old_paths["mono"], old_paths["dual"], pages, True
                )
                # Publish accepts only task-local files, including when no repair was necessary.
                for path, name in ((mono, "local-mono.pdf"), (dual, "local-dual.pdf")):
                    shutil.copyfile(path, work.output / name)
                mono, dual = work.output / "local-mono.pdf", work.output / "local-dual.pdf"
                final = {
                    "quality": {"status": "not_checked", "scope": "layout_only"},
                    "repair": {"replacedPages": layout["changedPages"], "newRequests": 0},
                }
            else:
                options = copy.deepcopy(settings)
                if agent:
                    options["agent"] = {"phase": agent["phase"]}
                    if agent["phase"] == "render":
                        options["agent"].update(
                            packetPath=str(work.root / "agent-source.json"),
                            translationsPath=str(work.root / "agent-translations.json"),
                        )
                if base_data:
                    options["repair"] = {
                        "baseJobId": base["id"],
                        "unitsPath": str(units_path(base_path, base)),
                        "pages": pages,
                        "allowNewRequests": max_new_requests > 0,
                        "maxRequests": max_new_requests,
                    }
                async for event in engine.events(work.source, work.output, provider, "zh", options):
                    if event["type"] == "usage":
                        if not event["persisted"]:
                            ledger.record(work.root.name, event["entry"])
                    elif event["type"] == "finish":
                        final = event
                    elif event["type"] in ("progress", "heartbeat"):
                        progress(event)
                if not final or not final["mono"] or not final["dual"]:
                    raise ValueError("引擎没有提供完整中文/双语产物")
                mono, dual = Path(final["mono"]), Path(final["dual"])
                work.check_source()
                work.validate(mono, dual)
                if agent and agent["phase"] == "export":
                    packet_path = work.root / "agent-source.json"
                    packet = json.loads(packet_path.read_text(encoding="utf-8"))
                    packet_hash = digest(packet_path)
                    work.manifest["agentSource"] = {"path": packet_path.name, "sha256": packet_hash}
                    count = write_batches(work.root, packet, packet_hash, agent["batchChars"])
                    work.manifest["agentBatches"] = count
                    status = "awaiting_translation"
                    return work.root / "manifest.json"
                if base_data:
                    from .translation.repair_results import patch_repair_pages

                    replaced = final["repair"]["replacedPages"]
                    if not set(replaced).issubset(pages):
                        raise ValueError("引擎尝试替换未请求的页面")
                    merged = []
                    for key, generated in (("mono", mono), ("dual", dual)):
                        target = work.output / f"patched-{key}.pdf"
                        patch_repair_pages(
                            old_paths[key], generated, target, replaced, work.manifest["source"]["pages"]
                        )
                        merged.append(target)
                    mono, dual = merged
                mono, dual, layout = await asyncio.to_thread(
                    postprocess, work, mono, dual, pages or range(1, work.manifest["source"]["pages"] + 1)
                )
        if agent:
            task_file(work.root, work.manifest["agentSource"])
            task_file(work.root, work.manifest["agentTranslations"])
        work.publish(mono, dual)
        units = work.root / "translation-units.json"
        if base_data and layout_only and "units" in base:
            shutil.copyfile(units_path(base_path, base), units)
        elif base_data and units.is_file():
            # Keep prior-page units available when the next repair selects different pages.
            saved = json.loads(units.read_text(encoding="utf-8"))
            previous = json.loads(units_path(base_path, base).read_text(encoding="utf-8"))
            changed = set(final["repair"]["replacedPages"])
            saved["units"] = [u for u in previous["units"] if u["page"] not in changed] + [
                u for u in saved["units"] if u["page"] in changed
            ]
            write_json(units, saved)
        if units.is_file():
            work.manifest["units"] = {"path": units.name, "sha256": digest(units)}
        status = "succeeded"
        return work.root / "manifest.json"
    except asyncio.CancelledError:
        status, error = "cancelled", "已取消；已发送请求的用量见本次账本"
        raise
    except BaseException as exc:
        error = str(exc) if isinstance(exc, (ValueError, OSError, TimeoutError)) else type(exc).__name__
        if provider.api_key:
            error = error.replace(provider.api_key, "[redacted]")
        raise
    finally:
        ledger.finalize(
            work.root.name, status, capture_complete=status in ("succeeded", "awaiting_translation")
        )
        usage = ledger.summary(work.root.name)
        ledger.close()
        work.manifest["status"] = status
        report = {
            "status": status,
            "error": error,
            "usage": usage,
            "quality": final["quality"] if final else None,
            "translationScope": final.get("translationScope") if final else None,
            "repair": final.get("repair") if final else None,
            "layout": layout,
            "agent": final.get("agent") if final else None,
            "note": "片段检查不证明语义正确；斜排标签可能保留英文。",
        }
        if base_data:
            report["baselineReport"] = str(base_path.parent / "report.json")
        write_json(work.root / "report.json", report)
        work.save()
        progress(
            {
                "type": "result",
                "status": status,
                "manifest": str(work.root / "manifest.json"),
                "report": str(work.root / "report.json"),
            }
        )


async def translate(args):
    provider = provider_from_args(args)
    return await execute(
        args.input,
        args.output,
        provider,
        translation_settings(args),
        cache_directory(args.cache_dir),
        args.timeout,
    )


async def repair(args):
    if args.max_new_requests < 0 or args.max_new_requests > 500:
        raise ValueError("补译新请求上限须为 0–500")
    if args.layout_only and args.max_new_requests:
        raise ValueError("本地排版修复不能同时开启补译请求")
    base_path, base, paths = load_base(args.manifest)
    if base["provider"] == AGENT_PROVIDER.identity() and args.max_new_requests:
        raise ValueError("助手译文不能自动补发 API 请求；请修改原任务译文文件并使用 agent-render")
    pages = parse_pages(args.pages, base["source"]["pages"])
    if args.max_new_requests:
        provider = provider_from_args(args)
        if provider.identity() != base["provider"]:
            raise ValueError("补译须使用基准任务的端点与模型，避免混入不兼容缓存")
    else:
        provider = ProviderConfig(base["provider"]["endpoint"], base["provider"]["model"], "")
    restore_generation(base["generation"])
    cache = cache_directory(args.cache_dir)
    os.environ["PDF_TRANSLATE_CACHE_DIR"] = str(cache)
    return await execute(
        base_path.parent / "input.pdf",
        args.output,
        provider,
        base["settings"],
        cache,
        args.timeout,
        base_data=(base_path, base, paths),
        pages=pages,
        max_new_requests=args.max_new_requests,
        layout_only=args.layout_only,
    )


def require_agent_assets(cache):
    from .resources import doctor

    os.environ["PDF_TRANSLATE_CACHE_DIR"] = str(cache)
    if not doctor()["ready"]:
        raise ValueError("助手模式需先准备本机资源，请运行 python launch.py warmup，再执行导出或排版")


async def agent_export(args):
    if not 2000 <= args.batch_chars <= 64000:
        raise ValueError("批次目标字符预算须为 2000–64000；单个超长单元不拆分")
    cache = cache_directory(args.cache_dir)
    require_agent_assets(cache)
    return await execute(
        args.input, args.output, AGENT_PROVIDER, translation_settings(args), cache, args.timeout,
        agent={"phase": "export", "batchChars": args.batch_chars},
    )


def agent_check(args):
    _, _, packet, packet_hash = load_packet(args.manifest)
    _, coverage = collect_translations(packet, packet_hash, args.translations)
    missing = coverage.pop("missingIds")
    print(json.dumps({
        **coverage, "missingCount": len(missing), "missingIds": missing[:30], "ready": not missing,
        "note": "检查完整性与占位符，不证明译文语义正确；不调用翻译服务。",
    }, ensure_ascii=False, indent=2))
    return 1 if missing else 0


async def agent_render(args):
    path, manifest, packet, packet_hash = load_packet(args.manifest)
    outputs, coverage = collect_translations(packet, packet_hash, args.translations)
    if coverage["missingIds"]:
        raise ValueError(f'仍有 {len(coverage["missingIds"])} 个单元未翻译；先补齐译文，不会自动调用 API')
    cache = cache_directory(args.cache_dir)
    require_agent_assets(cache)
    return await execute(
        path.parent / "input.pdf", args.output, AGENT_PROVIDER, manifest["settings"], cache, args.timeout,
        agent={"phase": "render", "packet": packet, "outputs": outputs,
               "origin": {"path": str(path), "sha256": digest(path)}},
    )
