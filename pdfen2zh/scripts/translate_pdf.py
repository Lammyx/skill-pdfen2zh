import argparse
import asyncio
import json
import os
import sys


def parser():
    cli = argparse.ArgumentParser(description="保留版式的英文 PDF 中文翻译与按页修复")
    cli.add_argument("--cache-dir", help="BabelDOC 资源/片段缓存目录；默认使用其用户缓存")
    commands = cli.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="离线检查依赖与已有资源，不下载或调用模型")
    doctor.add_argument("--packages-only", action="store_true")
    warmup = commands.add_parser("warmup", help="显式下载并检查布局/字体资源，不调用翻译模型")
    warmup.add_argument("--timeout", type=float, default=600)
    for name in ("translate", "repair"):
        task = commands.add_parser(name)
        task.add_argument("--output", required=True, help="输出根目录；每次建立独立任务子目录")
        task.add_argument("--base-url", help="OpenAI-compatible 地址；也可用 PDF_TRANSLATE_BASE_URL")
        task.add_argument("--model", help="模型名；也可用 PDF_TRANSLATE_MODEL")
        task.add_argument("--timeout", type=float, default=3600)
        if name == "translate":
            task.add_argument("--input", required=True)
            task.add_argument("--glossary", help="source/target 术语 JSON 数组")
            task.add_argument(
                "--sections",
                nargs="+",
                choices=("abstract", "body", "appendix"),
                default=["abstract", "body", "appendix"],
            )
            task.add_argument("--document-mode", choices=("digital", "table", "ocr"), default="digital")
        else:
            task.add_argument("--manifest", required=True, help="本技能基准任务的 manifest.json")
            task.add_argument("--pages", required=True, help="原文物理页码，例如 1,3-5")
            task.add_argument(
                "--max-new-requests", type=int, default=0, help="默认 0，仅重放缓存；显式正整数允许有限补译"
            )
            task.add_argument("--layout-only", action="store_true", help="只修复空白标签与页眉，0 模型请求")
    return cli


def main():
    args = parser().parse_args()
    if args.cache_dir:
        os.environ["PDF_TRANSLATE_CACHE_DIR"] = args.cache_dir
    try:
        if args.command in ("doctor", "warmup"):
            from pdf_backend.resources import doctor, warmup

            result = (
                doctor(not args.packages_only)
                if args.command == "doctor"
                else asyncio.run(warmup(args.timeout))
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return (
                0
                if result["engineCompatible"]
                and result["dependenciesReady"]
                and (args.command == "doctor" and args.packages_only or result["ready"])
                else 1
            )
        if not 5 <= args.timeout <= 86400:
            raise ValueError("任务超时须为 5–86400 秒")
        from pdf_backend.pipeline import repair, translate

        asyncio.run(translate(args) if args.command == "translate" else repair(args))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "error": "用户取消"}, ensure_ascii=False), file=sys.stderr)
        return 130
    except (ValueError, OSError, TimeoutError, ImportError) as exc:
        message = str(exc)
        secret = os.environ.get("PDF_TRANSLATE_API_KEY")
        if secret:
            message = message.replace(secret, "[redacted]")
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
