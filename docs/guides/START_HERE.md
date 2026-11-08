# 从这里开始

Eksmu 网站与服务端源码已完整包含在本仓库中，仅依赖 Python 3.11+ 标准库、SQLite 与少量本地渲染/加密依赖即可在本地启动完整实例。

阅读参与权与治理基线：[CHARTER.md](../../CHARTER.md)。查看仓库说明与离线核验：[README.md](../../README.md)。

```bash
python3 -m pip install -r requirements-research.txt
python3 scripts/doctor.py
python3 -m eksmu serve
```

在同一台电脑打开 `http://127.0.0.1:8765`。完整公式排版需要本机 Pandoc；没有 Pandoc 时显示原文。

运行本地回归测试：`python3 scripts/check_local.py`（或单独运行 `python3 scripts/check_integrated.py`）。无浏览器演示：`python3 scripts/demo_community.py` 与 `python3 scripts/demo_research.py`。全实例备份与恢复说明见 [BACKUP_README.md](BACKUP_README.md)。
