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
| `catalog refresh` | 分页抓取完整 `ListSets`，发布新的仓储集合目录代 |
| `catalog show` | 查看 active 目录代、成员状态与逐页证据 |
| `harvest` | 执行一次收割；支持 `--from`、`--until`、`--set`、`--metadata-prefix`、`--max-pages` |
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

## 集合目录与采集准入

`--set` 会作为 OAI-PMH `ListRecords` 的 `set` 过滤条件，决定一次运行实际能写入哪些记录。
但 `ListRecords` 对不存在的 set 通常只返回 `noRecordsMatch`，与“合法但为空的日期范围”无法区分。
因此本工具按**仓储身份**（OAI endpoint URL）维护集合目录，并在创建 run 之前做准入：

```bash
# 1) 先抓取完整目录（自动跟随 resumptionToken，所有分页成功后才发布新代）
PYTHONPATH=src python -m metaharvest catalog refresh --config configs/harvest_demo.yml
PYTHONPATH=src python -m metaharvest catalog show --config configs/harvest_demo.yml

# 2) 再按已发布目录中的 set 采集
PYTHONPATH=src python -m metaharvest harvest --config configs/harvest_demo.yml --set theses
```

规则：

- 目录分页**全部成功后才整代发布**。网络、XML 或协议失败不会替换上一次的 active 目录。
- `ListSets` 返回 `noRecordsMatch` 表示“完整但为空的目录”，会发布一个 0 成员的 active 代。
  这与 `ListRecords` 的 `noRecordsMatch`（合法的 0 记录完成运行）语义不同。
- 每个 `setSpec` 在一个目录代里有明确状态：
  - `available`：所有分页上的名称/说明一致，可直接用于新 run；
  - `conflict`：同一 `setSpec` 在不同页给出不同 `setName`/`setDescription`，身份不唯一；
  - `invalid`：`set` 元素缺少可用 `setSpec`，永不可选。
- 目录未同步、set 不在 active 目录、或冲突 set 未固定说明版本时，`harvest --set` 会在
  **发请求/创建 run 之前**报错退出（兼容开关见下）。
- 同一 `setSpec` 在另一个仓储下不会通过准入；目录按 endpoint URL 隔离。
- 冲突处理策略（`harvest.set_catalog.conflict_policy`）：
  - `strict`（路线 A，默认）：存在冲突时整代不激活，保留旧 active 目录继续用于准入；
  - `active`（路线 B）：激活目录代，冲突 set 隔离不可选，其他 set 立即可用；冲突 set 只有
    显式指定 `--set-name`/`--set-description` 或 `--set-version` 固定一个说明版本后才可用。
- 兼容开关 `--allow-unverified-set`（或 `harvest.set_catalog.verify_sets: false`）允许直接
  透传 `--set` 而不做目录校验，但新 run 会以 `set_catalog_status = unverified` 明确标记。
- 每个已在目录中验证的 run 保存当时的目录代、set 名称/说明/状态和说明版本哈希；即使目录后来
  变化或该 set 被删除，旧 run 仍能解释自己的采集范围。`report` 会输出这份快照。

删除的 set 会从新 run 的可选范围中消失（准入拒绝），但历史 run 的快照不受影响。

## 协议与数据能力

- OAI-PMH 2.0 verbs：`Identify`、`ListMetadataFormats`、`ListSets`、`ListIdentifiers`、
  `ListRecords`、`GetRecord`。
- 仅支持 `oai_dc`；支持 `from`、`until`、`set` 过滤和 HTTP GET/POST 请求。
- mock 分页通过带 HMAC 签名和过期时间的 `resumptionToken` 实现（`ListSets`、`ListRecords`、
  `ListIdentifiers`），篡改、过期、跨 verb 使用均报 `badResumptionToken`。
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
| `harvest_runs` | 每次运行的参数、状态、检查点、页数、结果计数与目录快照 |
| `records` | 按 OAI identifier 幂等保存的归一化记录和内容哈希 |
| `raw_responses` | 每页原始 XML 的归档路径、哈希和 HTTP 状态 |
| `events` | 运行、页面和记录级事件 |
| `errors` | 按运行/记录隔离的错误，供报告复核 |
| `set_catalog_generations` | 每个仓储的目录代（active / superseded / rejected）及汇总 |
| `set_catalog_members` | 每代中按 `setSpec` 去重后的成员状态 |
| `set_catalog_occurrences` | 每代中每个 set 在每一页上的原始名称/说明，作为冲突证据 |
| `set_catalog_pages` | 目录抓取逐页的 URL、HTTP 状态与 SHA-256 |

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
Dublin Core 归一化、幂等入库、增量筛选、断点续采、报告完整性校验、CLI 和完整 demo 流程，
以及集合目录的分页续传、跨页重复/冲突、层级缺父、空目录、失败保代、两路线准入、仓储隔离、
删除后旧 run 快照和兼容绕过标记。
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
