-- =============================================================================
-- 演示数据源 PostgreSQL 16 (Logical WAL CDC) 初始化脚本
-- 路径: infra/databases/demo-postgres/init_demo_pg.sql
-- 用途：
--   1. 初始化 M8 PostgreSQL CDC -> Doris 独立集成验证所需的业务表
--   2. 配置 REPLICA IDENTITY FULL 与逻辑复制 Publication
-- =============================================================================

-- 确保逻辑复制角色具备 REPLICATION 权限
ALTER ROLE demo_pg_user WITH REPLICATION;

-- 创建演示库存与履约订单表
CREATE TABLE IF NOT EXISTS pg_fulfillment_orders (
    fulfillment_id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL,
    warehouse_code VARCHAR(32) NOT NULL,
    carrier_name VARCHAR(64) NOT NULL,
    fulfillment_status VARCHAR(32) NOT NULL,
    shipping_fee NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    recipient_phone VARCHAR(32) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 开启完整旧值捕获以支持 CDC UPDATE/DELETE 精确同步
ALTER TABLE pg_fulfillment_orders REPLICA IDENTITY FULL;

-- 创建逻辑复制发布 (Publication) 供 SeaTunnel Postgres-CDC 订阅
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'etl_agent_pg_cdc_pub') THEN
        CREATE PUBLICATION etl_agent_pg_cdc_pub FOR TABLE pg_fulfillment_orders;
    END IF;
END
$$;

-- 写入初始演示数据
INSERT INTO pg_fulfillment_orders (
    fulfillment_id, order_id, warehouse_code, carrier_name,
    fulfillment_status, shipping_fee, recipient_phone
) VALUES
    (90001, 50001, 'WH-SH-01', 'SF-EXPRESS', 'DISPATCHED', 12.00, '13800138001'),
    (90002, 50002, 'WH-BJ-02', 'JD-LOGISTICS', 'IN_TRANSIT', 18.50, '13900139002'),
    (90003, 50003, 'WH-GZ-01', 'YTO-EXPRESS', 'DELIVERED', 10.00, '13800138001')
ON CONFLICT (fulfillment_id) DO NOTHING;
