# pdfen2zh

将英文论文 PDF 翻译为中文，保留页面版式，同时生成中文 PDF 和中英并排双语 PDF。支持 **API 翻译**和**由当前助手翻译、由本地引擎排版**两种方式，并可复用已有译文单元。

本仓库提供独立的文件处理后端和可供支持该格式的工具加载的 Skill，无需启动原阅读器、连接 Zotero 或建立知识库索引。翻译引擎固定为 **BabelDOC 0.6.4**。

## 功能

- 生成中文和中英并排双语 PDF，保留公式与版式保护逻辑。
- 通过 OpenAI 兼容的 Chat Completions 接口接入翻译服务。
- 导出真实待译单元，由当前助手分批翻译，检查后回填排版，无需额外翻译 API。
- 支持手工术语表、正文/摘要/附录范围选择和指定页面修复。
- 默认修复只复用缓存；显式设置新请求上限后，才补译缺失内容。
- 提供原文散列校验、请求用量、任务清单和检查报告，方便迁移与继续修复。
- 按白名单打包 Skill，排除本地配置、虚拟环境、PDF、缓存和运行数据。

## 目录

```text
skill-pdfen2zh/
├── README.md
└── pdfen2zh/
    ├── SKILL.md                 技能说明
    ├── launch.py                统一命令入口
    ├── config.example.json      无个人凭证的配置示例
    ├── pyproject.toml
    ├── uv.lock                  锁定运行依赖
    ├── LICENSE
    ├── agents/                 技能界面元数据
    ├── scripts/                翻译、修复与打包后端
    └── references/             使用说明、实现与来源记录
```

作为 Skill 使用时，导入完整的 `pdfen2zh/` 目录，入口说明见 [SKILL.md](pdfen2zh/SKILL.md)。也可直接使用以下命令行流程。

## 快速开始

### 1. 安装环境

需要宿主 **Python 3.11–3.13** 和 **uv**；API 模式另需翻译服务，助手文件模式无需供应商配置。初始化会选择或安装 Python 3.11，并创建独立虚拟环境。

```sh
git clone https://github.com/Lammyx/skill-pdfen2zh.git
cd skill-pdfen2zh/pdfen2zh
python -m pip install uv
python launch.py init --warmup
python launch.py doctor
```

请确保 `uv` 已加入 PATH。macOS/Linux 中可将 `python` 替换为 `python3`，Windows 也可使用 `py -3`。

`init` 安装锁定依赖；`--warmup` 继续下载布局模型、字体、CMap 和 tokenizer。这些资源不随源码分发。`doctor` 只检查本地环境和资源，不调用翻译模型，也不验证供应商密钥或模型能力。

入口按 `launch.py` 自身位置定位 Skill，可从其他目录通过绝对路径运行。输入和输出的相对路径按当前工作目录解析。

### 2. 选择翻译方式

| 方式 | 适用场景 | 命令流程 |
| --- | --- | --- |
| 当前助手翻译 | 让当前对话中的助手翻译具体内容，不额外调用翻译服务 | `agent-export` → 助手填写译文 → `agent-check` → `agent-render` |
| API 翻译 | 使用已配置的 OpenAI 兼容翻译服务 | 配置端点/模型/密钥 → `translate` |

助手模式示例：

```sh
python launch.py agent-export --input "paper.pdf" --output "agent-tasks"
# 当前助手读取 batches/*.source.json，逐批填写对应的 *.translations.json。
python launch.py agent-check --manifest "agent-tasks/<任务 ID>/manifest.json" --translations "agent-tasks/<任务 ID>/batches/0001.translations.json"
python launch.py agent-render --manifest "agent-tasks/<任务 ID>/manifest.json" --translations "agent-tasks/<任务 ID>/batches/0001.translations.json" --output "translated"
```

多批次时须提供全部译文文件。缺译、重复 ID 或损坏公式/样式标记会被报出；不会自动补发 API。此流程需要当前助手读取并写入文件，命令行不会自动驱动对话。修改译文时只编辑相应单元，保留其余译文；重新排版不产生翻译 API 请求。完整格式与步骤见[助手文件模式](pdfen2zh/references/agent-workflow.md)。

API 请求为 0 不表示不消耗当前对话的上下文或用量；本地账本不测量对话 token。使用助手模式可跳过以下供应商配置。

### 3. 配置翻译服务（仅 API 模式）

复制 `config.example.json` 为同目录的 `config.local.json`，将端点和模型改成供应商提供的实际值。最小配置如下，示例地址不能直接用于翻译：

```json
{
  "PDF_TRANSLATE_BASE_URL": "https://api.example.com/v1",
  "PDF_TRANSLATE_MODEL": "your-model-id"
}
```

