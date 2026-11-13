"""Synthetic, offline demo files written by the seed command."""

DEMO_FILES: dict[str, str] = {
    "config.yaml": """\
project: line-hello-demo
sql_dir: sql
contracts_dir: contracts
out_dir: out
default_schema: main
server:
  host: 127.0.0.1
  port: 5000
frozen_time: "2024-06-01T00:00:00Z"
severity:
  unexpected_upstream: info
max_query_depth: 8
""",
    "sql/01_sources.sql": """\
-- Synthetic source tables for the offline demo.
CREATE TABLE raw.orders (
  order_id BIGINT PRIMARY KEY,
  customer_id BIGINT NOT NULL,
  order_ts TIMESTAMP NOT NULL,
  gross_amount DECIMAL(12,2) NOT NULL,
  net_amount DECIMAL(12,2) NOT NULL,
  currency VARCHAR(3) NOT NULL,
  note VARCHAR(80)
);

CREATE TABLE raw.customers (
  customer_id BIGINT PRIMARY KEY,
  region VARCHAR(16) NOT NULL,
  segment VARCHAR(24)
);
""",
    "sql/02_marts.sql": """\
CREATE TABLE mart.daily_revenue (
  order_date DATE NOT NULL,
  revenue DECIMAL(18,2) NOT NULL,
  order_count BIGINT NOT NULL
);

INSERT INTO mart.daily_revenue (order_date, revenue, order_count)
SELECT CAST(order_ts AS DATE) AS order_date,
       SUM(net_amount) AS revenue,
       COUNT(*) AS order_count
FROM raw.orders
GROUP BY CAST(order_ts AS DATE);

CREATE TABLE mart.customer_revenue (
  customer_id BIGINT NOT NULL,
  region VARCHAR(16) NOT NULL,
  revenue DECIMAL(18,2) NOT NULL
);

INSERT INTO mart.customer_revenue (customer_id, region, revenue)
SELECT o.customer_id,
       c.region,
       SUM(o.net_amount) AS revenue
FROM raw.orders AS o
JOIN raw.customers AS c ON o.customer_id = c.customer_id
GROUP BY o.customer_id, c.region;
""",
    "sql/03_rollup.sql": """\
CREATE TABLE mart.customer_rollup AS
SELECT customer_id,
       region,
       SUM(revenue) AS lifetime_revenue
FROM mart.customer_revenue
GROUP BY customer_id, region;

CREATE TABLE mart.customer_latest (
  customer_id BIGINT PRIMARY KEY,
  region VARCHAR(16) NOT NULL,
  lifetime_revenue DECIMAL(18,2)
);

MERGE INTO mart.customer_latest AS t
USING mart.customer_rollup AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE
  SET t.region = s.region,
      t.lifetime_revenue = s.lifetime_revenue
WHEN NOT MATCHED THEN INSERT (customer_id, region, lifetime_revenue)
  VALUES (s.customer_id, s.region, s.lifetime_revenue);
""",
    "sql/04_star.sql": """\
CREATE TABLE mart.orders_copy AS
SELECT * FROM raw.orders;
""",
    "sql/05_maintenance.sql": """\
ALTER TABLE raw.orders ADD COLUMN channel VARCHAR(16);
DROP TABLE IF EXISTS mart.customer_rollup;
""",
    "contracts/daily.yaml": """\
version: 1
contracts:
  - target: mart.daily_revenue.order_date
    required_upstream:
      - raw.orders.order_ts
    expected_type: date
    assertions: [not_null]
    description: One row per UTC order day.
  - target: mart.daily_revenue.revenue
    required_upstream:
      - raw.orders.net_amount
    expected_type: numeric
    assertions: [not_null]
  - target: mart.daily_revenue.order_count
    expected_type: bigint
""",
    "contracts/customers.yaml": """\
version: 1
contracts:
  - target: mart.customer_revenue.customer_id
    required_upstream:
      - raw.orders.customer_id
    expected_type: integer
  - target: mart.customer_revenue.region
    required_upstream:
      - raw.customers.region
    expected_type: string
  - target: mart.customer_revenue.revenue
    required_upstream:
      - raw.orders.net_amount
    expected_type: numeric
  - target: mart.customer_rollup.lifetime_revenue
    required_upstream:
      - mart.customer_revenue.revenue
    expected_type: numeric
  - target: mart.customer_latest.lifetime_revenue
    required_upstream:
      - mart.customer_rollup.lifetime_revenue
    expected_type: numeric
  - target: mart.orders_copy.note
    required_upstream:
      - raw.orders.note
    expected_type: string
""",
}
