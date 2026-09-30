-- Pure computation: no SQL at all, only control flow.
CREATE FUNCTION calculate_discount(p_amount numeric, p_tier text DEFAULT 'standard')
RETURNS numeric
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    v_rate numeric := 0;
BEGIN
    IF p_amount IS NULL OR p_amount < 0 THEN
        RAISE EXCEPTION 'invalid amount: %', p_amount USING ERRCODE = '22023';
    END IF;

    CASE p_tier
        WHEN 'gold' THEN v_rate := 0.15;
        WHEN 'silver' THEN v_rate := 0.10;
        ELSE v_rate := 0.0;
    END CASE;

    IF p_amount > 1000 THEN
        v_rate := v_rate + 0.05;
    END IF;

    RETURN round(p_amount * v_rate, 2);
END;
$$;
