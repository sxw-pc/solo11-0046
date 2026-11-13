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
