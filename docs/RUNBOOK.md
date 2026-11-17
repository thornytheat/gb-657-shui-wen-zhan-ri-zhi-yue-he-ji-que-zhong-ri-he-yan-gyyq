# 运行手册 hydrocalendar

## 1 启动

```bash
cd projects/gb-657
docker compose up -d --build
curl -sS http://localhost:18216/api/health
```

页面：<http://localhost:18216/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18216；SQLite 落在命名卷 `hydrocalendar-data` 的 `/data/hydrocalendar.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 批次对账页看四账与运行清单；导出页下载五份文件。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18216/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18216/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18216/api/export/monthly.csv
```

## 2.5 跨站补估

```bash
# 1. 配置邻站系数
curl -sS -X POST http://localhost:18216/api/supplement/neighbors \
  -H 'Content-Type: application/json' \
  -d '{"station_id":"0012","neighbor_id":"0031","coefficient":"1.2"}'
# 2. 登记邻站实测与目标站已有实测
curl -sS -X POST http://localhost:18216/api/supplement/observations \
  -H 'Content-Type: application/json' \
  -d '{"station_id":"0031","date":"2024-02-02","rainfall_mm":"10.0"}'
# 3. 预览依据摘要，再补估（同幂等键重试返回同一份结果）
curl -sS -X POST http://localhost:18216/api/supplement/preview \
  -H 'Content-Type: application/json' -d '{"station_id":"0012","month":"2024-02"}'
curl -sS -X POST http://localhost:18216/api/supplement/estimates \
  -H 'Content-Type: application/json' \
  -d '{"station_id":"0012","month":"2024-02","idempotency_key":"2024-02-v1"}'
# 4. 回读：实测 / 估算 / 含估分开
curl -sS 'http://localhost:18216/api/supplement/readback?station_id=0012&month=2024-02'
# 5. 依据更正后立即撤销；中断用 resume 续跑
curl -sS -X POST http://localhost:18216/api/supplement/revocations \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"rv-2024-02","station_id":"0012","month":"2024-02"}'
curl -sS -X POST http://localhost:18216/api/supplement/revocations/resume -d '{}'
# 6. 撤销件随时可按落库快照复算（不读当前台账）
curl -sS http://localhost:18216/api/supplement/estimates/se-xxxxxxxxxxxxxxxx
```

页面：<http://localhost:18216/supplement?station_id=0012&month=2024-02>。

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |
| 422 `E_SUPPLEMENT_NO_NEIGHBOR` | 目标站没有邻站配置 | 先 POST 邻站系数 |
| 422 `E_SUPPLEMENT_CYCLE` | 邻站依赖成环 | 去掉回指的邻站关系 |
| 409 `E_SUPPLEMENT_REVISION_STALE` | 补测带了过期 `expected_revision` | 重新读取当前 revision 再补测 |
| 409 `E_SUPPLEMENT_BASIS_STALE` | 补估依据在计算/落库期间被改 | 重新预览取新 `basis_digest` 再补估；旧结果仍可回读 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HYDROCALENDAR_DB` | `/data/hydrocalendar.sqlite` | SQLite 路径 |
| `HYDROCALENDAR_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
