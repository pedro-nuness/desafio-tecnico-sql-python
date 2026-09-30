-- Procedure with transaction control, a cursor, dynamic SQL and a recursive CTE.
CREATE PROCEDURE archive_orders(IN p_days integer, IN p_category_id integer)
LANGUAGE plpgsql
AS $$
DECLARE
    c_old CURSOR FOR
        SELECT id FROM orders WHERE created_at < now() - make_interval(days => p_days);
    v_id integer;
    v_partition text := 'orders_archive_' || to_char(now(), 'YYYY');
BEGIN
    OPEN c_old;
    LOOP
        FETCH c_old INTO v_id;
        EXIT WHEN NOT FOUND;
        EXECUTE format('INSERT INTO %I SELECT * FROM orders WHERE id = $1', v_partition)
            USING v_id;
        DELETE FROM orders WHERE id = v_id;
    END LOOP;
    CLOSE c_old;

    WITH RECURSIVE tree AS (
        SELECT id FROM categories WHERE id = p_category_id
        UNION ALL
        SELECT c.id FROM categories c JOIN tree t ON c.parent_id = t.id
    )
    UPDATE products SET archived = true WHERE category_id IN (SELECT id FROM tree);

    COMMIT;
END;
$$;
