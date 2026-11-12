import importlib.metadata
import os
from pathlib import Path


def configure_cache():
    from babeldoc import const

    override = os.environ.get("PDF_TRANSLATE_CACHE_DIR")
    if override:
        const.CACHE_FOLDER = Path(override).expanduser().resolve()
        const.TIKTOKEN_CACHE_FOLDER = const.CACHE_FOLDER / "tiktoken"
        const.TIKTOKEN_CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
        os.environ["TIKTOKEN_CACHE_DIR"] = str(const.TIKTOKEN_CACHE_FOLDER)
    return const.CACHE_FOLDER


def doctor(check_assets=True):
    import sys

    versions = {}
    for name in ("babeldoc", "PyMuPDF", "pypdf", "openai", "httpx", "psutil"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    result = {
        "python": sys.version.split()[0],
        "packages": versions,
        "engineCompatible": versions["babeldoc"] == "0.6.4",
    }
    result["dependenciesReady"] = all(versions.values()) and (3, 11) <= sys.version_info[:2] < (3, 14)
    if check_assets and result["engineCompatible"]:
        from babeldoc.assets.assets import verify_file
        from babeldoc.assets.embedding_assets_metadata import (
            CMAP_METADATA,
            DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
            EMBEDDING_FONT_METADATA,
            TIKTOKEN_CACHES,
        )

        cache = configure_cache()
        model = verify_file(
            cache / "models/doclayout_yolo_docstructbench_imgsz1024.onnx",
            DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
        )
        groups = {
            "fonts": {k: v["sha3_256"] for k, v in EMBEDDING_FONT_METADATA.items()},
            "cmap": {k: v["sha3_256"] for k, v in CMAP_METADATA.items()},
            "tiktoken": TIKTOKEN_CACHES,
        }
        result["assets"] = {
            name: {
                "verified": sum(verify_file(cache / name / f, sha) for f, sha in files.items()),
                "total": len(files),
            }
            for name, files in groups.items()
        }
        result.update(cacheDirectory=str(cache), modelVerified=model)
        result["ready"] = model and all(v["verified"] == v["total"] for v in result["assets"].values())
    else:
        result["ready"] = False
    return result


async def warmup(timeout):
    import asyncio

    configure_cache()
    from babeldoc.assets.assets import async_warmup

    async with asyncio.timeout(timeout):
        await async_warmup()
    return doctor()
