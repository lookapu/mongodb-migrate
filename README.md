# MongoDB Migrate

[![GitHub release](https://img.shields.io/github/v/release/lookapu/mongodb-migrate)](https://github.com/lookapu/mongodb-migrate/releases)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/mongodb-migrate)](https://pypi.org/project/mongodb-migrate/)
[![License](https://img.shields.io/github/license/lookapu/mongodb-migrate)](LICENSE)
[![CI](https://github.com/lookapu/mongodb-migrate/actions/workflows/ci.yml/badge.svg)](https://github.com/lookapu/mongodb-migrate/actions/workflows/ci.yml)

面向生产环境的 All-in-One MongoDB 数据流动套件。它同时覆盖在线迁移、原生
BSON 备份、校验恢复、JSONL/CSV 数据交换和备份资产管理。工程思路来自同目录
`es-migrate`：任何数据操作都应当可恢复、可审计、可校验、可安全发布。

## 能力

- `_id` 有序流式扫描，避免全量数据进入内存
- `ReplaceOne(upsert=True)` 幂等写入，进程失败后可从 SQLite checkpoint 恢复
- 按文档数和 BSON 字节数双阈值分批，避免撞上 MongoDB 16 MiB 消息边界
- 集合级并发、全局 docs/s 限流、网络/选主/超时指数退避
- 不可恢复写入错误落 Extended JSON DLQ，同时任务失败，避免静默丢数据
- 复制 collection validator、time-series/capped 等关键选项及二级索引
- count / sample / full 三档内容校验
- full 校验持久化双方内容指纹、校验文档数与差异数，形成可审计证据
- 通过影子集合迁移；校验成功后可在目标库内 `renameCollection` 切换
- Change Streams CDC 覆盖 insert/update/replace/delete，并持久化 resume token
- SQLite WAL 审计、Job 租约及目标集合互斥租约
- 生产安全模式、不可变执行计划、审批码和目标运行时资源熔断
- 轮转应用日志、全局 GUI 崩溃报告、脱敏诊断包
- 独立 macOS/Windows 应用、SPDX SBOM、SHA-256 与发布清单
- 原子 `.mmbackup` BSON 归档、集合选项和索引完整保存
- 每个 BSON 数据段与元数据段独立 SHA-256，落盘后强制回读校验
- 可选 PBKDF2-SHA256 + AES-256-GCM 认证加密，密码永不持久化
- 恢复前全包校验、fail/merge/drop 冲突策略及二级索引重建
- Extended JSONL 和显式字段 CSV 导入导出
- SQLite 备份资产目录、校验时间、保留期限与文件指纹
- URI 凭据不会写入状态库

## 安装

### 从 PyPI 安装（推荐）

```bash
pip install mongodb-migrate
```

安装后可直接使用 `mongodb-migrate` 命令行工具：

```bash
mongodb-migrate --help
```

### 从源码安装

```bash
git clone https://github.com/lookapu/mongodb-migrate.git
cd mongodb-migrate
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell 激活虚拟环境：`.venv\Scripts\Activate.ps1`。

建议在 MongoDB 兼容矩阵内使用：源端/目标端至少 4.4，驱动使用 PyMongo 4.x。
目标账号至少需要目标库的 `readWrite` 与索引创建权限；源账号只需读取集合、
索引和 collection metadata。启用 `--cutover` 时还需要 `renameCollection`
对应权限。

## 第一次运行

### macOS 图形界面

直接双击：

```text
dist/MongoDB Migrate.app
```

GUI 分为“连接与集合”“迁移策略”“执行与审计”“备份与交换”四页，提供集合搜索、多选、
Extended JSON Query、核心策略、负载保护、在线追平、DLQ 与任务租约等完整
选项。关键参数旁的 `?` 可查看含推荐值和风险边界的悬浮说明。

连接串仅保存在当前进程内存中，不会写入 GUI 配置文件；状态库、DLQ 和非敏感
设置默认保存在 `~/Library/Application Support/MongoDB Migrate/`。建议先点击
“读取源集合”，选择集合后执行“连接预检”，最后再开始迁移。运行期间可以在
批次边界安全停止，随后使用界面显示的 Job ID 恢复。

“备份与交换”可以直接创建独立 BSON 备份、逐段校验、恢复到目标数据库，或执行
JSONL/CSV 数据交换。`.mmbackup` 是本产品的自描述逻辑备份格式，不依赖系统
`mongodump`；它保存 BSON 类型、集合选项和索引，但不是跨分片的时间点快照。

“生产安全模式”会自动选择 FULL 校验与 fail 冲突策略。首次执行先生成不含
凭据和 Query 明文的计划文件，界面显示计划路径与审批码；确认计划后才会开始
写入。若目标连接占用、WiredTiger Cache 或磁盘空闲越过阈值，任务会在安全
批次边界暂停并写入审计事件。

### 命令行

同一个 `mongodb-migrate` 可执行文件提供全部能力：

```bash
# 原生 BSON 备份
mongodb-migrate backup --db app --output app.mmbackup

# AES-256-GCM 加密备份（密码不出现在进程参数中）
export MONGODB_MIGRATE_BACKUP_PASSWORD='a-long-random-passphrase'
mongodb-migrate backup --db app --output app.mmbackup --encrypt

# 独立校验与恢复
mongodb-migrate verify --input app.mmbackup
mongodb-migrate restore --db app_restore --input app.mmbackup --conflict fail

# Extended JSONL 数据交换
mongodb-migrate export --db app --collection users \
  --format jsonl --output users.jsonl
mongodb-migrate import --db app_restore --collection users \
  --format jsonl --input users.jsonl

# 查看备份资产目录
mongodb-migrate list
```

备份读取采用每集合 majority read cursor，能避免读取未多数提交的数据，但多个集合
并不共享同一个全局时间点。需要分片集群 PITR、整集群灾备或极低 RPO 时，应使用
Atlas/Ops Manager/mongosync 或协调后的存储快照。

先做只读预检：

```bash
export MONGODB_MIGRATE_SOURCE_URI='mongodb://...'
export MONGODB_MIGRATE_TARGET_URI='mongodb://...'
mongodb-migrate \
  --source-db app --target-db app \
  --collections "users,orders_*" \
  --dry-run
```

执行离线迁移（推荐首次上线先停写）：

```bash
mongodb-migrate \
  --source-db app --target-db app \
  --collections "users,orders_*" \
  --target-suffix __migrating_20260727 \
  --workers 4 --batch-size 1000 \
  --docs-per-second 10000 \
  --verify sample --sample-size 1000
```

成功输出 `job_id`。中断后使用完全相同参数并追加：

```bash
mongodb-migrate ... \
  --conflict resume \
  --job-id 0123456789abcdef
```

`--job-id` 对应的端点和参数必须完全一致，防止把 checkpoint 错用到另一个任务。

### 生产审批的两段式执行

先生成计划，不改动 MongoDB：

```bash
mongodb-migrate \
  --source-db app --target-db app \
  --collections "users,orders_*" \
  --change-stream --continuous-writes \
  --verify full --production-safe-mode --plan-only
```

审阅 `reports/<job_id>.plan.json` 后，以同一 Job ID 和参数提交审批码：

```bash
mongodb-migrate ... \
  --production-safe-mode --verify full --change-stream --continuous-writes \
  --job-id <job_id> --approval-token <approval_code>
```

生产安全模式声明源端持续写入时必须使用 Change Streams；持续写入状态下禁止
自动 cutover，应在受控停写窗口或外部发布编排中完成最终切流。

## 在线追平

业务必须为每次新增和更新维护单调、可索引的 BSON Date 或数值字段，例如
`updated_at`：

```bash
mongodb-migrate \
  --source-db app --target-db app \
  --incremental-field updated_at \
  --incremental-rounds 8 \
  --incremental-overlap-seconds 120 \
  --verify sample
```

重叠窗口会重读最近数据，upsert 保证幂等。这个模式只覆盖新增与更新，**不会捕获
物理删除**。存在迁移期间删除、无法维护可靠更新时间、或要求近零停机时，应在
源端 Replica Set/Sharded Cluster 上使用内置 Change Streams CDC：

```bash
mongodb-migrate \
  --source-db app --target-db app \
  --change-stream \
  --cdc-quiet-seconds 5 \
  --cdc-max-seconds 1800 \
  --verify sample
```

工具会在全量前持久化 cluster time，全量后同步 insert/update/replace/delete，
并逐事件保存 resume token。建议切换前短暂停写，等待静默窗口完成后再校验和
切流；不要仅凭 count 相等宣称数据一致。

## 安全切换

默认只写影子集合，不触碰现有目标集合。确认校验、应用兼容和回滚方案后才能加：

```bash
mongodb-migrate ... --verify full --cutover
```

工具会把目标现有 `users` 改名为带时间戳的 backup，再把影子集合改名为
`users`。backup 不会自动删除。跨数据库/跨集群切换应用连接不是原子操作，
需要在服务发现或配置平台完成。

## 运维与审计

```bash
mongodb-migrate-jobs --state-db .mongodb-migrate.sqlite3
mongodb-migrate-jobs --state-db .mongodb-migrate.sqlite3 --job-id JOB_ID
```

状态库开启 WAL 和 `synchronous=FULL`。每个任务带租约，避免两个进程同时恢复
同一个任务。DLQ 默认写到 `dlq/JOB_ID.jsonl`，内容使用 MongoDB Extended JSON。

## 生产上线清单

1. 在脱敏副本演练全量、恢复、切换和回滚。
2. 确认目标 FCV 与源 BSON 类型、validator、collation、time-series 配置兼容。
3. 对迁移账号使用最小权限，URI 通过环境变量或 secret manager 注入。
4. 为源库的 `_id` 和增量字段确认索引，观察复制延迟、CPU、磁盘与连接数。
5. 首次设置保守的 `--workers`、`--batch-size` 和 `--docs-per-second`。
6. 停写或启动 CDC；等待追平后执行 sample/full 校验。
7. 切流后保留旧集合和状态库，完成业务冒烟与回滚窗口后再人工清理。

## 当前边界

- 不迁移 views、用户/角色、sharding zone、balancer 配置和集群参数。
- `_id` checkpoint 依赖 MongoDB 的 BSON 排序；迁移期间修改既有文档 `_id`
  本身不受 MongoDB 支持。
- collection metadata 跨大版本若不兼容会直接失败，不会静默删掉 validator。
- `renameCollection` 仅用于目标数据库内切换；分片集合需按目标集群版本和权限
  先做专项演练。

商业发布门槛、真实集群认证范围及安全报告流程分别见
`COMMERCIAL_READINESS.md`、`COMPATIBILITY.md` 与 `SECURITY.md`。本地构建和
单元测试通过不等同于已经完成特定 MongoDB 版本组合的商业认证。

## 开发验证

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## 贡献

欢迎提交 [Issue](https://github.com/lookapu/mongodb-migrate/issues) 与
[Pull Request](https://github.com/lookapu/mongodb-migrate/pulls)。请先阅读
`SECURITY.md` 的安全报告流程与 `COMMERCIAL_READINESS.md` 的发布门槛。

## 支持本项目

如果这个工具帮你节省了时间，欢迎请作者喝杯咖啡：

| 支付宝 | 微信 |
| --- | --- |
| ![支付宝](docs/sponsor/alipay.jpg) | ![微信](docs/sponsor/wechat.jpg) |

## 构建单文件

```bash
MONGODB_MIGRATE_PYTHON=/path/to/python3.12 ./build_macos.sh
# Windows PowerShell / cmd:
build_windows.bat
```

产物位于 `dist/`。CI 同时执行 Python 3.9/3.12 测试，并可在 Windows/macOS
构建 PyInstaller 可执行文件。macOS GUI 构建需要 Python 3.10+ 与 Tk 8.6+；
不要使用 Xcode Command Line Tools 自带的旧 Python/Tk。

Windows 构建要求安装官方 Python 3.12（包含 Tcl/Tk）和 Python Launcher。
`build_windows.bat` 会自动创建隔离环境、安装依赖、执行 Ruff 与测试，然后生成：

- `dist\mongodb-migrate.exe`：命令行单文件
- `dist\MongoDB-Migrate-GUI.exe`：无控制台窗口的独立 GUI
- `dist\MongoDB-Migrate-windows-x64.zip`：可分发的完整 Windows 压缩包
- `dist\SBOM.spdx.json`：SPDX 2.3 软件物料清单
- `dist\RELEASE.json`：版本、架构、签名状态与制品哈希清单
- `dist\SHA256SUMS-windows.txt`：exe、压缩包和 SBOM 的 SHA-256

两个 exe 都包含 Python、Tcl/Tk、PyMongo 和运行依赖，不要求目标电脑安装 Python。
