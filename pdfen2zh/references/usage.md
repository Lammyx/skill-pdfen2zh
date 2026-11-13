# 环境、命令与结果

统一入口为 `python launch.py`。Windows 可使用 `py -3 launch.py`，macOS/Linux 使用 `python3 launch.py`。入口按文件自身位置定位技能和虚拟环境；相对输入/输出路径仍相对于当前工作目录。可从其他目录执行 `python "<技能绝对路径>/launch.py" ...`。

## 环境

需要宿主 Python 3.11–3.13 和 uv。没有 uv 时可先执行 `python -m pip install uv`，确保 `uv` 在 PATH。初始化使用 uv 安装/选择 Python 3.11 并按锁文件创建本技能独立环境，不借用其他项目环境：

```powershell
python launch.py init
python launch.py doctor --packages-only
```

依赖版本锁在本技能 `uv.lock`。初始化可能下载 Python 和依赖；`init --warmup` 可继续下载资源。需要访问 PyPI 以及引擎资源下载源；网络受限时按下文配置代理。不需要 Node、Tauri、FastAPI 服务或文库数据库。另一台机器需重新初始化，不要复制虚拟环境。日常命令不自动安装依赖；环境缺失或损坏时提示运行 init。

Windows 中若 ONNX 等原生库报 `DLL load failed`，按 BabelDOC 的诊断提示安装 Microsoft Visual C++ x64 Redistributable；Python 包安装成功不保证系统原生库可加载。当前 `doctor` 检查包版本和资源散列，不检测所有原生库的加载情况。布局推理可使用 CPU，不要求 CUDA。技能目录须可写，以创建 `.venv`；任务目录和用户缓存目录也须可写。

`doctor` 只检查版本和已有资源散列；`doctor --packages-only` 只检查依赖。模型/字体资源缺失时执行：

```powershell
python launch.py warmup --timeout 600
python launch.py doctor
```

warmup 下载布局模型、字体、CMap 和 tokenizer，不发送论文给翻译模型。默认使用 BabelDOC 的用户资源及片段缓存 `~/.cache/babeldoc`，可与同版本引擎复用。指定独立缓存须把全局参数放在子命令前：

```powershell
python launch.py --cache-dir "D:/pdf-cache" doctor
```

## 配置翻译接口

需要 OpenAI-compatible Chat Completions 服务：

- `PDF_TRANSLATE_BASE_URL`：API 基地址，可含 `/v1`，也接受完整 `/chat/completions` 地址。
- `PDF_TRANSLATE_MODEL`：供应商模型 ID。
- `PDF_TRANSLATE_API_KEY`：当前会话环境中的密钥，本地无密钥服务可为空。
- `PDF_TRANSLATE_REQUEST_TIMEOUT_SECONDS`：单请求超时，默认 90，范围 5–600。
- `PDF_TRANSLATE_MAX_OUTPUT_TOKENS`：单请求输出上限，默认 8192，范围 128–65536。
- `PDF_TRANSLATE_MAX_RETRIES`：默认 2，范围 0–5。SDK 内部重试禁用，实际尝试单独入账；局部补译每单元只发一次。
- `PDF_TRANSLATE_THINKING`：`auto/enabled/disabled`；auto 对官方 DeepSeek 关闭思考。

入口默认读取技能目录的 `config.local.json`，也可用 `--config "<路径>"` 明确指定。复制 `config.example.json` 为 `config.local.json`，把示例地址和模型改成供应商实际值。空字符串不设置变量；已有环境变量优先于文件，CLI 端点/模型/缓存参数优先级最高。配置中的相对缓存、证书路径按配置文件所在目录解析。JSON 路径可用 `/`，Windows 反斜线须转义。

密钥建议只放当前终端环境；示例不包含任何个人凭证：

```powershell
$env:PDF_TRANSLATE_API_KEY = "<填写自己的密钥>"
# 仅在网络需要代理时设置；本地无密钥服务可保持 API_KEY 为空
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:NO_PROXY = "localhost,127.0.0.1,::1"
```

macOS/Linux 用 `export PDF_TRANSLATE_API_KEY='...'` 等形式设置相同变量。NO_PROXY 可追加需要直连的供应商域名。自签证书可用 SSL_CERT_FILE 指定可信 CA 文件，不关闭 TLS 校验。入口不读取 `.env` 或桌面配置。doctor 只验证本地环境/资源，不验证密钥、供应商连通性或模型响应质量。

