-- =============================================================
-- Evaluation seed: runs after examples/schema.sql (Annex A), in a throwaway schema per case.
-- Every row exists to exercise a branch of an annex (see scenarios.yml).
-- =============================================================

INSERT INTO clientes (id, nome, cpf, data_cadastro, status) VALUES
    (1, 'Ana',   '00000000001', '2025-01-10', 'ATIVO'),
    (2, 'Bruno', '00000000002', '2025-02-10', 'ATIVO'),
    (3, 'Carla', '00000000003', '2025-03-10', 'INATIVO');   -- B/F: no active account

INSERT INTO contas (id, cliente_id, agencia, numero, tipo, saldo, status, data_abertura) VALUES
    (10, 1, '0001', '10-1', 'CORRENTE', 1000.00, 'ATIVA',     '2025-01-10'),
    (11, 1, '0001', '11-1', 'POUPANCA',  500.00, 'ATIVA',     '2025-01-10'),
    (12, 1, '0001', '12-1', 'SALARIO',   300.00, 'INATIVA',   '2025-01-10'),  -- B: not summed; D: inactive
    (20, 2, '0002', '20-2', 'CORRENTE',  200.00, 'ATIVA',     '2025-02-10'),
    (21, 2, '0002', '21-2', 'POUPANCA',    0.00, 'ATIVA',     '2025-02-10'),  -- C: never moved
    (22, 2, '0002', '22-2', 'SALARIO',   100.00, 'ATIVA',     '2025-02-10'),  -- C: idle for 45 days
    (30, 3, '0003', '30-3', 'CORRENTE',   50.00, 'ENCERRADA', '2025-03-10');

INSERT INTO taxas (id, tipo_operacao, percentual, valor_minimo, vigente_de, vigente_ate) VALUES
    (1, 'TRANSFERENCIA', 0.5000, 1.00, '2026-01-01', NULL),
    (2, 'SAQUE',         1.0000, 2.00, '2026-06-01', NULL),
    (3, 'SAQUE',         3.0000, 9.00, '2025-01-01', '2026-05-31'),  -- E: expired, must be ignored
    (4, 'DEPOSITO',      0.2500, 0.50, '2026-01-01', NULL);

INSERT INTO transacoes (id, conta_origem_id, conta_destino_id, tipo, valor, data_transacao, status) VALUES
    -- C: activity relative to now() (the original and the Python version see the same clock)
    (1, 10,   20,   'TRANSFERENCIA',  10.00, now() - interval '2 days',  'EFETIVADA'),
    (2, 22,   NULL, 'SAQUE',          15.00, now() - interval '45 days', 'EFETIVADA'),
    -- E: batch of 2026-09-15
    (101, 10,   20,   'TRANSFERENCIA', 100.00, '2026-09-15 10:00', 'EFETIVADA'),  -- fee = minimum 1.00
    (102, 10,   NULL, 'SAQUE',         454.50, '2026-09-15 11:00', 'EFETIVADA'),  -- 4.545 -> 4.55 -> x1.10 = 5.01
    (103, NULL, 11,   'DEPOSITO',      300.00, '2026-09-15 12:00', 'EFETIVADA'),  -- fee found, no origin: skipped
    (104, 20,   NULL, 'DEPOSITO',       80.00, '2026-09-15 13:00', 'EFETIVADA'),  -- ELSE branch: 0.50 x0.90
    (105, 20,   NULL, 'SAQUE',          50.00, '2026-09-15 14:00', 'CANCELADA'),  -- not EFETIVADA
    (106, 20,   NULL, 'TARIFA',          3.00, '2026-09-15 15:00', 'EFETIVADA'),  -- TARIFA is never charged
    (107, 11,   NULL, 'SAQUE',          20.00, '2026-09-16 09:00', 'EFETIVADA'),  -- another day
    -- E (holdout): batch of 2026-09-17, same traps on another account
    (108, 20,   NULL, 'SAQUE',         454.50, '2026-09-17 10:00', 'EFETIVADA'),  -- double rounding again
    (109, 20,   21,   'TRANSFERENCIA', 100.00, '2026-09-17 11:00', 'EFETIVADA'),  -- second fee, same account
    -- F: monthly movement of client 1 (June has none)
    (201, 20,   10,   'TRANSFERENCIA', 250.00, '2026-07-05 09:00', 'EFETIVADA'),  -- credit
    (202, 11,   20,   'TRANSFERENCIA',  75.25, '2026-07-20 09:00', 'EFETIVADA'),  -- debit
    (203, 10,   11,   'TRANSFERENCIA',  40.00, '2026-08-03 09:00', 'EFETIVADA');  -- both sides are client 1

-- Explicit ids do not advance BIGSERIAL sequences: new rows must not collide with the seed.
SELECT setval(pg_get_serial_sequence('clientes', 'id'), (SELECT max(id) FROM clientes));
SELECT setval(pg_get_serial_sequence('contas', 'id'), (SELECT max(id) FROM contas));
SELECT setval(pg_get_serial_sequence('taxas', 'id'), (SELECT max(id) FROM taxas));
SELECT setval(pg_get_serial_sequence('transacoes', 'id'), (SELECT max(id) FROM transacoes));
