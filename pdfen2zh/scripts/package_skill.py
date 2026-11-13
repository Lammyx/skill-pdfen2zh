"""Package only maintained skill source and public documentation, never local data."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    "SKILL.md", "LICENSE", "pyproject.toml", "uv.lock", ".python-version", ".gitignore",
    "launch.py", "config.example.json", "agents/openai.yaml", "references/usage.md",
    "references/architecture.md", "references/licenses.md", "references/source-map.json",
    "references/agent-workflow.md",
)


def main():
    parser = argparse.ArgumentParser(description="白名单打包；输出 ZIP 必须位于技能目录之外")
    parser.add_argument("--output", required=True, help="新 ZIP 路径；不覆盖已有文件")
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    if output.is_relative_to(ROOT) or output.suffix.lower() != ".zip":
        parser.error("输出必须是技能目录之外的 .zip 文件")
    files = [ROOT / name for name in PUBLIC_FILES]
    files += sorted(
        path for path in (ROOT / "scripts").rglob("*.py")
        if not any(part.startswith(".") or part in {"__pycache__", "tests"}
                   for part in path.relative_to(ROOT / "scripts").parts)
    )
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            parser.error(f"分发文件缺失或链接到技能之外：{path.relative_to(ROOT)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "pdfen2zh/" + path.relative_to(ROOT).as_posix())
    print(json.dumps({
        "archive": str(output), "files": len(files), "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
