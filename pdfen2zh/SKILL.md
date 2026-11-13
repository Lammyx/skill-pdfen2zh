---
name: pdfen2zh
description: Translate English academic PDFs into Chinese and bilingual PDFs with preserved layout. Use an API or export units for the current assistant to translate and locally render without a translation API. Also repair selected pages using saved translations or bounded API requests. General PDF editing and summarization are outside this workflow.
---

# pdfen2zh：英文 PDF 翻译为中文

使用本技能自带 Python 后端，底层固定 BabelDOC 0.6.4。

## 首次准备（两种模式共用）

先阅读 [usage.md](references/usage.md) 的环境说明。需要 Python 3.11–3.13 和 PATH 中的 uv；缺少 uv 时执行 `python -m pip install uv`。安装完整技能目录后，先运行 `python launch.py init --warmup`，再运行 `python launch.py doctor`。首次准备需要联网下载锁定依赖、布局模型和字体；不能把安装 Skill 目录当作运行环境已经就绪。

入口按自身位置定位技能，可从其他目录通过 `launch.py` 绝对路径调用。助手模式不需要供应商配置；只有 API 模式需要端点、模型和所需密钥。

## 选择翻译方式

- 用户要求“你来翻译”“不走 API”时使用**助手文件模式**，先读 [agent-workflow.md](references/agent-workflow.md)，执行 `agent-export`，由当前助手实际翻译并保存每批译文，再 `agent-check`、`agent-render`。不配置或调用翻译服务，不能自动回退 API。
- 用户明确选择翻译 API 时使用下文 `translate` 流程。命令行 `translate` 保持原有 API 行为。
- 用户未指定方式时，在当前助手能读取/写入文件的环境中优先使用助手文件模式；不能因发现环境中的密钥而自动选择 API。两种方式均先准备本地引擎资源。
- 助手文件模式的 API 账本为 0 请求，只代表没有额外翻译服务调用；对话本身仍有上下文和用量。不要称其不消耗 token。

## API 模式

1. 确认用户指定的输入 PDF 和输出位置。没有指定位置时使用输入目录下的 `translated/`，任务会自动创建独立子目录。
2. 使用已准备好的独立环境；参数、缓存与修复命令见 [usage.md](references/usage.md)。不得借用其他项目的密钥或依赖路径作为固定运行条件。
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
- 助手任务需要改译文时，修改原导出任务的译文文件并重新 `agent-render`，复用其余单元。不能用正数 API 预算补译助手任务。

## 输出检查

- 交付 `result/translated.zh.pdf`、`result/bilingual.en-zh.pdf`，附必要的报告路径。
- 报告包括输入散列、物理页数、处理范围、保留片段、排版修改、实际请求/缓存命中与已知 token。未知用量不当成 0。
- 翻译失败时查看该任务 `report.json` 和 `engine-diagnostic.json`。停止的任务保留诊断，不将部分 PDF 当成完成结果。
- 打开生成的代表页面检查版式；对整篇论文的质量结论必须来自实际检查。仅 CLI/Mock 检查不能证明真实翻译质量。

维护后端时阅读 [architecture.md](references/architecture.md) 和 [source-map.json](references/source-map.json)。源码与引擎许可说明见 [licenses.md](references/licenses.md)；模型/字体资产不随本技能分发。

分享时使用 `python launch.py package --output "<技能目录之外>/pdfen2zh.zip"`，白名单保留源码、配置示例和许可证，不包含本地配置、环境、缓存、任务或 PDF。不要直接压缩整个运行目录。
