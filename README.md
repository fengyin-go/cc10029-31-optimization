# 智能爬虫采集平台

面向网页爬取、API接入、增量抓取、反爬对抗与采集质量校验的数据采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集目标 | `targetsite` | 目标站点 | 站点编号、站点名称、入口URL |
| 抽取规则 | `selector` | 选择器规则 | 规则编号、归属站点、列表选择器 |
| 爬虫引擎 | `crawler` | 爬虫任务 | 任务编号、任务名称、关联站点 |
| 代理池 | `proxy` | 代理节点 | 节点编号、IP地址、端口号 |
| 反爬对抗 | `antiblock` | 反爬策略 | 策略编号、策略名称、触发条件 |
| 定时调度 | `schedule` | 调度计划 | 计划编号、关联任务、Cron表达式 |
| 数据提取 | `extractor` | 提取模板 | 模板编号、模板名称、输入URL |
| 采集校验 | `validate` | 校验规则 | 规则编号、校验字段、期望格式 |
| 失败重试 | `retry` | 重试策略 | 策略编号、适用任务、最大重试 |
| 存储落地 | `storage` | 存储配置 | 配置编号、存储类型、连接信息 |
| 重复去重 | `dedup` | 去重策略 | 策略编号、业务键列、去重算法 |
| 质量报告 | `quality` | 质量指标 | 指标编号、指标名称、统计口径 |
| 运行监控 | `monitor` | 监控大盘 | 大盘编号、关联任务、采集速率 |
| 告警中心 | `alert` | 告警规则 | 规则编号、监控对象、告警条件 |
| 采集API | `apigate` | API配置 | 配置编号、端点路径、请求方法 |
| 历史归档 | `archive` | 归档策略 | 策略编号、数据来源、归档周期 |
| 模板市场 | `template` | 采集模板 | 模板编号、模板名称、适用站点 |
| 采集流程 | `flowline` | 流程编排 | 流程编号、流程名称、步骤序列 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 分区计算快照（存储落地）

存储落地的分区判断曾经散在列表、导出、归档三个入口各自计算，批次结果容易不一致。
现在分区计算抽取成一份共用实现（`backend/app/services/partition.py`），三个入口统一读取
已提交的分区计算快照，不再各自判定。

按既有数据流梳理的处理步骤：

1. 读取存储配置（分区策略、保存天数、配置版本）与该配置已提交的既有快照（既有落地区段）。
2. 选定保留配置来源：冻结版沿用历史批次冻结的配置，最新版采用当前存储配置；
   一份快照只采用一份保留配置来源，当前配置与历史批次配置冲突时下游不混用。
3. 按分区策略把批次边界切分成区段，并计算每个区段的保存天数。
4. 历史区段按原配置保留（保存天数逐字沿用），统一计算不悄悄改动已落地数据。
5. 生成校验结论，把分区计算快照与归档批次清单在同一事务落库；任一步失败整体回退，
   归档批次清单不会读到半份数据。
6. 列表、导出、归档三个入口读取同一份已提交快照；重复送交同一批次只写入一份，
   后续请求直接读取已存在结果。

相关接口（沿用既有返回结构）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/storage` / `GET /api/storage/export` | 列表与导出携带同一快照派生的分区口径 |
| `POST /api/storage/{id}/partition-snapshots` | 送交分区计算快照（批次边界 + 冻结版/最新版），幂等写入 |
| `GET /api/storage/{id}/partition-snapshots` | 读取该配置已提交的快照（配置版本、批次边界、校验结论） |
| `GET /api/storage/partition-compare?配置编号=…` | 对比列表/导出/归档三入口口径、冻结版与最新版血缘、历史保存天数核对 |
| `GET /api/archive/manifest?配置编号=…` | 归档批次清单：读取快照同步的台账，区段与原落地记录一一对应 |

回归测试：`cd backend && .venv/bin/python tests/test_partition_snapshot.py`

领域：数据工程
子方向：数据采集、爬虫与RPA
