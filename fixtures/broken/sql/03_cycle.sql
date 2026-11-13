INSERT INTO mart.ledger_a (id)
SELECT id FROM mart.ledger_b;

INSERT INTO mart.ledger_b (id)
SELECT id FROM mart.ledger_a;
