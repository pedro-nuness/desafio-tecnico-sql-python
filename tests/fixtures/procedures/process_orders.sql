-- Hybrid case: row-by-row loop with locking, per-row UPDATE (N+1), diagnostics,
-- OUT/INOUT parameters, exception handling, JSONB and a call to another routine.
CREATE OR REPLACE FUNCTION billing.process_customer_orders(
    p_customer_id integer,
    OUT total_amount numeric,
    INOUT processed_count integer
)
RETURNS record
LANGUAGE plpgsql
AS $$
DECLARE
    r_order record;
    v_sum numeric := 0;
    v_rows integer;
BEGIN
    processed_count := 0;
    FOR r_order IN
        SELECT o.id, o.amount
          FROM orders o
         WHERE o.customer_id = p_customer_id
           AND o.status = 'pending'
         FOR UPDATE
    LOOP
        UPDATE order_items SET processed = true WHERE order_id = r_order.id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_sum := v_sum + r_order.amount;
        processed_count := processed_count + 1;
    END LOOP;

    IF v_sum < 0 THEN
        RAISE EXCEPTION 'Negative total % for customer %', v_sum, p_customer_id;
    END IF;

    PERFORM audit_log('orders_processed', jsonb_build_object('customer', p_customer_id));
    total_amount := v_sum;
EXCEPTION
    WHEN others THEN
        RAISE NOTICE 'processing failed for %', p_customer_id;
        RAISE;
END;
$$;
