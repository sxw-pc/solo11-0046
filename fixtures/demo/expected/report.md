# Lineage Inquest report

- Project: `line-hello-demo`
- Run: `run-d9d14b33f6d4`
- Generated (UTC): `2024-06-01T00:00:00Z`
- Input digest: `4fc04d8bb371d181a1e9785d4b7b8a4c9d5e7e3251b04c2c001c4bb5b673c550`
- Config digest: `1d7aa3a9e62a0055e43d352f46c0200f3934c30dc88b9505f3d04eb65e3a47c5`

## Overview

| Tables | Nodes | Edges | Findings | Unsupported | Scripts | Statements |
|---:|---:|---:|---:|---:|---:|---:|
| 7 | 30 | 18 | 0 | 0 | 5 | 12 |

By severity: none

## Severity policy

| Category | Severity | Source |
|---|---|---|
| `missing_upstream` | error | default |
| `unexpected_upstream` | info | override |
| `type_mismatch` | error | default |
| `assertion_mismatch` | warn | default |
| `unsupported_sql` | warn | default |
| `cycle_detected` | error | default |
| `table_redefined` | warn | default |

## Findings

No findings.

## Cycle detection

No cyclic strongly connected components.

## Files

| Script | Statements | Supported | Unsupported | Edges | Findings |
|---|---:|---:|---:|---:|---:|
| `01_sources.sql` | 2 | 2 | 0 | 0 | 0 |
| `02_marts.sql` | 4 | 4 | 0 | 5 | 0 |
| `03_rollup.sql` | 3 | 3 | 0 | 6 | 0 |
| `04_star.sql` | 1 | 1 | 0 | 7 | 0 |
| `05_maintenance.sql` | 2 | 2 | 0 | 0 | 0 |

## Statement inventory

### `01_sources.sql`

- line 2, #1, `create_table`: **supported**
- line 12, #2, `create_table`: **supported**

### `02_marts.sql`

- line 1, #1, `create_table`: **supported**
- line 7, #2, `insert_select`: **supported**
- line 14, #3, `create_table`: **supported**
- line 20, #4, `insert_select`: **supported**

### `03_rollup.sql`

- line 1, #1, `ctas`: **supported**
- line 8, #2, `create_table`: **supported**
- line 14, #3, `merge`: **supported**

### `04_star.sql`

- line 1, #1, `ctas`: **supported**

### `05_maintenance.sql`

- line 1, #1, `alter_table`: **supported**
- line 2, #2, `drop_table`: **supported**

## Unsupported statements

None.

## Topology summary

| Column | Upstream edges | Downstream edges |
|---|---:|---:|
| `raw.orders.net_amount` | 0 | 3 |
| `mart.customer_revenue.customer_id` | 1 | 1 |
| `mart.customer_revenue.region` | 1 | 1 |
| `mart.customer_revenue.revenue` | 1 | 1 |
| `mart.customer_rollup.customer_id` | 1 | 1 |
| `mart.customer_rollup.lifetime_revenue` | 1 | 1 |
| `mart.customer_rollup.region` | 1 | 1 |
| `raw.orders.customer_id` | 0 | 2 |
| `raw.orders.order_ts` | 0 | 2 |
| `mart.customer_latest.customer_id` | 1 | 0 |