## 翻译

以下 `translate` 是 API 模式。无需翻译 API、由当前助手翻译时，使用[助手文件模式](agent-workflow.md)：`agent-export → 当前助手分批填写译文 → agent-check → agent-render`。引擎命令不会自行驱动当前对话，也不会在缺译时切换 API。

```powershell
python launch.py translate --input "paper.pdf" --output "translated"
python launch.py translate --input "paper.pdf" --output "translated" --glossary "terms.json" --sections abstract body
```

术语文件为 JSON 数组，例如 `[{"source":"world model","target":"世界模型"}]`。`--sections` 默认 abstract body appendix，排除章节仅保留原文，不删除物理页面；章节无法可靠识别时报告错误。`--document-mode` 默认 digital，可选 table / ocr。任务 `--timeout` 默认 3600 秒。

## 按页修复

```powershell
# 只复用既有缓存，不增加翻译请求
python launch.py repair --manifest "translated/<task>/manifest.json" --pages "5,7-8" --output "repaired"

# 在复用缓存后允许最多 10 次新请求
python launch.py repair --manifest "translated/<task>/manifest.json" --pages "5" --max-new-requests 10 --output "repaired"

# 本地恢复空白斜排标签、统一重复页眉
python launch.py repair --manifest "translated/<task>/manifest.json" --pages "1-12" --layout-only --output "repaired"
```

页码是从 1 开始的原文物理页码，不是论文印刷编号。补译沿用基准的模型身份、生成策略、术语和处理范围。`--layout-only` 不能与正数新请求预算同时启用。缓存缺失或内容无法安全套用时保留整页旧译文，不填入半页新内容。查看 `report.json` 中 missingUnits/rejectedUnits/replacedPages。

换电脑时复制完整任务目录，包括 input.pdf、manifest.json、translation-units.json 和 result/。输出路径保存为通用 `/` 分隔的相对路径，也兼容旧 Windows 清单的 `\`。原 source.path 与旧 cacheDirectory 仅作来源记录；修复从清单旁的 input.pdf 读取原文，使用当前 CLI/环境/配置缓存，默认 `~/.cache/babeldoc`。新修复清单的 baseManifest.cacheDirectoryAtCreation 保留旧缓存路径。原电脑可不可访问不影响本地缓存重放；当前机器仍需具备引擎字体/模型资源。更换翻译端点/模型后不允许在同一基准中补发，以免混用缓存。

每次任务生成：

```text
<output>/<UTC时间-随机ID>/
  input.pdf                  输入副本
  manifest.json              设置、源散列、产物散列、修复基准
  report.json                状态、质量、范围、用量、排版报告
  usage.sqlite3              逐次请求用量，不存提示词或密钥
  translation-units.json     可复用译文单元，含文献内容，保留在本地
  result/translated.zh.pdf
  result/bilingual.en-zh.pdf
```

退出码 0 表示命令成功，1 表示失败/资源未就绪，130 表示用户取消。成功修复也可能保留缓存不足的旧页，需看修复报告。Ctrl+C 会停止工作进程并记录取消；已发送请求可能已有供应商用量。

## 当前能力

保留现有公式/占位符检查、标题碎片合并、斜排原字形保护、重复页眉统一、片段缓存及有限按页补译。首版接入手工术语 JSON，没有自动提取术语的 CLI 选项。斜排标签可能保留英文；位图/OCR、表格、复杂双栏和公式排版仍受引擎能力限制，须按实际 PDF 检查。

## 分享与交付范围

```powershell
python launch.py package --output "../pdfen2zh.zip"
```

ZIP 输出必须位于技能目录之外，不覆盖已有 ZIP。按代码中 PUBLIC_FILES 及 scripts/ 的 Python 源码白名单收集；新增分发文件时显式维护该名单。不会带入 config.local.json、.env、虚拟环境、缓存、PDF、任务或测试产物，保留 LICENSE 与来源说明。解压后从 init 开始；本地配置和任务数据需接收方自行准备。

本交付为需首次初始化的源码 skill。助手模式无需配置翻译服务，但需要可读写文件并执行本地命令的助手；API 模式需自行配置翻译服务。源码不内置运行时、布局/字体资源或共享 API 密钥。Windows 以外的入口已按平台选择解释器路径，但需各平台实测后才能声明支持。
