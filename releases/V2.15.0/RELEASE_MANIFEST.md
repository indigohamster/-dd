# V2.15.0 发布清单

- 发布日期：2026-07-20
- 可执行文件：`dingtalk-ops-v2.15.0.exe`
- 文件大小：23,861,575 字节
- SHA-256：`315ACF237846F0013E1C82C390E1C2D954CB660B9725473D20C132A24017B5CF`

## 发布门禁

- `python -m unittest discover -s tests -q`：110/110 通过。
- `python tests/test_ticket.py`：76/76 通过。
- Python `compileall`：通过。
- `admin_ui/app.js` Node.js 语法检查：通过。
- PyInstaller 6.20.0 单文件 EXE 构建：通过。
- 独立复核：本轮未发现遗留的 P0/P1/P2 功能发布阻断问题。

## 数据边界

本快照不包含 `config.yaml`、`data/`、`logs/`、`build/`、`dist/`、缓存目录或旧发布目录。真实 API 密钥、企业配置、工单数据库和运行日志均未进入历史版本。

运行 EXE 时，请把当前环境自己的 `config.yaml` 放在 EXE 同目录。首次启动会对工单数据库执行兼容迁移；上线前仍建议保留 `data/` 的独立备份。
