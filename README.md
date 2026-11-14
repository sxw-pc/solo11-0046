# 谱系勘验台（Lineage Inquest）

一个**纯静态、纯离线**的 SQL 字段级血缘抽取与契约勘验工具。它读取本地
`.sql` 脚本和 YAML 血缘契约，用 sqlglot 构建 AST，重建
`schema.table.column` 级别的上下游图谱，对账声明与实际血缘，输出可复核的
JSON / Markdown / 自包含 HTML 报告及 SQLite 台账。

- 不执行 SQL，不连接数据库，不读数据内容。
- 不调度任务，不监听文件变化，不做增量扫描。
- 运行期不访问网络，不引用 CDN，不依赖外部服务、队列或云存储。
- Flask 服务只读、仅绑定 `127.0.0.1`。

## 1. 定位与适用边界

### 是

- 面向本地 Git 目录中散落 SQL 脚本的小团队离线工具。
- 输入：`.sql` 文件（DDL + 产出语句）和本地 `contracts/*.yaml`。
- 输出：字段级血缘图、契约 finding、报告、历史台账、两次 run 的差异。
- 适合：上线前检查字段改名影响面、核对“团队声明血缘”与“脚本真实血缘”、
  在断网环境中做可重复的静态回归。

### 不是

- 不是 SQL 引擎，不会执行脚本或采样数据。
- 不是数据质量平台；`assertions` 只比较静态 DDL 元数据，不验证真实数据。
- 不是通用 SQL 方言解析器；只支持第 3 节列出的受限子集。
- 不是元数据服务、调度系统或线上血缘采集器。
- 不是 Web CRUD 后台；页面无登录、无编辑、无写接口。

## 2. 快速上手

依赖：Python 3.10+；直接依赖已锁定为
`sqlglot==30.18.0`、`PyYAML==6.0.2`、`Flask==3.0.3`、`pytest==8.3.5`
（见 `requirements.txt`）。开发与验证环境为 Python 3.13.15。

### 离线安装

在有 wheel 的机器上准备全部直接与传递依赖的 wheel，将 `wheels/` 目录随仓库
带到目标机器，然后执行：

```bash
python -m venv .venv
python -m pip install --no-index --find-links wheels -r requirements.txt
```

安装过程不使用网络。若本机已具备上述精确版本，也可直接运行。

### 五条命令

```bash
# 1. 幂等生成合成演示数据；重复执行会得到相同字节
python -m lineage_inquest seed --out .work/demo

# 2. 扫描 SQL、构建图谱、勘验契约并写出全部产物
python -m lineage_inquest scan --config fixtures/demo/config.yaml

# 3. 扫描负样本并核对 findings 期望快照（不一致时退出码 1）
python -m lineage_inquest verify --config fixtures/broken/config.yaml

# 4. 启动只读本地查看服务（仅 127.0.0.1，默认端口 5000）
python -m lineage_inquest serve --config fixtures/demo/config.yaml

# 5. 运行离线测试（测试内置外网阻断）
pytest -q
```

`scan` / `verify` 支持 `--out` 覆盖输出目录、`--frozen-time` 固定报告与台账
时间。`verify` 还可用 `--expect` 指定 findings 快照；未指定时依次使用配置中的
`expect_findings` 或配置目录下的 `expected/findings.json`。

`serve` 通过已有台账读数据，不会重新扫描。可用 `--host` / `--port` 覆盖配置，
但 host 必须严格等于 `127.0.0.1`，否则拒绝启动并打印原因。

### 依赖的 fixture 命令

仓库自带：

- `fixtures/demo/`：干净的正样本，扫描结果为 0 finding、0 unsupported。
- `fixtures/broken/`：包含契约冲突、字段环、重复建表、窗口函数、递归 CTE、
  LATERAL、PIVOT 等负样本，并带期望快照。

## 3. 支持的 SQL 子集

解析器使用 sqlglot 的通用 SQL 语法，支持以下**明确列出的子集**：

- `CREATE TABLE`：列名、类型字符串、`PRIMARY KEY`（列级或表级）、
  `NOT NULL`、`UNIQUE`、`CHECK (col >= min AND col <= max)` 的范围元数据。
- `CREATE TABLE ... AS SELECT`：列从 SELECT 推导，节点标记 `derived`。
- `INSERT INTO target [(columns)] SELECT ...`。
- `MERGE INTO target USING source ON ...`，含 `WHEN MATCHED THEN UPDATE SET`
  和 `WHEN NOT MATCHED THEN INSERT ... VALUES`。
- `DROP TABLE`：按文件名、文件内行号顺序移除后续语句可见的表。
- `ALTER TABLE ... ADD COLUMN`：按同样顺序扩充表结构。
- 上述产出语句中的 `SELECT`：JOIN、表别名、列别名、非递归 CTE、表达式、
  聚合函数、常量列、`SELECT *`（可解析来源表时展开）。
