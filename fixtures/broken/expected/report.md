# Lineage Inquest report

- Project: `line-hello-broken`
- Run: `run-3acaed7dd33e`
- Generated (UTC): `2024-06-02T00:00:00Z`
- Input digest: `14ba5bd3727acd1c6f7e9fc7418cabdf99c0a3fc1e661600f4c6cbcaebb8badb`
- Config digest: `5fb7f3df5bd1d38072aa0c63d09392ea059839432b3982240286d9f7fc2b019c`

## Overview

| Tables | Nodes | Edges | Findings | Unsupported | Scripts | Statements |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 14 | 5 | 11 | 5 | 4 | 12 |

By severity: error=2, warn=9

## Severity policy

| Category | Severity | Source |
|---|---|---|
| `missing_upstream` | error | default |
| `unexpected_upstream` | warn | default |
| `type_mismatch` | warn | override |
| `assertion_mismatch` | warn | default |
| `unsupported_sql` | warn | default |
| `cycle_detected` | error | default |
| `table_redefined` | warn | default |

## Findings

| Severity | Category | Target | Message |
|---|---|---|---|
| error | `cycle_detected` | `mart.ledger_a.id` | a cycle was detected in the column lineage graph |
| error | `missing_upstream` | `mart.revenue.total_amount` | 1 required upstream column(s) are not direct sources |
| warn | `assertion_mismatch` | `mart.revenue.day` | declared assertions are not present in DDL/static metadata |
| warn | `table_redefined` | `raw.orders` | table is defined more than once |
| warn | `type_mismatch` | `mart.revenue.total_amount` | declared type does not match the statically extracted type |
| warn | `unexpected_upstream` | `mart.revenue.total_amount` | column has upstream sources outside the declared allow-list |
| warn | `unsupported_sql` | `04_unsupported.sql:10:set_operation` | unsupported SQL construct: set_operation |
| warn | `unsupported_sql` | `04_unsupported.sql:17:lateral` | unsupported SQL construct: lateral |
| warn | `unsupported_sql` | `04_unsupported.sql:21:pivot` | unsupported SQL construct: pivot |
| warn | `unsupported_sql` | `04_unsupported.sql:5:window_function` | unsupported SQL construct: window_function |
| warn | `unsupported_sql` | `04_unsupported.sql:8:recursive_cte` | unsupported SQL construct: recursive_cte |

### Finding details

<details><summary><code>f-480494a53ae5ddc1</code> cycle_detected</summary>

- Target: `mart.ledger_a.id`
- Location: `contract/global`
- Detail: cycle: mart.ledger_a.id, mart.ledger_b.id

</details>

<details><summary><code>f-233609e49c117955</code> missing_upstream</summary>

- Target: `mart.revenue.total_amount`
- Location: `broken.yaml:0`
- Detail: missing: raw.orders.gross_amount

</details>

<details><summary><code>f-0b53e4c46806d783</code> assertion_mismatch</summary>

- Target: `mart.revenue.day`
- Location: `broken.yaml:0`
- Detail: missing assertions: not_null

</details>

<details><summary><code>f-102db59fc2b603e1</code> table_redefined</summary>

- Target: `raw.orders`
- Location: `01_schema.sql:11`
- Detail: redefined at 01_schema.sql:11; previous: 01_schema.sql:2

</details>

<details><summary><code>f-0b5c7f8d4e99cf9c</code> type_mismatch</summary>

- Target: `mart.revenue.total_amount`
- Location: `broken.yaml:0`
- Detail: expected: numeric; actual: VARCHAR(20)

</details>

<details><summary><code>f-a5ca4c81012c4302</code> unexpected_upstream</summary>

- Target: `mart.revenue.total_amount`
- Location: `broken.yaml:0`
- Detail: unexpected: raw.orders.net_amount; forbidden: raw.orders.net_amount

</details>

<details><summary><code>f-4f1423ee8af7d835</code> unsupported_sql</summary>

- Target: `04_unsupported.sql:10:set_operation`
- Location: `04_unsupported.sql:10`
- Detail: UNION/INTERSECT/EXCEPT are not supported

</details>

<details><summary><code>f-e267c70dba967d8f</code> unsupported_sql</summary>

- Target: `04_unsupported.sql:17:lateral`
- Location: `04_unsupported.sql:17`
- Detail: LATERAL sources are not supported

</details>

<details><summary><code>f-15acf6d4a06c119a</code> unsupported_sql</summary>

- Target: `04_unsupported.sql:21:pivot`
- Location: `04_unsupported.sql:21`
- Detail: PIVOT/UNPIVOT is not supported

</details>

<details><summary><code>f-ed8a0abfa802f806</code> unsupported_sql</summary>

- Target: `04_unsupported.sql:5:window_function`
- Location: `04_unsupported.sql:5`
- Detail: window functions (OVER) are outside the supported subset

</details>

<details><summary><code>f-d0bf97fc0fb965c5</code> unsupported_sql</summary>

- Target: `04_unsupported.sql:8:recursive_cte`
- Location: `04_unsupported.sql:8`
- Detail: recursive common table expressions are not supported

</details>

## Files

| Script | Statements | Supported | Unsupported | Edges | Findings |
|---|---:|---:|---:|---:|---:|
| `01_schema.sql` | 5 | 5 | 0 | 0 | 1 |
| `02_revenue.sql` | 1 | 1 | 0 | 3 | 0 |
| `03_cycle.sql` | 2 | 2 | 0 | 2 | 0 |
| `04_unsupported.sql` | 4 | 0 | 4 | 0 | 5 |

## Statement inventory

### `01_schema.sql`

- line 2, #1, `create_table`: **supported**
- line 11, #2, `create_table`: **supported**
- line 20, #3, `create_table`: **supported**
- line 27, #4, `create_table`: **supported**
- line 28, #5, `create_table`: **supported**

### `02_revenue.sql`

- line 1, #1, `insert_select`: **supported**

### `03_cycle.sql`

- line 1, #1, `insert_select`: **supported**
- line 4, #2, `insert_select`: **supported**

### `04_unsupported.sql`

- line 1, #1, `insert_select`: **unsupported** (window_function)
- line 8, #2, `select`: **unsupported** (recursive_cte)
- line 15, #3, `select`: **unsupported** (lateral)
- line 19, #4, `select`: **unsupported** (pivot)

## Unsupported statements

- `04_unsupported.sql:5` `window_function` — window functions (OVER) are outside the supported subset
- `04_unsupported.sql:8` `recursive_cte` — recursive common table expressions are not supported
- `04_unsupported.sql:10` `set_operation` — UNION/INTERSECT/EXCEPT are not supported
- `04_unsupported.sql:17` `lateral` — LATERAL sources are not supported
- `04_unsupported.sql:21` `pivot` — PIVOT/UNPIVOT is not supported

## Topology summary

| Column | Upstream edges | Downstream edges |
|---|---:|---:|
| `mart.ledger_a.id` | 1 | 1 |
| `mart.ledger_b.id` | 1 | 1 |
| `mart.revenue.day` | 1 | 0 |
| `mart.revenue.source_note` | 1 | 0 |
| `mart.revenue.total_amount` | 1 | 0 |
| `raw.orders.booked_at` | 0 | 1 |
| `raw.orders.net_amount` | 0 | 1 |
| `raw.orders.note` | 0 | 1 |
