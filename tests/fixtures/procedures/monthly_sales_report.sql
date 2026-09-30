-- Set-based case: aggregation, JOIN, CTE and JSONB. Nothing procedural.
CREATE FUNCTION monthly_sales_report(p_year integer)
RETURNS TABLE(month date, category text, revenue numeric, top_products jsonb)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    WITH sales AS (
        SELECT date_trunc('month', o.created_at)::date AS month,
               c.name AS category,
               oi.product_id,
               sum(oi.quantity * oi.unit_price) AS revenue
          FROM orders o
          JOIN order_items oi ON oi.order_id = o.id
          JOIN categories c ON c.id = oi.category_id
         WHERE extract(year FROM o.created_at) = p_year
         GROUP BY 1, 2, 3
    )
    SELECT s.month, s.category, sum(s.revenue),
           jsonb_agg(jsonb_build_object('product', s.product_id, 'revenue', s.revenue))
      FROM sales s
     GROUP BY s.month, s.category;
END;
$$;
