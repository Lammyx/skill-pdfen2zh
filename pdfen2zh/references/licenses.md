# 代码来源与许可证

本技能后端从工作区 `PDFtranslate/services/reader_service` 提取、适配；源文件 SHA-256 与迁入位置记录于 [source-map.json](source-map.json)。原工程以 GNU AGPL v3 提供，本技能复制的后端及新增适配代码沿用 AGPL-3.0，完整文本见技能根目录 `LICENSE`。

主要修改：去除桌面、项目和文库导入；改为文件任务清单；提供独立 CLI/环境变量；显式传递修复单元文件；保留片段缓存、用量与版式保护。

运行依赖保留各自许可：BabelDOC 0.6.4 为 AGPL-3.0，PyMuPDF 为 AGPL/商业双许可，pypdf 为 BSD-3-Clause；其他依赖以其安装包许可为准。本技能不附带布局模型、字体、CMap、tokenizer 或第三方 PDF，也不复制原阅读器的密钥、文库或虚拟环境。
