# OpsPilot V2.16.2 发布清单

- 冻结日期：2026-07-30
- 版本：V2.16.2
- 用途：内部历史快照与可复现验收，不是客户交付目录
- 客户交付目录：`dist/dingtalk-ops-v2.16.2-package/`
- 用户运行目录：`dist/dingtalk-ops-v2.16.2-runtime/`

## 交付物校验

客户交付目录严格包含三个文件：

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| `dingtalk-ops-v2.16.2.exe` | 24,088,342 | `e899ae7792c025b3e14c7a56566b1a150a3797b078c87e96c81bf076a3ad9233` |
| `config.yaml` | 2,293 | `af738457fdff199248442cc90af7441e8ac2e9d22b76024feb157ef6cee9e2ff` |
| `首次使用说明.txt` | 520 | `f42c1e33705b7e70609779a067fcfe6f739e55ed6ed286b08392e333bf8adc56` |

构建环境为 Python 3.11、PyInstaller 6.20.0、Waitress 3.0.2。发行门禁已验证 Windows `MZ` 文件头、最小体积、三文件白名单、空白凭证、敏感 URL 参数、本机绝对路径和已知租户标记。

## 稳定性修复

- 管理后台从 Flask 开发服务器切换为生产级 Waitress，并限制为固定工作线程池。
- 修复快速任务完成后 Future 残留、任务启动/取消竞争和关闭过程中状态不一致。
- 服务退出时统一关闭钉钉客户端、任务调度器、后台副作用线程池、Waitress 监听器和 CodeBuddy 子进程。
- 通知失败重试使用数据库原子认领，避免并发点击或重复请求造成多次发送。
- AI 表格同步任务合并，相同时间只允许一个同步任务排队或运行。
- 通知和同步后台工作使用固定大小线程池，避免请求突发时无限创建线程。

## 验收结果

- `python -m unittest discover -s tests -p "test_*.py"`：188 项通过。
- `python tests/test_ticket.py`：76 项通过，0 项失败。
- V2.16.2 专项回归覆盖快速任务清理、运行任务取消、幂等关闭、排队任务中断、并发通知重试认领和 Waitress 并发请求。
- Python 编译：`main.py`、`build.py`、`handlers/`、`modules/`、`utils/`、`tests/` 通过。
- JavaScript 语法：`admin_ui/app.js`、`admin_ui/console-shell.js`、`utils/spreadsheet_worker.mjs` 通过。
- 最终 EXE 启动：`/api/setup/status` 返回 HTTP 200，服务器标识为 `OpsPilot/V2.16.2`；停止后无进程与监听端口残留。
- Word 商用简介：ZIP/OOXML 与 `python-docx` 结构校验通过，LibreOffice 转换成功，5 页 PNG 已逐页完成视觉验收；文档 SHA-256 为 `f32ac9d4129befd3ac3250ffd11db7b8566d23753542e2ecb92b93ada62251da`。
- 用户配置迁移：V2.16.1 配置按原 SHA-256 `a9c258ce29eb3559f58aedb861746f5ae2ab06d3e20e2f3a72fdec727cededed` 复制到独立 V2.16.2 运行目录。
- 用户数据迁移：`tickets.db`、`tasks.db`、`knowledge_loop.db`、`observability.db` 的 SQLite `quick_check` 均为 `ok`。

## 快照范围

包含当前 `admin_ui/`、`handlers/`、`modules/`、`utils/`、`tests/`，当前构建脚本和 spec、空白配置模板、依赖文件、`.gitignore`、发布保存规则、更新日志、操作手册、V2.15/V2.16 当前文档、商用简介 Markdown/Word，以及上述三件已验证产物。

明确不包含开发环境 `config.yaml`、`data/`、`logs/`、`build/`、`dist/` 根目录、用户运行目录、其他历史 `releases/`、`.codebuddy/`、缓存、旧 spec 和补丁脚本。

## 历史与安全说明

V2.16.1 冻结目录保持不变；其发布清单在本次冻结时的 SHA-256 为 `b8d8b7d53c44b4279da4c58476589298a7db815bbcb052081417bc08a5bc1ce3`。

外部客户只能收到 `dist/dingtalk-ops-v2.16.2-package/`，不能收到本快照、用户运行目录或仓库根目录。标准包不包含企业凭证、人员、知识库 ID、数据库和日志。
