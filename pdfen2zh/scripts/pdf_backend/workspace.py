import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def pdf_info(path):
    pdf = PdfReader(path, strict=False)
    if pdf.is_encrypted:
        raise ValueError("加密 PDF 暂不支持，请先提供解密副本")
    if not pdf.pages:
        raise ValueError("PDF 没有页面")
    return len(pdf.pages)


def write_json(path, data):
    path = Path(path)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def task_file(root, record):
    # Existing Windows manifests used backslashes; both forms must survive relocation.
    target = (root / record["path"].replace("\\", "/")).resolve(strict=True)
    if not target.is_relative_to(root) or digest(target) != record["sha256"]:
        raise ValueError("基准文件位置或散列不匹配")
    return target


class Workspace:
    def __init__(self, source, output, kind, settings, provider, generation, cache_dir):
        source = Path(source).resolve(strict=True)
        if source.suffix.lower() != ".pdf":
            raise ValueError("输入必须是 PDF 文件")
        source_hash = digest(source)
        count = pdf_info(source)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.root = Path(output).resolve() / f"{stamp}-{uuid4().hex[:12]}"
        self.output = self.root / "result"
        self.output.mkdir(parents=True, exist_ok=False)
        self.source = self.root / "input.pdf"
        shutil.copyfile(source, self.source)
        if digest(self.source) != source_hash:
            raise ValueError("输入在复制时发生变化")
        self.manifest = {
            "schemaVersion": 1,
            "id": self.root.name,
            "kind": kind,
            "status": "running",
            "source": {"path": str(source), "sha256": source_hash, "pages": count},
            "settings": settings,
            "provider": provider.identity(),
            "generation": generation,
            "cacheDirectory": str(cache_dir),
            "engine": "babeldoc==0.6.4",
            "adapterVersion": "pdf-en-to-zh-v1",
            "outputs": {},
        }
        self.save()

    def save(self):
        write_json(self.root / "manifest.json", self.manifest)

    def check_source(self):
        expected = self.manifest["source"]["sha256"]
        if digest(self.source) != expected or digest(self.manifest["source"]["path"]) != expected:
            raise ValueError("输入或工作副本发生变化，拒绝发布产物")

    def validate(self, mono, dual):
        count = self.manifest["source"]["pages"]
        for path in (mono, dual):
            path = Path(path).resolve(strict=True)
            if not path.is_relative_to(self.output) or path == self.source:
                raise ValueError("引擎产物超出本次输出目录")
            if pdf_info(path) != count:
                raise ValueError("引擎产物物理页数与原文不一致")
        original, translated, bilingual = (PdfReader(p) for p in (self.source, mono, dual))
        for a, b, c in zip(original.pages, translated.pages, bilingual.pages, strict=True):
            aw, ah = float(a.mediabox.width), float(a.mediabox.height)
            bw, bh = float(b.mediabox.width), float(b.mediabox.height)
            cw, ch = float(c.mediabox.width), float(c.mediabox.height)
            if abs(aw - bw) > 0.1 or abs(ah - bh) > 0.1 or abs(cw - 2 * bw) > 0.1 or abs(ch - bh) > 0.1:
                raise ValueError("产物尺寸不符合中文逐页/并排双语格式")

    def publish(self, mono, dual):
        self.check_source()
        self.validate(mono, dual)
        for kind, path, name in (("mono", mono, "translated.zh.pdf"), ("dual", dual, "bilingual.en-zh.pdf")):
            target = self.output / name
            if Path(path).resolve() != target:
                shutil.copyfile(path, target)
            self.manifest["outputs"][kind] = {
                "path": target.relative_to(self.root).as_posix(),
                "sha256": digest(target),
            }


def load_base(path):
    path = Path(path).resolve(strict=True)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest["schemaVersion"] != 1
        or manifest["status"] != "succeeded"
        or manifest["engine"] != "babeldoc==0.6.4"
        or manifest["adapterVersion"] != "pdf-en-to-zh-v1"
    ):
        raise ValueError("基准须为本技能成功完成且版本兼容的任务 manifest")
    root = path.parent
    if digest(root / "input.pdf") != manifest["source"]["sha256"]:
        raise ValueError("基准输入副本已变化")
    paths = {}
    for kind in ("mono", "dual"):
        paths[kind] = task_file(root, manifest["outputs"][kind])
    return path, manifest, paths