- 简单标识符大小写归一化、双引号/反引号/方括号标识符；两段表名使用
  `default_schema` 补全。

### 明确不支持

- 窗口函数（`OVER`）、递归 CTE、`LATERAL`、`PIVOT`/`UNPIVOT`、`QUALIFY`、
  `UNNEST` 及其他表函数。
- `UNION` / `INTERSECT` / `EXCEPT` 等集合运算。
- `VALUES` 作为行来源（包括 `INSERT ... VALUES`）。
- `UPDATE`、`DELETE`、`CREATE VIEW`、存储过程、控制流、动态 SQL。
- 无法确定来源表的 `SELECT *`、未知目标列、插入列数与 SELECT 列数不一致、
  未知 `ALTER TABLE` 目标、无法解析的语句。

**不支持时的行为约定**：不猜测列关系，不静默跳过。每条不支持项都记录
`script`、`line`、`construct`、`reason`、`snippet`，进入 `unsupported` 列表、
`unsupported_sql` finding、Markdown / HTML 报告与 SQLite `unsupported` 表。
只有通过守卫且能解析的语句才参与血缘建模。

## 4. 契约 YAML

契约目录中的 `.yaml` / `.yml` 按相对路径排序加载。根字段只能是 `version`
和 `contracts`；目前只接受 `version: 1`。每个 target 只能定义一次。

| 字段 | 必填 | 说明 |
|---|---|---|
| `target` | 是 | 目标列，`schema.table.column`；两段写法用 `default_schema` 补全 |
| `required_upstream` | 否 | 允许的直接上游列白名单；空表示不检查缺失 |
| `forbidden_upstream` | 否 | 禁止的直接上游；与 required 重合会报配置错误 |
| `expected_type` | 否 | 期望类型；支持 `integer`/`numeric`/`string`/`boolean`/`date`/`timestamp` 家族、`any`/`*` 或精确类型文本 |
| `assertions` | 否 | 静态断言列表：`not_null`、`unique`、`range:[min,max]`（范围建议写成带引号字符串） |
| `description` | 否 | 仅供人工阅读，不参与比对 |

完整合成示例：

```yaml
version: 1
contracts:
  - target: mart.daily_revenue.revenue
    required_upstream:
      - raw.orders.net_amount
    forbidden_upstream:
      - raw.orders.legacy_amount
    expected_type: numeric
    assertions:
      - not_null
      - "range:[0,99999999.99]"
    description: Declared daily revenue derivation.
  - target: mart.customer_revenue.region
    required_upstream:
      - raw.customers.region
    expected_type: string
```

比对语义：

- 只比较**直接上游边**，不自动展开传递闭包。若契约要求传递来源，请显式列出
  直接来源列，或在后续迭代中扩展。
- 任何未被 `required_upstream` 列出的实际直接来源都会产生
  `unexpected_upstream`；`forbidden_upstream` 命中会写入该 finding 的 detail。
- target 不在图中、或必需来源缺失，产生 `missing_upstream`。
- `expected_type` 与静态推导/DDL 类型按类型家族或精确文本比较，不符时产生
  `type_mismatch`。
- `assertions` 必须已经出现在目标列的 DDL 静态元数据中；CTAS 派生列默认没有
  断言，会因此产生 `assertion_mismatch`。**不校验数据。**
- finding ID 由类别、目标和详情内容哈希生成；内容相同的 finding 会去重。
- 七类 finding 的默认严重级别为：`missing_upstream=error`、
  `unexpected_upstream=warn`、`type_mismatch=error`、
  `assertion_mismatch=warn`、`unsupported_sql=warn`、`cycle_detected=error`、
  `table_redefined=warn`。配置的 `severity` 可覆写，报告会标明 `override`。

## 5. 输出与台账

默认写往配置的 `out_dir`（相对配置文件解析）；`--out` 可覆盖。

### `graph.json`

无时间戳，字段稳定排序。`version`、`project`、`input_digest`、`stats`；
`nodes` 每项含：

- `id`：节点内容哈希 ID，形如 `n-...`；
- `name`：全名 `schema.table.column`；
- `table`、`column`；
- `data_type`、`nullable`、`primary_key`；
- `origin`：`ddl`、`derived` 或 `referenced`；
- `assertions`：静态断言列表。

`edges` 每项含：

- `id`：由 source、target、kind 生成的内容哈希；
- `source`、`target`；
- `kind`：`direct`、`expression` 或 `aggregate`（常量不产生边）；
- `scripts`：贡献该边的脚本名，升序去重；
- `locations`：每项含 `script`、`line`、`statement`。

节点按全名排序；边按 `(target, source, kind)` 排序。同一 `(source, target,
kind)` 的重复边去重，但保留全部来源脚本和语句位置。

