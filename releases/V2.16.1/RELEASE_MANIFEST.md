# OpsPilot V2.16.1 发布清单

- 冻结日期：2026-07-27
- 版本：V2.16.1
- 用途：内部历史快照与可复现验收，不是客户交付目录
- 客户交付目录：`dist/dingtalk-ops-v2.16.1-package/`

## 交付物校验

交付目录严格包含三个文件：

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| `dingtalk-ops-v2.16.1.exe` | 23,948,329 | `f20af16d970bad4c72635b59c904f8148e652175d317afa4d3cf3cd326cc4798` |
| `config.yaml` | 2,280 | `051dada2b5a58fbbc7eafdfbe3b2cba68a38d602c507ee10188c8663372e0d84` |
| `首次使用说明.txt` | 520 | `3d5327f5a8687e6beb5433cd112f9e5f919d45e03413253f1b4ed67a6aded386` |

构建环境为 Python 3.11.9、PyInstaller 6.20.0。发行门禁已验证 Windows `MZ` 文件头、最小体积、三文件白名单、空白凭证、敏感 URL 参数、本机绝对路径和已知租户标记。

## 验收结果

- `python -m unittest discover -s tests -p "test_*.py"`：182 项通过。
- `python tests/test_ticket.py`：76 项通过，0 项失败。
- V2.16.1 专项回归：运行时路径、工单/知识路由、活动排障直接建单、IMA 正文质量、AI 错误隔离、咨询/申请分类、新话题、管理员查询性能、反馈语义和日志脱敏均通过。
- Python 编译：`main.py`、`build.py`、`handlers/`、`modules/`、`utils/`、`tests/` 通过。
- JavaScript 语法：`admin_ui/app.js`、`admin_ui/console-shell.js`、`utils/spreadsheet_worker.mjs` 通过。
- Word 商用简介：ZIP/OOXML 与 `python-docx` 结构校验通过，版本文本为 V2.16.1；LibreOffice 26.2.4.2 转换成功，5 页 PNG 已逐页完成视觉验收，无截断、重叠或多余空白页。
- 最终 EXE 启动：管理页面 HTTP 200，未配置凭证时首次配置模式保持可用。
- 最终 EXE 工单实测：通过管理 API 创建并查询 `TKT-20260727-002` 成功，运行目录 `data/tickets.db` 存在，确认不再写入 PyInstaller 临时目录。
- EXE 日志实测：运行目录 `logs/opspilot-error.log` 已生成；专项测试覆盖 Bearer、`sk-*`、带供应商前缀的 API Key/Access Token、Refresh Token、Client Secret 和密码脱敏，并确认 token 数量指标不被误删。
- 用户运行目录迁移：V2.16.0 的既有配置、人员设置、日志和数据已复制到独立 V2.16.1 运行目录；配置哈希一致，4 个 SQLite 数据库 `quick_check` 均为 `ok`，工单库 schema 为 3。
- 测试结束后无 V2.16.1 EXE 进程残留。

## 修复范围

- 单文件 EXE 的工单库、备份、附件和语音路径统一到持久运行目录。
- 工单意图优先于旧式知识搜索，排障续轮不再被触发词误截获。
- 活动排障中所有建单说法立即升级，并保留原始问题与已尝试方案。
- AI API 错误与正常答案分离；失败不推进排障、不写知识闭环、不设置反馈状态，自动模式支持本地引擎降级。
- IMA 仅有标题而无有效正文时按未命中处理，并从多个内容字段选择最长有效文本。
- 排障会话支持明确开启新话题，流程咨询不再自动建单，反馈文案与模块状态一致。
- 普通 AI 对话不再提前执行管理员数据库查询。
- 新增 WARNING 及以上的轮转持久日志，并在控制台和文件输出前脱敏常见凭证。

## 快照范围

包含当前 `admin_ui/`、`handlers/`、`modules/`、`utils/`、`tests/`，当前构建脚本、空白配置模板、依赖文件、`.gitignore`、发布保存规则、更新日志、操作手册、V2.15/V2.16 当前文档、商用简介 Markdown/Word，以及上述三件已验证产物。

明确不包含开发环境 `config.yaml`、`data/`、`logs/`、`build/`、`dist` 根目录、运行验收目录、其他历史 `releases/`、`.codebuddy/`、缓存、旧 spec 和补丁脚本。

## 历史与安全说明

V2.16.0 冻结目录保持不变；其发布清单 SHA-256 在本次冻结时为 `27b906a4247dabdde3877dcf9d9b63773e9447331e18a59659a2e54090f40112`。

外部客户只能收到 `dist/dingtalk-ops-v2.16.1-package/`，不能收到本快照、运行验收目录或仓库根目录。标准包不包含企业凭证、人员、知识库 ID、数据库和日志。
