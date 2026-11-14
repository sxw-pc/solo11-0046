-- revenue depends on two raw measures: net_amount is the declared source,
-- gross_amount is the undeclared branch used by the negative contracts.
INSERT INTO mart.customer_revenue (customer_id, revenue)
SELECT customer_id, SUM(net_amount) + SUM(gross_amount)
FROM raw.orders
GROUP BY customer_id;

CREATE TABLE mart.customer_rollup AS
SELECT customer_id, SUM(revenue) AS lifetime_revenue
FROM mart.customer_revenue
GROUP BY customer_id;

CREATE TABLE mart.customer_latest (
  customer_id BIGINT PRIMARY KEY,
  lifetime_revenue DECIMAL(18,2)
);

MERGE INTO mart.customer_latest AS t
USING mart.customer_rollup AS s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE
  SET t.lifetime_revenue = s.lifetime_revenue
WHEN NOT MATCHED THEN INSERT (customer_id, lifetime_revenue)
  VALUES (s.customer_id, s.lifetime_revenue);
