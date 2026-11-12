---
name: pdfen2zh
description: Translate English academic PDFs into Chinese while preserving page layout, producing Chinese and bilingual PDFs. Also repair selected pages of this skill's prior output using cached translations or bounded new requests. Use for full PDF translation; general PDF extraction, editing, and summarization use the existing pdf skill.
---

# pdfen2zh：英文 PDF 翻译为中文

使用本技能自带 Python 后端，底层固定 BabelDOC 0.6.4。无需启动阅读器、连接 Zotero 或建立索引。

## 开始

1. 确认用户指定的输入 PDF 和输出位置。没有指定位置时使用输入目录下的 `translated/`，任务会自动创建独立子目录。
2. 阅读 [usage.md](references/usage.md) 获取环境安装、参数、缓存与修复命令。统一使用 `python launch.py`；首次执行 `init` 安装锁定依赖，再 `warmup` 准备资源，最后 `doctor` 检查。入口按自身位置定位技能，可从任意目录调用。不得借用其他项目的密钥或依赖路径作为固定运行条件。
3. 输入为扫描件或复杂表格时按材料选择 `--document-mode ocr` 或 `table`；这些模式仍依赖原引擎能力，不能保证所有图中文字可译。
4. 用户要求翻译具体 PDF 后执行 `translate`。接口从环境变量或本地 `config.local.json` 读取，格式见 `config.example.json`；现有环境变量优先。密钥优先放会话环境，不写入公开技能文件、命令参数或任务清单。
5. 根据最终 `manifest.json` 和 `report.json` 检查执行状态、页数、片段保留原因与用量，再交付中文和双语 PDF。对用户报告待检查页，不把 `checks_passed` 当成翻译语义验收。

```powershell
# 在技能目录内；其他目录使用 launch.py 的绝对路径
python launch.py init
python launch.py warmup
python launch.py doctor
python launch.py translate --input "paper.pdf" --output "translated"
```

## 修复已有结果

- 用户指出漏译页时，使用该译文任务的 `manifest.json`。`repair --pages 5,7` 默认 0 新请求；缓存不足的页面保持旧版本并在报告中列出。
- 用户要求补译缺失内容时可设置 `--max-new-requests N`，优先复用缓存，在这个上限内只补缺失单元；不重新生成整篇。必须沿用基准模型、端点与译文设置。
- 图表斜排标签空白或重复页眉不一致时，优先 `repair --layout-only --pages ...`；该模式不调用模型。恢复英文标签只解决内容丢失，不称其已翻成中文。
- 任一修复均生成新目录，保留原文和旧产物。没有本技能清单的外部译文不直接套用缓存修复，先说明缺少源版本/设置记录。
- 迁移时复制完整任务目录。修复使用当前机器缓存，旧缓存路径仅记录来源；模型/端点身份与片段记录仍须兼容。

## 输出检查

- 交付 `result/translated.zh.pdf`、`result/bilingual.en-zh.pdf`，附必要的报告路径。
- 报告包括输入散列、物理页数、处理范围、保留片段、排版修改、实际请求/缓存命中与已知 token。未知用量不当成 0。
- 翻译失败时查看该任务 `report.json` 和 `engine-diagnostic.json`。停止的任务保留诊断，不将部分 PDF 当成完成结果。
- 打开生成的代表页面检查版式；对整篇论文的质量结论必须来自实际检查。仅 CLI/Mock 检查不能证明真实翻译质量。

维护后端时阅读 [architecture.md](references/architecture.md) 和 [source-map.json](references/source-map.json)。源码与引擎许可说明见 [licenses.md](references/licenses.md)；模型/字体资产不随本技能分发。

分享时使用 `python launch.py package --output "<技能目录之外>/pdfen2zh.zip"`，白名单保留源码、配置示例和许可证，不包含本地配置、环境、缓存、任务或 PDF。不要直接压缩整个运行目录。
