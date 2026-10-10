# Automation

用 GitHub Actions 定时刷新市场广度数据，生成并保存市场日报。

## 定时任务

以下均为北京时间。

| 任务 | 运行时间 |
| ---- | -------- |
| [刷新 A 股广度](.github/workflows/refresh-market-breadth-zh.yml) | 周一至周五 18:07 |
| [刷新美股广度](.github/workflows/refresh-market-breadth-en.yml) | 周二至周六 10:17 |
| [生成日报](.github/workflows/market-analysis.yml) | 周二至周六 09:00 |

休市时跳过广度刷新；昨天两个市场都休市时跳过日报。日报提示词见 [market-analysis-daily-prompt.md](market-analysis-daily-prompt.md)。

当前 A 股节假日日历只覆盖到 2026 年。运行 2027 年的任务前，需更新 [日历依赖](requirements-market-calendar.txt)。

## 配置与运行

在仓库的 GitHub Actions secrets 中添加以下密钥。

| Secret | 用途 |
| ------ | ---- |
| `MARKET_API_TOKEN` | 日报访问市场数据接口 |
| `ARK_API_KEY` | 日报调用 AI 模型 |
| `UPSTASH_REDIS_REST_URL` | 广度任务使用的 Redis REST 地址 |
| `UPSTASH_REDIS_REST_TOKEN` | 广度任务读写 Redis 的令牌 |

工作流在默认分支上自动运行。手动运行时，在 GitHub 的 Actions 页面选择任务，点击 **Run workflow**。结果和报错都在任务日志中。

## 更新广度代码

广度任务使用本仓库的 [producer/](producer/)。A 股日常增量更新，距上次全量满 14 个自然日时重新抓取历史数据。

先将源仓库 `stock_analysis` 的完整 commit SHA 写入 [stock-analysis-revision.txt](stock-analysis-revision.txt)，再导出并检查。

```sh
python3 scripts/breadth_bundle.py export /path/to/stock_analysis
python3 scripts/breadth_bundle.py check
```

## 本地检查

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)、Bash 和 jq。macOS 可用 `brew install uv jq` 安装。在仓库根目录执行。

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

测试不需要密钥，也不会调用真实的市场数据或 AI 接口。

## 本地实际取数

需要 Bash、curl 和 jq。先在当前终端设置 `MARKET_API_TOKEN`，再执行下面的命令。它会调用真实接口获取数据，不检查休市日期，也不生成日报。

```sh
bash scripts/fetch-market-data.sh
```
