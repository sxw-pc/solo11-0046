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
