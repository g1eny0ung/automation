# Automation

用 GitHub Actions 定时刷新市场广度数据，生成并保存市场日报。

## 定时任务

以下时间和日期均按北京时间计算。

| 任务                                                            | 运行时间         | 检查哪天是否开市 |
| --------------------------------------------------------------- | ---------------- | ---------------- |
| [刷新A股广度](.github/workflows/refresh-market-breadth-zh.yml)  | 每天 18:07       | 今天             |
| [刷新美股广度](.github/workflows/refresh-market-breadth-en.yml) | 每天 10:17       | 昨天             |
| [生成日报](.github/workflows/market-analysis.yml)               | 周二至周六 09:00 | 两个市场的昨天   |

广度任务遇到休市就跳过。日报只有在两个市场都休市时才跳过。手动运行也按表中的日期检查。

只检查指定日期，不往前找交易日。例如，美股任务周六检查周五，周一检查周日并跳过。

当前A股节假日日历只覆盖到 2026 年。运行 2027 年的任务前，需要更新 [日历依赖](requirements-market-calendar.txt)。日历检查失败会停止任务。

日报使用宏观、美股和A股数据，提示词见 [market-analysis-daily-prompt.md](market-analysis-daily-prompt.md)。

## 配置与运行

在仓库的 GitHub Actions secrets 中添加以下密钥。

| Secret             | 用途                             |
| ------------------ | -------------------------------- |
| `MARKET_API_TOKEN` | 访问市场数据接口，三个任务都需要 |
| `ARK_API_KEY`      | 调用 AI 模型，仅日报需要         |

工作流在默认分支上自动运行。手动运行时，在 GitHub 的 Actions 页面选择任务，点击 **Run workflow**。结果和报错都在任务日志中。

## 本地检查

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)、Bash 和 jq。macOS 可用 `brew install uv jq` 安装。在仓库根目录执行。

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

测试不需要密钥，也不会调用真实的市场数据或 AI 接口。

单独检查是否开市。`zh` 是A股，`en` 是美股，`en,zh` 同时检查两个市场。

```sh
.venv/bin/python scripts/market_session.py zh --date today
.venv/bin/python scripts/market_session.py en,zh --date yesterday
```

`--date` 必填，`today` 表示今天，`yesterday` 表示昨天。复查某个时间的结果时，可加 `--now 2026-10-06T09:00:00+08:00`。

## 本地实际取数

需要 Bash、curl 和 jq。先在当前终端设置 `MARKET_API_TOKEN`，再执行下面的命令。它会调用真实接口获取数据，不检查休市日期，也不生成日报。

```sh
bash scripts/fetch-market-data.sh
```
