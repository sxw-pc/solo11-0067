# 元仓收割台（metaharvest）

面向机构仓储、小型数字图书馆和内部资料库的**纯本地 OAI-PMH 元数据收割工作台**。
项目自带一个 loopback-only 的 mock OAI-PMH 仓储，运行期不访问任何外网、CDN、云服务或第三方 API。

一条流水线覆盖：

```
协议请求 → 分页/续传令牌 → 原始 XML 留档 → 安全解析 → Dublin Core 归一化
        → SQLite 幂等入库 → 收割台账/错误事件 → JSON/CSV/Markdown 报告
```

## 快速开始

要求 Python 3.11+。依赖为 Flask、Werkzeug、requests、PyYAML、defusedxml 和 pytest。

```bash
# 可选：安装项目与测试依赖（依赖可提前以离线 wheel 准备）
python -m pip install ".[dev]" --no-build-isolation

# 一条命令跑完整演示：mock 启动、全量收割、增量收割、重复收割、报告生成
make demo
```

不安装也可以从仓库直接运行，Makefile 会设置 `PYTHONPATH=src`：

```bash
make test       # 运行测试
make mock       # 启动 mock 仓储，默认 http://127.0.0.1:8787/oai
make demo       # 全流程演示，产物写入 build/demo/
```

手动分步运行：

```bash
# 终端 1：启动 mock 仓储
PYTHONPATH=src python -m metaharvest mock --config configs/mock_repo.yml

# 终端 2：全量收割
PYTHONPATH=src python -m metaharvest harvest --config configs/harvest_demo.yml

# 查看台账 / 生成报告
PYTHONPATH=src python -m metaharvest status --config configs/harvest_demo.yml
PYTHONPATH=src python -m metaharvest report --config configs/harvest_demo.yml --run-id 1
```

默认状态库、XML 归档、报告和审计日志分别位于 `var/metaharvest.sqlite3`、
`var/archive/`、`var/reports/` 和 `var/audit/requests.jsonl`。它们都由配置中的路径决定，也可以用
`harvest --db/--archive-dir/--audit-log` 覆盖。

## 演示内容

`demo` 会启动一个临时端口的本地 mock，创建一个带时间戳的独立会话目录，并执行三轮收割：

1. **全量**：3 页、12 条记录（11 条有效记录 + 1 条删除标记）；
2. **增量**：`from=2024-05-01T00:00:00Z`，读取 6 条记录；
3. **重复增量**：验证同一窗口再次采集时 6 条全部为 `unchanged`，数据库记录幂等。

每轮都会生成 JSON、CSV、Markdown 报告并校验原始 XML 的 SHA-256。演示不会连接外部主机。

## 命令行

| 命令 | 用途 |
| --- | --- |
| `mock` | 启动内置 OAI-PMH 仓储，只允许配置绑定 loopback |
| `harvest` | 执行一次收割；支持 `--from`、`--until`、`--set`、`--metadata-prefix`、`--max-pages`、`--retry-policy inline\|deferred` |
| `harvest --resume RUN_ID` | 从台账中的 resumptionToken 断点续采 |
| `report` | 生成可复核报告；`--format json csv markdown` |
| `status` | 查看最近收割运行 |
| `demo` | 启动 mock、两轮增量语义演示、报告全流程 |

断点续采示例：

```bash
PYTHONPATH=src python -m metaharvest harvest --config configs/harvest_demo.yml --max-pages 1
# 输出 status=interrupted 和 run_id
PYTHONPATH=src python -m metaharvest harvest --config configs/harvest_demo.yml --resume 1
```

续采沿用运行记录中的仓储 URL、筛选条件、归档根目录和已保存的续传令牌，不接受改变筛选条件的参数。

## 瞬时失败与 Retry-After

OAI 分页的续传请求丢失会留下“上一页已提交、当前页未采集”的中间态。本工具把**一次**瞬时失败
当作可恢复的运营状态处理，而不是直接让整次运行失败。

- 只对幂等的 OAI GET 重试显式白名单中的状态码（`408/429/502/503/504`）以及连接超时/重置。
  `400/401/403/404/500` 等一律不重试；**HTTP 200 的 OAI 协议错误**（如
  `badResumptionToken`、`noRecordsMatch`）也绝不重试。
- `Retry-After` 支持 delta 秒与 HTTP 日期两种形式；无法解析时使用
  `retry.default_wait_seconds`。每次等待都先按 `retry.max_retry_after_seconds` 截断。
- 重试期间不归档、不解析、不入库、不推进 checkpoint；只有最终成功的响应才会成为一页证据，
  并只被处理一次。续传请求重放时只带 `verb` 与 `resumptionToken`，与首次请求形状一致。
- 每次 HTTP 尝试都单独写入 `request_attempts` 表（状态、Retry-After、等待、截止时间），
  与 `raw_responses` 的页面证据分离，并出现在报告中。

两种用户可见策略（配置或 `--retry-policy`）：

