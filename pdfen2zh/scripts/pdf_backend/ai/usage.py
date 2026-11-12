"""Local request ledger. Never stores prompts, completions, keys or raw errors."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def normalize_usage(raw) -> dict:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raw = {}

    def number(value):
        return value if type(value) is int and value >= 0 else None

    result = {key: number(raw.get(key)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    result["totalSource"] = "provider" if result["total_tokens"] is not None else None
    if result["total_tokens"] is None and all(
        result[key] is not None for key in ("prompt_tokens", "completion_tokens")
    ):
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
        result["totalSource"] = "derived"
    prompt_details = raw.get("prompt_tokens_details") or {}
    output_details = raw.get("completion_tokens_details") or {}
    cached = number(raw.get("prompt_cache_hit_tokens"))
    result["cached_input_tokens"] = (
        cached
        if cached is not None
        else number(prompt_details.get("cached_tokens"))
        if isinstance(prompt_details, dict)
        else None
    )
    result["reasoning_tokens"] = (
        number(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    )
    # Component counters are subsets, never added a second time to total_tokens.
    return result


class UsageRecorder:
    """Emit a start BEFORE sending; finish even when received output is rejected."""

    def __init__(self, emit=None):
        self.emit = emit or (lambda entry: None)
        self.entries = {}
        self.lock = threading.RLock()

    def begin(self, operation_id=None, attempt=0):
        with self.lock:
            request_id = str(uuid4())
            entry = {
                "id": request_id,
                "operationId": operation_id or request_id,
                "attempt": attempt,
                "status": "in_flight",
                "startedAt": timestamp(),
                "finishedAt": None,
                "usage": normalize_usage(None),
                "errorCode": None,
            }
            self.entries[request_id] = entry
            self.emit(dict(entry))
            return request_id

    def finish(self, request_id, status, usage=None, error_code=None):
        with self.lock:
            entry = self.entries[request_id]
            if entry["status"] != "in_flight":
                return
            entry.update(
                status=status, finishedAt=timestamp(), usage=normalize_usage(usage), errorCode=error_code
            )
            self.emit(dict(entry))

    def cache_hit(self):
        request_id = str(uuid4())
        with self.lock:
            self.emit(
                {
                    "id": request_id,
                    "operationId": request_id,
                    "attempt": 0,
                    "status": "cache_hit",
                    "startedAt": timestamp(),
                    "finishedAt": timestamp(),
                    "usage": normalize_usage(None),
                    "errorCode": None,
                }
            )


class UsageRepository:
    def __init__(self, path, *, recover=True):
        self.path = Path(path).resolve()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 1:
            raise RuntimeError("用量账本来自更新版本")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS usage_tasks(
                id TEXT PRIMARY KEY, document_id TEXT, kind TEXT NOT NULL, model TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, capture_complete INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS usage_document ON usage_tasks(document_id,created_at);
            CREATE TABLE IF NOT EXISTS usage_requests(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES usage_tasks(id), entry TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS usage_task ON usage_requests(task_id);
            PRAGMA user_version=1;
        """)
        # A restart cannot confirm whether an unfinished transport was charged.
        if recover:
            for row in self.db.execute("SELECT id FROM usage_tasks WHERE status='active'").fetchall():
                self.finalize(row["id"], "interrupted", capture_complete=False)

    def create(self, task_id, kind, model, document_id=None):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO usage_tasks(id,document_id,kind,model,status,created_at) "
                "VALUES(?,?,?,?,'active',?)",
                (task_id, document_id, kind, model, timestamp()),
            )

    def recorder(self, task_id):
        return UsageRecorder(lambda entry: self.record(task_id, entry))

    def record(self, task_id, entry):
        # Whitelist fields crossing the worker boundary. Raw usage extensions are discarded.
        clean = {
            key: entry[key]
            for key in ("id", "operationId", "attempt", "status", "startedAt", "finishedAt", "errorCode")
        }
        if clean["status"] not in ("in_flight", "succeeded", "failed", "cancelled", "unknown", "cache_hit"):
            raise ValueError("非法用量记录状态")
        if type(clean["attempt"]) is not int or clean["attempt"] < 0:
            raise ValueError("非法重试编号")
        # Error codes are local constants, never provider exception text.
        code = clean["errorCode"]
        if code is not None and (
            not isinstance(code, str)
            or not code.replace("_", "").isalnum()
            or not code.isupper()
            or len(code) > 64
        ):
            raise ValueError("非法用量错误码")
        usage = entry.get("usage") or {}
        normalized = normalize_usage(usage)
        for key in ("cached_input_tokens", "reasoning_tokens"):
            if type(value := usage.get(key)) is int and value >= 0:
                normalized[key] = value
        if usage.get("totalSource") == "derived":
            normalized["totalSource"] = "derived"
        clean["usage"] = normalized
        with self.lock, self.db:
            previous = self.db.execute(
                "SELECT task_id,entry FROM usage_requests WHERE id=?", (clean["id"],)
            ).fetchone()
            if previous:
                old = json.loads(previous["entry"])
                if previous["task_id"] != task_id:
                    raise ValueError("用量请求不可跨任务移动")
                if old["status"] != "in_flight":
                    if old != clean:
                        raise ValueError("已确认的用量记录不可覆盖")
                    return
            self.db.execute(
                "INSERT INTO usage_requests(id,task_id,entry) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET entry=excluded.entry",
                (clean["id"], task_id, json.dumps(clean)),
            )

    def finalize(self, task_id, status, capture_complete=True):
        with self.lock:
            for entry in self.entries(task_id):
                if entry["status"] == "in_flight":
                    entry.update(status="unknown", finishedAt=timestamp(), errorCode="INTERRUPTED")
                    self.record(task_id, entry)
            with self.db:
                self.db.execute(
                    "UPDATE usage_tasks SET status=?,capture_complete=min(capture_complete,?) WHERE id=?",
                    (status, int(capture_complete), task_id),
                )

    def entries(self, task_id):
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT entry FROM usage_requests WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]

    def summary(self, task_id):
        with self.lock:
            task = self.db.execute("SELECT * FROM usage_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                return None
            entries = self.entries(task_id)
        requests = [e for e in entries if e["status"] != "cache_hit"]
        unknown = sum(
            any(e["usage"][k] is None for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
            for e in requests
        )
        sums = {
            key: sum(e["usage"][key] or 0 for e in requests)
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
            )
        }
        return {
            "version": 1,
            "taskId": task_id,
            "kind": task["kind"],
            "model": task["model"],
            "status": task["status"],
            "requests": len(requests),
            "retries": sum(e["attempt"] > 0 for e in requests),
            "succeeded": sum(e["status"] == "succeeded" for e in requests),
            "failed": sum(e["status"] == "failed" for e in requests),
            "cancelled": sum(e["status"] == "cancelled" for e in requests),
            "active": sum(e["status"] == "in_flight" for e in requests),
            "unknownUsageRequests": unknown,
            "cacheHits": len(entries) - len(requests),
            "knownTokens": sums,
            "complete": bool(task["capture_complete"]) and unknown == 0,
            "captureComplete": bool(task["capture_complete"]),
        }

    def document_tasks(self, document_id):
        with self.lock:
            return [
                self.summary(row[0])
                for row in self.db.execute(
                    "SELECT id FROM usage_tasks WHERE document_id=? ORDER BY created_at DESC LIMIT 50",
                    (document_id,),
                ).fetchall()
            ]

    def report(self, task_id):
        # A worker may append while the user exports. Summary and rows share one snapshot.
        with self.lock:
            self.db.execute("BEGIN")
            try:
                return {"summary": self.summary(task_id), "entries": self.entries(task_id)}
            finally:
                self.db.rollback()

    def close(self):
        self.db.close()