密钥建议通过当前终端的环境变量提供。PowerShell：

```powershell
$env:PDF_TRANSLATE_API_KEY = "<填写自己的密钥>"
```

macOS/Linux：

```sh
export PDF_TRANSLATE_API_KEY='<填写自己的密钥>'
```

本地无密钥服务可保持密钥为空。配置优先级为：**显式 CLI 参数 > 已有环境变量 > 本地 JSON 配置**。入口不自动读取 `.env`。代理、证书、缓存路径和生成参数见[完整使用说明](pdfen2zh/references/usage.md)。

### 4. 通过 API 翻译 PDF

```sh
python launch.py translate --input "paper.pdf" --output "translated"
```

每次建立独立任务目录：

```text
translated/<任务 ID>/
├── input.pdf
├── manifest.json
├── report.json
├── usage.sqlite3
├── translation-units.json
└── result/
    ├── translated.zh.pdf
    └── bilingual.en-zh.pdf
```

`manifest.json` 记录设置、源文件及产物散列；`report.json` 记录处理范围、用量、保留原因和排版检查。成功状态不代表译文语义已通过人工验收，交付前应检查实际页面。

## 局部修复

针对本 Skill 已成功生成的任务，使用其 `manifest.json`。页码从 1 开始，指原 PDF 的物理页码。

```sh
# 只复用已有译文单元，0 次新翻译请求
python launch.py repair --manifest "translated/<任务 ID>/manifest.json" --pages "5,7-8" --output "repaired"

# 允许最多 10 次新请求，优先复用已有译文
python launch.py repair --manifest "translated/<任务 ID>/manifest.json" --pages "5" --max-new-requests 10 --output "repaired"

# 仅修复空白斜排标签、统一重复页眉，不调用翻译模型
python launch.py repair --manifest "translated/<任务 ID>/manifest.json" --pages "1-12" --layout-only --output "repaired"
```

缓存不足时保留旧页，具体结果以报告为准。修复生成新任务，不覆盖原文或旧产物；补译须沿用基准任务的端点、模型和翻译设置。

换电脑时复制完整任务目录，并在新机器初始化环境、准备资源。修复使用当前机器缓存，旧机器路径仅作来源记录；任务内的译文单元可继续复用。外部工具生成且没有本 Skill 清单的译文不能直接套用这一修复流程。

## 打包分享

在 `pdfen2zh/` 目录执行：

```sh
python launch.py package --output "../pdfen2zh.zip"
```

输出必须位于 Skill 目录之外，且不覆盖已有 ZIP。分享包包含完整运行源码、配置示例、依赖锁、说明和许可证，不包含本地密钥配置、虚拟环境、任务、PDF 或缓存。

## 验证与限制

- v0.2.0 已验证助手模式完整链路：实际导出单元、填写中文、覆盖检查和两页双语 PDF 回填。测试中拦截 OpenAI 客户端创建及 HTTP 发送，助手流程仍完成且 API 请求为 0；原 API 模式通过本地模拟接口回归。
- 缺译在排版前停止；长英文原样返回会被拒绝，失败不发布成功产物。助手产物的缓存修复为 0 新请求，未选页面像素保持不变，正数 API 补译预算被拒绝。
- 已在 Windows x64 / Python 3.11.9 验证：ZIP 解压到含中文与空格的路径、独立环境安装、空资源缓存下载和校验、实际 BabelDOC 生成中文/双语 PDF、任务迁移及局部修复。
- API 回归使用本地模拟接口，助手流程的示例译文由当前对话填写；未调用真实翻译或 embedding 服务。这些检查不代表真实供应商兼容性或论文翻译质量评测。
- macOS、Linux 和 ARM 尚未实测。
- 图表斜排标签可能保留英文；扫描件、复杂表格、双栏和公式仍受引擎能力限制。极稀疏文本页可能被上游扫描检测拒绝。
- 当前是需要初始化和配置服务的源码 Skill，不内置运行时、资源或共享翻译 API 密钥。

详细实现和验收边界见[架构与验收记录](pdfen2zh/references/architecture.md)。

## 来源与许可

后端从 PDFtranslate 阅读器模块提取并适配，沿用 **GNU AGPL v3**；完整文本见 [LICENSE](pdfen2zh/LICENSE)，来源与文件指纹见[来源清单](pdfen2zh/references/source-map.json)。

翻译引擎 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 及其他依赖保留各自许可。布局模型、字体和第三方 PDF 不包含在本仓库中；详情见[许可证说明](pdfen2zh/references/licenses.md)。
