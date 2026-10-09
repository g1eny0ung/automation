# Automation

用 GitHub Actions 定时刷新市场广度数据，生成并保存市场日报。

## 定时任务

以下时间和日期均按北京时间计算。

| 任务                                                            | 运行时间         | 检查哪天是否开市 |
| --------------------------------------------------------------- | ---------------- | ---------------- |
| [刷新A股广度](.github/workflows/refresh-market-breadth-zh.yml)  | 周一至周五 18:07       | 今天             |
| [刷新美股广度](.github/workflows/refresh-market-breadth-en.yml) | 周二至周六 10:17       | 昨天             |
| [生成日报](.github/workflows/market-analysis.yml)               | 周二至周六 09:00 | 两个市场的昨天   |

广度任务遇到休市就跳过。日报只有在两个市场都休市时才跳过。手动运行也按表中的日期检查。

只检查指定日期，不往前找交易日。例如，美股任务周六检查周五，周一检查周日并跳过。

当前A股节假日日历只覆盖到 2026 年。运行 2027 年的任务前，需要更新 [日历依赖](requirements-market-calendar.txt)。日历检查失败会停止任务。

日报使用宏观、美股和A股数据，提示词见 [market-analysis-daily-prompt.md](market-analysis-daily-prompt.md)。

## 配置与运行

在仓库的 GitHub Actions secrets 中添加以下密钥。

| Secret             | 用途                             |
| ------------------ | -------------------------------- |
| `MARKET_API_TOKEN` | 日报访问市场数据接口 |
| `UPSTASH_REDIS_REST_URL` | 广度任务使用的现有 Redis REST 地址 |
| `UPSTASH_REDIS_REST_TOKEN` | 广度任务读取成分缓存、取得刷新锁并写快照的 Redis 令牌 |
| `ARK_API_KEY`      | 调用 AI 模型，仅日报需要         |

工作流在默认分支上自动运行。手动运行时，在 GitHub 的 Actions 页面选择任务，点击 **Run workflow**。结果和报错都在任务日志中。

## 广度刷新

Actions 使用本仓库的 [producer/](producer/) 运行广度刷新，不需要访问 GitLab。该目录只公开广度运行所需的 12 个 Python 文件，以及原版 `pyproject.toml` 和 `uv.lock`。完整文件清单见 [scripts/breadth_bundle.py](scripts/breadth_bundle.py)，不包含 `.env`、凭据、源仓库历史或其他业务代码。保留原项目的公开依赖锁文件以维持依赖版本，其中也有本任务不使用的依赖。

[stock-analysis-revision.txt](stock-analysis-revision.txt) 记录源代码的完整 commit SHA，[stock-analysis-bundle.json](stock-analysis-bundle.json) 记录同一版本和每个文件的 SHA-256。准备和运行时都会核对版本、文件清单和内容摘要，然后在 `producer/` 内用 Python 3.14 和 `uv sync --locked` 安装依赖。算法、目标日期、成分缓存、刷新锁和 Redis 发布均由原版 `scripts.refresh_market_breadth` 负责。

准备步骤包括交易日检查，最多 4 分钟。刷新进程组最多运行 14 分钟，发送 TERM 后最多再等 15 秒强杀；job 上限 20 分钟。任务不自动重试。失败后可查看最终摘要及 `breadth-en-*` 或 `breadth-zh-*` artifact，内含逐次尝试日志、汇总和成功时的完整快照。来源失败次数与最终失败股票数分开统计，备用来源成功不会增加失败股票数。发布响应丢失时，按日志中的本次 `refreshed_at`、内容摘要与 Redis 核对，不能只凭同日键存在就认为本次发布成功。

更新 producer 时，先在 stock_analysis 部署相应缓存改动，把包含 CLI 的完整 commit SHA 写入版本文件，再从本地源仓库按固定清单导出。命令读取该提交的文件，不读取工作区改动。

```sh
python3 scripts/breadth_bundle.py export /path/to/stock_analysis
python3 scripts/breadth_bundle.py check
```

核对公开文件及差异后，先在迁移分支手动运行两个广度 workflow，核对日志、Redis 和 GET 返回的同一份快照，再合并到默认分支切换定时路径。现有 POST 仍可用于人工刷新。

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