### `findings.json`

无时间戳。`summary.total`、`by_severity`、`by_category`、
`severity_overrides`；`findings` 每项含 `id`、`category`、`severity`、
`default_severity`、`overridden`、`target`、`message`、`detail`、`script`、
`line`。排序为严重级别、类别、目标、详情。

### `report.md` / `report.html`

包含概览计数、严重级别策略（含配置覆写）、finding 表与可折叠详情、按脚本
统计、逐条语句清单（supported / unsupported 及原因）、不支持项、拓扑摘要。
HTML 使用内联 CSS、无 JavaScript、无外链、无 CDN，可直接离线打开。
报告头部含生成时间。

### SQLite 台账 `ledger.sqlite`

固定表：`run`、`node`、`edge`、`finding`、`unsupported`；另有查询索引。

- `run`：`run_id`、`project`、`created_at`、`input_digest`、`config_digest`、
  各计数、`summary_json`、`severity_json`、`overrides_json`。
- `node`：run 外键、全名、表/列名、类型、可空、主键、origin、
  `assertions_json`。
- `edge`：run 外键、edge ID、source、target、kind、`locations_json`。
- `finding`：run 外键、finding ID、category、severity、default_severity、
  overridden、target、message、detail、script、line。
- `unsupported`：run 外键、seq、script、line、construct、reason、snippet。

相同 `run_id` 重写时先删除旧子行再写入，保证幂等；不同输入/时间形成不同 run。

### 其他命令输出

- `export --run-id RUN`：导出 `{run, nodes, edges, findings, unsupported}`
  快照到 `<out>/exports/RUN.json`，可用 `--output` 指定路径。
- `diff --base A --target B`：输出新增/消除的 finding 与边及计数。
- `impact --base A --target B`：对 diff 的每条新增/消除边，从边的 target
  端沿 target 图向下游枚举到无出边的终端列，给出按确定规则去重排序的列名
  路径集合；removed 边起点以 base 图判定，target 图中缺失的节点不可达、
  不产链。可用 `--output` 落盘。
- `query --node ... --direction upstream|downstream --depth N`：输出
  `root`、`direction`、`depth`、`truncated`、带 `hop` 的节点和边。

## 6. 确定性与 `--frozen-time`

- SQL 文件、契约文件、产物内容与图元素均有稳定排序。
- `graph.json` 与 `findings.json` 不含时间戳，同一输入重复运行字节一致。
- 图 ID 用内容哈希，不受文件位置或运行时间影响。
- 报告头部和台账 `run.created_at` 使用 UTC 秒级时间戳；未固定时自然变化。
- 配置 `frozen_time` 可固定时间；CLI `--frozen-time` 优先。格式为 ISO-8601，
  例如 `2024-06-01T00:00:00Z`（无时区按 UTC）。
- run ID 由输入摘要、配置摘要和生成时间计算，因此固定时间下重复扫描的 run ID
  也一致；未固定时间时 run ID 会变化，但图与 findings 的字节仍保持稳定。
- 文本文件统一 UTF-8、LF 换行写出。

## 7. 已知限制

- 契约只比较直接上游；没有隐式传递闭包、列合并或阈值匹配。
- 方言覆盖是 sqlglot 通用语法上的**受限子集**，不做多方言适配；不支持项只报告，
  不进行部分推断。
- `SELECT *` 依赖已解析的来源表结构；来源未知或列不可确定时记 unsupported。
- 类型信息来自 DDL 或有限的静态推导；复杂表达式可能得到 `unknown`。
- CTAS 派生列不继承源列断言；这会有意触发 assertion 缺失，而不是猜测。
- 顺序模型只按相对文件名和文件内行号执行，不分析跨文件控制流、宏或模板。
- 重复建表以后一个定义建模，同时产生 `table_redefined`；DROP 只影响后续语句。
- 环检测按字段级 SCC 报告；环中的 finding target 为字典序最小的字段。
- 服务无鉴权，仅依赖严格回环绑定；不应暴露到其他主机。
- HTML 只提供表格和文字拓扑摘要，不包含交互式图可视化。

## 8. 安全与合规声明

- 全部 SQL、表名、列名、契约和报告样例均为合成名称，不含真实公司名、
  真实凭据、令牌、密钥、真实域名或外部目标地址。
- 仓库不包含 `.env`、凭据文件或连接串；工具本身不读取数据库配置。
- 运行期没有任何网络请求、CDN、消息队列、云存储或外部服务依赖。
- 服务仅监听 `127.0.0.1`；非该地址一律拒绝启动。
- 测试通过 autouse 夹具在进程内阻断外网连接（保留回环），并包含显式自检，
  证明外网访问会失败；全部测试在普通笔记本上数秒内完成。
- 本工具只做静态文本分析，不执行输入 SQL，不产生数据写入或外部副作用。
