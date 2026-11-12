"""One BabelDOC translation per process; framed events on stdout."""

import asyncio
import contextlib
import csv
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

from ..ai.generation import GenerationPolicy
from ..ai.provider import ProviderConfig
from ..ai.usage import UsageRepository
from .errors import AssetSetupError, error_report
from .protocol import VERSION, EventWriter
from .settings import ACADEMIC_PROMPT


async def run(config: dict, emit):
    from ..resources import configure_cache

    configure_cache()
    from .quality import QualityObserver
    from .rotated_text import RotatedTextGuard
    from .sections import ALL_SECTIONS, SectionFilter

    observer = QualityObserver()
    rotations = RotatedTextGuard()
    scope = SectionFilter((config.get("options") or {}).get("sections", ALL_SECTIONS))
    binding = config.get("usageLedger")
    repository = UsageRepository(binding["path"], recover=False) if binding else None
    repair_options = (config.get("options") or {}).get("repair")
    repair = None
    if repair_options:
        from .repair import RepairSession

        manifest_path = Path(repair_options["unitsPath"])
        manifest = None
        if manifest_path.is_file():
            try:
                saved = json.loads(manifest_path.read_text(encoding="utf-8"))
                if saved.get("identity") == unit_cache_identity(config):
                    manifest = saved["units"]
            except (ValueError, KeyError):
                pass
        repair = RepairSession(
            repair_options,
            scope,
            observer,
            ProviderConfig(**config["provider"]),
            config["target_lang"],
            manifest,
        )
    try:
        with (
            rotations.instrument(),
            observer.instrument(),
            repair.instrument() if repair else contextlib.nullcontext(),
            scope.instrument(),
        ):
            await run_observed(config, emit, observer, scope, repository, repair, rotations)
    finally:
        if repository:
            repository.close()


def unit_cache_identity(config):
    return {
        "version": 1,
        "sourceHash": hashlib.sha256(Path(config["source"]).read_bytes()).hexdigest(),
        "provider": ProviderConfig(**config["provider"]).identity(),
        "target": config["target_lang"],
        "options": {
            k: v
            for k, v in (config.get("options") or {}).items()
            if k
            not in ("repair", "formulaProtection", "headingRepair", "rotatedTextPolicy", "runningTitlePolicy")
        },
    }


def apply_manual_priority(translation_config):
    """Pinned 0.6.4 otherwise uses only the auto glossary after extraction."""
    from babeldoc.glossary import Glossary

    shared = translation_config.shared_context_cross_split_part

    def combined(auto_extract_enabled):
        # User tables precede automatic entries; Glossary deduplicates normalized sources.
        entries = [entry for glossary in shared.get_glossaries() for entry in glossary.entries]
        return [Glossary("reader_terms", entries)] if entries else []

    shared.get_glossaries_for_translation = combined


