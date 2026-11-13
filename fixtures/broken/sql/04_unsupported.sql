INSERT INTO mart.revenue (day, total_amount, source_note, order_count)
SELECT CAST(booked_at AS DATE) AS day,
       net_amount,
       note,
       ROW_NUMBER() OVER (ORDER BY booked_at) AS order_count
FROM raw.orders;

WITH RECURSIVE seq(n) AS (
  SELECT 1
  UNION ALL
  SELECT n + 1 FROM seq WHERE n < 10
)
SELECT n FROM seq;

SELECT o.order_id, x.note
FROM raw.orders AS o,
LATERAL (SELECT o.note AS note) AS x;

SELECT order_id, p."1"
FROM raw.orders
PIVOT (SUM(net_amount) FOR currency IN ('USD', 'EUR')) AS p;
