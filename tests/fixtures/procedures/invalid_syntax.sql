CREATE FUNCTION broken() RETURNS integer LANGUAGE plpgsql AS $$
BEGIN
    RETURN 1
$$;