async def run_observed(config: dict, emit, observer, scope, repository=None, repair=None, rotations=None):
    emit({"type": "progress", "stage": "加载 BabelDOC（首次可能较慢）", "progress": 0})
    from babeldoc.format.pdf.high_level import async_translate, init
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.glossary import Glossary, GlossaryEntry
    from babeldoc.translator.translator import set_translate_rate_limiter

    from .openai_adapter import ReaderOpenAITranslator

    init()
    provider = config["provider"]
    options = config.get("options") or {}
    glossary = options.get("glossary") or {}

    def record_usage(entry):
        if repository:
            # Commit the start before sending HTTP; a lost stdout event cannot lose this attempt.
            repository.record(config["usageLedger"]["taskId"], entry)
        emit({"type": "usage", "entry": entry, "persisted": repository is not None})

    translator = ReaderOpenAITranslator(
        ProviderConfig(**provider),
        config["target_lang"],
        replace(GenerationPolicy.from_env(), max_retries=0) if repair else GenerationPolicy.from_env(),
        options.get("sourceLang", "en"),
        usage_emit=record_usage,
    )
    if repair:
        translator.request_gate = repair.check_request
    set_translate_rate_limiter(2)
    emit({"type": "progress", "stage": "检查布局模型与字体资源（缺失时下载）", "progress": 0})
    try:
        table_model = None
        if options.get("documentMode") == "table":
            from babeldoc.docvision.table_detection.rapidocr import RapidOCRModel

            table_model = RapidOCRModel()
        translation_config = TranslationConfig(
            translator=translator,
            input_file=Path(config["source"]),
            output_dir=Path(config["output"]),
            lang_in=options.get("sourceLang", "en"),
            lang_out=config["target_lang"],
            doc_layout_model=None,
            qps=2,
            pool_max_workers=2,
            debug=False,
            auto_extract_glossary=options.get("autoExtractGlossary", False),
            glossaries=[
                Glossary(
                    "project_terms",
                    [
                        GlossaryEntry(entry["source"], entry["target"], config["target_lang"])
                        for entry in glossary.get("entries", [])
                    ],
                )
            ]
            if glossary.get("entries")
            else None,
            custom_system_prompt=ACADEMIC_PROMPT if options.get("academicStyle") else None,
            min_text_length=options.get("minTextLength", 5),
            skip_formula_offset_calculation=options.get("formulaMode") == "no_offset",
            table_model=table_model,
            ocr_workaround=options.get("documentMode") == "ocr",
            watermark_output_mode=WatermarkOutputMode.NoWatermark,
            no_dual=False,
            no_mono=False,
            only_include_translated_page=False,
        )
        if options.get("autoExtractGlossary") and glossary.get("entries"):
            apply_manual_priority(translation_config)
    except Exception as exc:
        raise AssetSetupError() from exc
    stream = async_translate(translation_config)
    pending = None
    while True:
        if pending is None:
            pending = asyncio.create_task(anext(stream))
        done, _ = await asyncio.wait([pending], timeout=2)
        if translator.activity.error:
            # BabelDOC may catch paragraph exceptions and otherwise produce a partial PDF.
            # Report the fatal provider failure immediately; the parent reaps this process.
            emit(error_report(translator.activity.error))
            translation_config.cancel_translation()
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
            return
        if not done:
            emit({"type": "heartbeat", "requests": translator.activity.snapshot()})
            continue
        try:
            event = pending.result()
        except StopAsyncIteration:
            break
        pending = None
        if event["type"] == "error":
            if isinstance(event.get("error"), Exception):
                raise event["error"]
            raise RuntimeError("BabelDOC error")
        if event["type"] == "finish":
            result = event["translate_result"]
            if repair and rotations:
                # Restoring a dropped chart label is useful even when every normal
                # paragraph was skipped. Existing page-blocking checks still win.
                repair.applied.update(rotations.restored.keys() & repair.pages)
            (Path(config["source"]).parent / "translation-units.json").write_text(
                json.dumps(
                    {"identity": unit_cache_identity(config), "units": observer.saved_units()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            terms_count = 0
            if options.get("autoExtractGlossary") or glossary.get("entries"):
                combined = translation_config.shared_context_cross_split_part.get_glossaries_for_translation(
                    options.get("autoExtractGlossary", False)
                )
                terms = [entry for table in combined for entry in table.entries]
                terms_count = len(terms)
                with (Path(config["output"]) / "glossary.csv").open(
                    "w", encoding="utf-8-sig", newline=""
                ) as output:
                    writer = csv.writer(output)
                    writer.writerow(["source", "target", "tgt_lng"])
                    for entry in terms:
                        # Spreadsheet programs must treat imported model output as text.
                        cells = [entry.source, entry.target, config["target_lang"]]
                        writer.writerow(
                            [
                                "'" + cell if cell.startswith(("=", "+", "-", "@", "\t", "\r")) else cell
                                for cell in cells
                            ]
                        )
            emit(
                {
                    "type": "finish",
                    "mono": str(result.mono_pdf_path) if result.mono_pdf_path else None,
                    "dual": str(result.dual_pdf_path) if result.dual_pdf_path else None,
                    "quality": rotations.quality(
                        observer.summary(), repair.summary()["replacedPages"] if repair else None
                    )
                    if rotations
                    else observer.summary(),
                    "translationScope": scope.report,
                    "glossaryEntries": terms_count,
                    **(
                        {
                            "repair": {
                                **repair.summary(),
                                "newRequests": translator.activity.snapshot()["started"],
                            }
                        }
                        if repair
                        else {}
                    ),
                }
            )
            break
        if event["type"] in ("progress_start", "progress_update", "progress_end"):
            emit(
                {
                    "type": "progress",
                    "stage": str(event.get("stage", "翻译")),
                    "progress": float(event.get("overall_progress", 0)),
                    "stageCurrent": event.get("stage_current", 0),
                    "stageTotal": event.get("stage_total", 0),
                    "requests": translator.activity.snapshot(),
                }
            )


def main():
    output = sys.stdout
    config = json.loads(sys.stdin.readline())
    emit = EventWriter(output, config["protocolToken"]).emit
    try:
        emit({"type": "hello", "version": VERSION})
        # Keep imported library prints out of the NDJSON channel.
        with contextlib.redirect_stdout(sys.stderr):
            asyncio.run(run(config, emit))
    except Exception as exc:
        emit(error_report(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
