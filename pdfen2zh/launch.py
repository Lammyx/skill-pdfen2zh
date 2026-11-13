"""Dependency-free entry point; locate the skill relative to this file."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_KEYS = {
    "PDF_TRANSLATE_BASE_URL", "PDF_TRANSLATE_MODEL", "PDF_TRANSLATE_API_KEY",
    "PDF_TRANSLATE_CACHE_DIR", "PDF_TRANSLATE_REQUEST_TIMEOUT_SECONDS",
    "PDF_TRANSLATE_MAX_OUTPUT_TOKENS", "PDF_TRANSLATE_MAX_RETRIES", "PDF_TRANSLATE_THINKING",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
}


def environment(config_path, cache_dir):
    env = dict(os.environ)
    path = Path(config_path).expanduser().resolve() if config_path else ROOT / "config.local.json"
    if config_path or path.is_file():
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or config.keys() - CONFIG_KEYS:
            raise ValueError("配置须为环境变量 JSON 对象；支持的字段见 config.example.json")
        for key, value in config.items():
            if not isinstance(value, str):
                raise ValueError(f"配置 {key} 必须是字符串")
            if value and key not in env:
                if key in {"PDF_TRANSLATE_CACHE_DIR", "SSL_CERT_FILE"}:
                    value = str((path.parent / Path(value).expanduser()).resolve())
                env[key] = value
    if cache_dir:
        env["PDF_TRANSLATE_CACHE_DIR"] = str(Path(cache_dir).expanduser().resolve())
    # Never inherit another project's environment as the uv install destination.
    env["UV_PROJECT_ENVIRONMENT"] = str(ROOT / ".venv")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def main():
    parser = argparse.ArgumentParser(description="PDF 英译中：初始化、诊断、翻译、修复与干净打包")
    parser.add_argument("--config", help="本地 JSON 配置；默认读取技能目录 config.local.json")
    parser.add_argument("--cache-dir", help="当前机器资源/片段缓存，优先于环境变量及配置文件")
    parser.add_argument("command", choices=(
        "init", "doctor", "warmup", "translate", "repair", "package",
        "agent-export", "agent-check", "agent-render",
    ))
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="子命令参数；用 <命令> --help 查看")
    args = parser.parse_args()
    if args.command == "package":
        return subprocess.call(
            [sys.executable, "-B", str(ROOT / "scripts/package_skill.py"), *args.arguments],
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    env = environment(args.config, args.cache_dir)
    runtime = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    backend = ROOT / "scripts/translate_pdf.py"
    if args.command == "init":
        init_parser = argparse.ArgumentParser(description="安装锁定依赖；--warmup 另行下载资源")
        init_parser.add_argument("--warmup", action="store_true")
        init_args = init_parser.parse_args(args.arguments)
        uv = shutil.which("uv")
        if not uv:
            raise ValueError("未找到 uv。先安装 uv 并加入 PATH，再运行 python launch.py init。")
        result = subprocess.call(
            [uv, "sync", "--project", str(ROOT), "--locked", "--no-dev", "--python", "3.11"], env=env
        )
        if result:
            return result
        command = ["warmup"] if init_args.warmup else ["doctor", "--packages-only"]
    else:
        command = [args.command, *args.arguments]
    if not runtime.is_file():
        raise ValueError(f'尚未初始化。先运行 python "{ROOT / "launch.py"}" init，再运行 warmup。')
    if command[0] != "doctor" and "--help" not in command:
        check = subprocess.run(
            [str(runtime), "-B", str(backend), "doctor", "--packages-only"],
            env=env, capture_output=True, encoding="utf-8",
        )
        if check.returncode:
            print(check.stdout, end="")
            print(check.stderr, end="", file=sys.stderr)
            raise ValueError("运行环境不完整或版本不兼容，请重新运行 init。")
    return subprocess.call([str(runtime), "-B", str(backend), *command], env=env)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (ValueError, OSError) as exc:
        # Configuration parsing errors must not echo a config file or credential value.
        if isinstance(exc, json.JSONDecodeError):
            message = "配置不是有效的 JSON；请参考 config.example.json。"
        else:
            message = str(exc)
            secret = os.environ.get("PDF_TRANSLATE_API_KEY")
            if secret:
                message = message.replace(secret, "[redacted]")
        print(message, file=sys.stderr)
        raise SystemExit(1)
