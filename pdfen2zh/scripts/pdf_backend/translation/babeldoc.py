import asyncio
import importlib.metadata
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psutil

from ..ai.provider import ProviderConfig
from .protocol import MAX_LINE_BYTES, EventReader, WorkerProtocolError


def kill_process_tree(pid: int):
    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            process.terminate()
        except psutil.Error:
            pass
        _, alive = psutil.wait_procs([*children, process], timeout=2)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


class BabelDocEngine:
    def __init__(self, usage_path=None):
        self.usage_path = usage_path

    def identity(self) -> dict:
        try:
            version = importlib.metadata.version("babeldoc")
        except importlib.metadata.PackageNotFoundError:
            version = None
        return {
            "engine": "babeldoc",
            "version": version,
            "supportedVersion": "0.6.4",
            "available": version == "0.6.4",
        }

    async def events(
        self,
        source: Path,
        output: Path,
        provider: ProviderConfig,
        target_lang: str,
        options: dict | None = None,
    ) -> AsyncIterator[dict]:
        if not self.identity()["available"]:
            raise ValueError(
                "需要 BabelDOC 0.6.4：运行 uv sync --extra dev --extra pdf；首次翻译还需要模型/字体资源"
            )
        if not (options or {}).get("repair") or options["repair"]["allowNewRequests"]:
            provider.validate()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        token = uuid4().hex
        channel = EventReader(token)
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pdf_backend.translation.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_LINE_BYTES,
                env=env,
                **flags,
            )
        )
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            # Cancellation can arrive while Windows is still returning the process handle.
            process = await spawn_task
            await asyncio.to_thread(kill_process_tree, process.pid)
            await process.wait()
            raise
        try:
            assert process.stdin and process.stdout
            process.stdin.write(
                (
                    json.dumps(
                        {
                            "source": str(source),
                            "output": str(output),
                            "provider": provider.__dict__,
                            "target_lang": target_lang,
                            "options": options or {},
                            "protocolToken": token,
                            "usageLedger": {"path": str(self.usage_path), "taskId": source.parent.name}
                            if self.usage_path
                            else None,
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await process.stdin.drain()
            process.stdin.close()
            finished = False
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    raise WorkerProtocolError("LINE_TOO_LARGE") from None
                if not line:
                    break
                event = channel.read(line)
                if event is None:
                    continue
                if event["type"] == "error":
                    (output.parent / "engine-diagnostic.json").write_text(
                        json.dumps(event.get("diagnostic", {}), indent=2), encoding="utf-8"
                    )
                    raise ValueError(event["message"])
                if event["type"] == "finish":
                    finished = True
                yield event
            code = await process.wait()
            channel.end()
            if code != 0 or not finished:
                raise ValueError("BabelDOC 工作进程未正常完成；检查资源预热与 Provider 配置")
        except WorkerProtocolError as exc:
            (output.parent / "engine-diagnostic.json").write_text(
                json.dumps(
                    {"code": "WORKER_PROTOCOL_ERROR", "reason": exc.reason, "protocol": channel.diagnostic()},
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise
        finally:
            if process.returncode is None:
                await asyncio.to_thread(kill_process_tree, process.pid)
                await process.wait()
            (output.parent / "worker-protocol.json").write_text(
                json.dumps(channel.diagnostic(), indent=2),
                encoding="utf-8",
            )
