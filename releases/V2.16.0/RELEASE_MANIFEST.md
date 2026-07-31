# OpsPilot V2.16.0 发布清单

- 冻结日期：2026-07-20
- 版本：V2.16.0
- 用途：内部历史快照与可复现验收，不是客户交付目录
- 客户交付目录：`dist/dingtalk-ops-v2.16.0-package/`

## 交付物校验

交付目录严格包含三个文件：

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| `dingtalk-ops-v2.16.0.exe` | 23,943,375 | `7a8890b19574f1efe3fd08487874629d39d75b2f59a03545dbdb07540ffb570c` |
| `config.yaml` | 2,280 | `051dada2b5a58fbbc7eafdfbe3b2cba68a38d602c507ee10188c8663372e0d84` |
| `首次使用说明.txt` | 520 | `2ba1c9f73d670e66d2cdbb9faeeb0b1ec0c3bfce78dba1f9e0692e50fd974d21` |

构建门禁已验证 EXE 的 Windows `MZ` 文件头、最小体积、三文件白名单、空白凭证、敏感 URL 参数、本机绝对路径和已知租户标记。

## 验收结果

- `python -m unittest discover -s tests -p "test_*.py"`：163 项通过。
- `python tests/test_ticket.py`：76 项通过，0 项失败。
- Python 编译：`main.py`、`build.py`、`handlers/`、`modules/`、`utils/`、`tests/` 通过。
- JavaScript 语法：`admin_ui/app.js`、`admin_ui/console-shell.js` 通过。
- 源码 HTTP 启动：首次配置状态、管理页面和敏感响应门禁通过。
- EXE 第一次启动：无配置时向导可访问，配置提交后无需重启即可进入运行流程。
- EXE 第二次启动：已完成状态持久化，管理页面 HTTP 200。
- 测试结束后无 V2.16 测试进程残留。

## 快照范围

包含当前 `admin_ui/`、`handlers/`、`modules/`、`utils/`、`tests/`，当前构建脚本、空白配置模板、依赖文件、`.gitignore`、发布保存规则、更新日志、操作手册、V2.15/V2.16 当前文档，以及上述三件已验证产物。

明确不包含开发环境 `config.yaml`、`data/`、`logs/`、`build/`、`dist` 根目录、其他历史 `releases/`、`.codebuddy/`、缓存、旧 spec 和补丁脚本。

## 安全说明

外部客户只能收到 `dist/dingtalk-ops-v2.16.0-package/`，不能收到本快照或仓库根目录。旧 V2.15 及更早构建脚本不得用于新的外部分发。

旧的 V1.6.0、V1.7.0、V1.7.1 历史手册被审查出曾写入完整 `sk-...` 形式密钥。它们不在本快照与客户交付范围内，但对应密钥仍必须在提供商侧立即吊销或轮换，旧历史目录应仅保存在受控内部位置。

当前环境的应用内浏览器初始化失败，因此没有完成像素级浏览器截图回归；真实 HTTP 页面加载、DOM 合同、JavaScript 语法和两轮打包 EXE 启动均已通过。
