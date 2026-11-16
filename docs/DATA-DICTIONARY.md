# 定义 fieldmap

本文件是字段含义与计算方式的唯一出处；页面只显示字段名、状态与数值。

## 1 字段身份

- `field_id`：字段身份，字符串，跨版本不变。改名不产生新身份。
- 无 `field_id` 时按 `sha256(表名|列名)` 取前 12 位派生，前缀 `auto:`，与行序无关。
- 同一身份在一个版本里出现两次：第二次进隔离区，原因码 `FIELD_ID_DUP`。
- 同表列名比较不分大小写；保留首次出现的列，重复列进隔离区。
- 身份是 2^53 以上的整数时一律按字符串保存与导出，禁止过 `float`。

## 2 类型格

一个类型归一成 `{族, 基础类型, 精度, 标度, 长度}`，族为 numeric / text / temporal / bool / unknown。

每个数值类型带四个 Decimal 常量：

| 常量 | 含义 |
|---|---|
| `lo` / `hi` | 可表示的取值范围 |
| `quantum` | 最小刻度，例如 decimal(18,2) 为 0.01 |
| `exact` | 能不丢精度表示的最大整数绝对值，double 为 2^53 |

从旧类型到新类型判为放宽（`widen`）当且仅当四件事同时成立：`new.lo <= old.lo`、`new.hi >= old.hi`、`new.quantum <= old.quantum`、`new.exact >= old.exact`。否则判收窄（`narrow`），并给出细分：

| 细分 | 触发条件 |
|---|---|
| `int_digits_drop` | decimal 的整数位（精度减标度）变少 |
| `scale_drop` | decimal 的小数位变少 |
| `range_narrow` | 取值范围变小 |
| `precision_loss` | 刻度变粗 |
| `exactness_loss` | 精确整数范围变小 |
| `length_narrow` | 字符长度上限变小 |
| `temporal_narrow` | 时间精度回退（timestamp 到 date） |

跨族（数值到字符等）判 `cross_family`；任一端不在类型格内判 `unknown`。三种都算破坏。

## 3 必填与两向结论

- `required=true → false`：`REQUIRED_LOOSENED`，读者与写者都通过。
- `required=false → true`：`REQUIRED_TIGHTENED`，写者不通过，读者通过。
- 读者向看名字能否落到同一身份、类型能否解释；写者向看类型是否容纳与是否新增约束。
- 结论三档：`COMPATIBLE`、`COMPATIBLE_BY_MAPPING`（需要建别名视图）、`BREAKING`。

## 4 时间与区间

- 存储一律 UTC，页面按 `+08:00` 显示。
- 生效区间是半开区间 `[start, end)`；`end` 为空表示不设终点。
- 重叠判定：`left.start < right.end` 且 `right.start < left.end`，缺端点按无穷远。
- 端点相邻（左区间终点等于右区间起点）不算重叠。

## 5 引用

- 下游登记给出 `sql`、`upstream`、`downstream`；SQL 里只抽 `别名.列名`。
- 解析必须过身份：先按 `(表, 列名)` 找到旧版本字段，再按 `field_id` 找新版本字段。
- 名字还在但身份换了的，判断链 `REF_BROKEN`，不允许悄悄绑到新列。
- 改名同时换类型的字段不生成别名，引用同样判断链。
- 上下游任一边缺登记即 `REF_ONE_SIDED`。

## 6 采信

- 编码回退链：`utf-8-sig` → `utf-8` → `gb18030`，先剥字节序标记再探测。
- 逻辑行按 `\n` 切分并去掉行尾 `\r`，行号从 1 开始，保留原始行号。
- 阻断行（列定义损坏、重复列、重复身份）不进判定，提示行（派生身份、未知类型、编码回退）照常参算。
- 空文件、仅表头、全空行都返回 `EMPTY_INPUT` 提示，不当作成功采信。

## 7 原因码

原因码唯一来源是 `app/reasons.py`，级别 `info / warn / break`。主原因按 `app/modules/m3_compat/priority.py` 的优先级表取，同优先级按码字典序，保证重排输入结论不变。

## 8 账目

- 旧版本字段数 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 删除
- 新版本字段数 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 新增
- 登记引用 = 可达 + 断链；可达 = 直接 + 经别名
- 列定义行 = 接受字段 + 阻断行

每个字段只进一个桶，所以四个等式成立是必要条件；差额不为 0 即判定漏项。

## 9 影响凭证

- 凭证对象是下游节点（视图 / 任务），每个节点在一次审计里恰好一条凭证，三档互斥：
  - `CONFIRMED` 已确认：节点自己有直接证据，或从已确认节点只经**双方一致边**传播到达。
  - `PENDING` 待补证：确认图走不到，但含单边边的联合图走得到；路径上至少一条 `REF_ONE_SIDED` 边，须对方补登记后才能确认，不得据此停任务。
  - `UNAFFECTED` 不受影响：联合图也走不到，本批次没有任何受影响路径。
- 直接证据只来自身份解析，与登记边无关，共两类：
  - `field`：SQL 引用经 `field_id` 解析到一个 `BREAKING` 字段，依据带 `field_id`、引用名与原因码。
  - `broken`：引用断链（`REF_BROKEN`：删除、名字被别的身份占用、改名同时换类型别名不安全）。
- 登记边核验：`A.downstream` 含 `B` 且 `B.upstream` 含 `A` 才算 `mutual`；只有一边登记为 `one_sided`，记录 `declared` 与 `missing`。单边边进联合图不进确认图。
- 传播沿 `上游 → 下游` 反向闭包，环与自引用靠 visited 截止，深度取最短跳数，同层按节点名字典序，重排输入结论不变。
- 每条凭证带 `path`（种子到本节点的节点序列）、`path_edges`（逐边核验结果）与 `basis`（路径上全部去重依据字段，含 `field_id` / 引用 / 原因码）。
- 凭证号 `evidence_id = ev- + sha256(批次键, 节点, 结论, 路径, 依据, 路径边)` 前 16 位；登记不变重跑同一审计，凭证号与行数不变，库内按 `(version_id, evidence_id)` 主键去重，不产生重复凭证。
- 凭证版本分 `draft / published / superseded`。发布在单事务内把旧 published 置 superseded、草稿置 published；任一步失败整体回滚，旧版凭证与 `runs` 批次记录原样保留。
- 守恒：已确认 + 待补证 + 不受影响 = 下游节点总数。
