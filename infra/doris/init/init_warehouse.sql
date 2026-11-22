-- =============================================================================
-- Apache Doris 2.1.7 目标数仓初始化脚本 (infra/doris/init/init_warehouse.sql)
-- 用途：创建默认目标数据库 etl_warehouse 及影子表/正式表发布测试库
-- =============================================================================

CREATE DATABASE IF NOT EXISTS etl_warehouse;
USE etl_warehouse;

-- 场景 A 目标宽表示例：dwd_order_user_wide（支持 Unique Key 模型与原子 REPLACE WITH TABLE 交换）
CREATE TABLE IF NOT EXISTS dwd_order_user_wide (
    order_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username VARCHAR(64),
    masked_email VARCHAR(128),
    masked_phone VARCHAR(32),
    region_code VARCHAR(32),
    vip_tier VARCHAR(16),
    order_status VARCHAR(32),
    currency VARCHAR(8),
    order_amount DECIMAL(18, 2),
    discount_amount DECIMAL(18, 2),
    net_amount DECIMAL(18, 2),
    pay_channel VARCHAR(32),
    order_updated_at DATETIME
)
UNIQUE KEY(order_id)
DISTRIBUTED BY HASH(order_id) BUCKETS 4
PROPERTIES (
    "replication_num" = "1",
    "enable_unique_key_merge_on_write" = "true"
);