| 策略 | 行为 |
| --- | --- |
| `inline` | 服务器要求的等待仍落在 `total_wait_seconds` 内时就地休眠重试，一次批处理可无中断完成。 |
| `deferred` | 最多只进行 `deferred_wait_seconds` 的短等待；更长的等待会持久化 `retry_not_before` 并以 `interrupted` 返回，交给调度器/运维续采。到期前 `--resume` 不发任何请求并以退出码 3 拒绝。 |

同一份 `503 + Retry-After: 120`：`inline`（预算充足时）可返回 `completed` 且 12 条记录全部可见；
`deferred` 返回 `interrupted`，只有更早的页面可见并持久化重试截止时间。这是记录集可见性与运行状态
的真实差异，不只是等待时间长短。

```yaml
retry:
  policy: "deferred"          # inline | deferred
  max_attempts: 3             # 单页最多 HTTP 尝试次数
  default_wait_seconds: 5     # 缺少/非法 Retry-After 时的回退等待
  max_retry_after_seconds: 120  # 单次等待上限
  total_wait_seconds: 300     # 本次调用累计等待预算
  deferred_wait_seconds: 5    # deferred 模式下允许的最短就地等待
```

```bash
# 强制内联重试，一次跑完
PYTHONPATH=src python -m metaharvest harvest --config configs/harvest_demo.yml --retry-policy inline
# deferred 中断后，到期前续采会被拒绝（退出码 3），到期后再执行同一条命令
PYTHONPATH=src python -m metaharvest harvest --config configs/harvest_demo.yml --resume 1
```

## 协议与数据能力

- OAI-PMH 2.0 verbs：`Identify`、`ListMetadataFormats`、`ListSets`、`ListIdentifiers`、
  `ListRecords`、`GetRecord`。
- 仅支持 `oai_dc`；支持 `from`、`until`、`set` 过滤和 HTTP GET/POST 请求。
- mock 分页通过带 HMAC 签名和过期时间的 `resumptionToken` 实现，篡改、过期、跨 verb 使用均报
  `badResumptionToken`。
- 删除记录以 `header status="deleted"` 表示，重复删除幂等。
- 空结果返回 `noRecordsMatch`，收割器将其视为正常的 0 记录完成，并保留错误响应 XML。
- Dublin Core 全字段归一化：标题、创建者、主题、描述、出版者、贡献者、日期、类型、格式、标识符、
  来源、语言、关系、覆盖范围和权限。
- 规范化字段的稳定内容哈希驱动 `created / updated / unchanged / deleted` 统计；更新时间戳本身不会
  造成内容更新。
- 原始响应逐页落盘为 `run-XXXXXX/page-XXXX.xml`，附 `.meta.json` 旁车文件，并在 SQLite 保存
  SHA-256、字节数和请求 URL。

## SQLite 台账

| 表 | 作用 |
| --- | --- |
| `harvest_runs` | 每次运行的参数、状态、检查点、页数和结果计数 |
| `records` | 按 OAI identifier 幂等保存的归一化记录和内容哈希 |
| `raw_responses` | 每页原始 XML 的归档路径、哈希和 HTTP 状态 |
| `request_attempts` | 每次 HTTP 尝试的结果、Retry-After、等待与截止时间（与页面证据分离） |
| `events` | 运行、页面和记录级事件 |
| `errors` | 按运行/记录隔离的错误，供报告复核 |

记录写入使用内容哈希对比；唯一主键为 OAI identifier。停止在 `interrupted` 的运行可通过
`--resume` 继续，从最后保存的令牌请求下一页。

## 安全边界

- HTTP 客户端在发起请求**之前**做 URL 校验，只允许 `127.0.0.1`、`localhost`、`::1`。
- 外部主机、带凭据 URL、非法协议、重定向到外部地址都会直接拒绝，并写入 JSONL 审计日志。
- 手动处理重定向，最多 3 次；禁用环境代理（`trust_env=False`）。
- XML 使用 `defusedxml` 解析，拒绝 DTD 和外部实体，避免 XXE。
- mock 服务只允许绑定 loopback；配置加载会拒绝其他 host。
- SQLite、归档、报告、审计路径均由配置或 CLI 决定，不硬编码到用户主目录。
- 不含认证绕过、限流规避、身份伪造或远程探测能力。

`fixtures/malformed_record.xml` 是一个带外部实体声明的合成 XXE 夹具，测试会确认安全解析器拒绝它。

## 测试

```bash
make test
# 或
PYTHONPATH=src python -m pytest
```

测试覆盖配置解析、mock 协议与分页、协议错误、续传令牌篡改、XXE 拒绝、URL 白名单、审计日志、
Dublin Core 归一化、幂等入库、增量筛选、断点续采、报告完整性校验、CLI 和完整 demo 流程。
全部测试只访问进程内的 loopback mock。

## 目录

```
configs/                 mock 与收割配置
fixtures/                合成种子记录、XXE 夹具
src/metaharvest/         CLI、协议、解析、归档、数据库、报告、mock 与 demo
tests/                   pytest 测试
```

所有 fixture 和示例记录均为合成内容，仅使用 `demo.invalid`、`127.0.0.1`、
`admin@demo.invalid` 等保留/合成标识。项目不实现远程仓储采集，也不会访问演示 mock 以外的目标。
