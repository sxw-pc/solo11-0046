-- Synthetic fixtures for transitive upstream contracts: a two-hop chain
-- plus a second, undeclared source branch feeding the same mart table.
CREATE TABLE raw.orders (
  order_id BIGINT PRIMARY KEY,
  customer_id BIGINT NOT NULL,
  net_amount DECIMAL(12,2) NOT NULL,
  gross_amount DECIMAL(12,2) NOT NULL,
  note VARCHAR(80)
);

CREATE TABLE mart.customer_revenue (
  customer_id BIGINT NOT NULL,
  revenue DECIMAL(18,2) NOT NULL
);
