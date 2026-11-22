-- =============================================================================
-- 演示数据源 MySQL 8.0 初始化脚本 (infra/databases/mysql/init_demo.sql)
-- 用途：
--   1. 为 demo_etl_reader 授予只读与 CDC Binlog 复制读取权限
--   2. 初始化电商订单与用户表（场景 A：MySQL -> Doris 宽表加工）
--   3. 初始化日志事件表（场景 B：异常质量分流与监督熔断）
-- =============================================================================

CREATE DATABASE IF NOT EXISTS demo_ecommerce
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

-- 授予只读与 CDC Binlog 复制最小必要权限
GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT
    ON *.* TO 'demo_etl_reader'@'%';
FLUSH PRIVILEGES;

USE demo_ecommerce;

-- 场景 A 表 1：客户主数据表（含需识别与脱敏的敏感字段 email, phone, id_card_hash）
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    email VARCHAR(128) NOT NULL,
    phone VARCHAR(32) NOT NULL,
    region_code VARCHAR(32) NOT NULL,
    vip_tier VARCHAR(16) NOT NULL DEFAULT 'STANDARD',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_users_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 场景 A 表 2：订单交易表（支持按 updated_at 增量同步与 CDC 捕获）
CREATE TABLE IF NOT EXISTS orders (
    order_id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    order_status VARCHAR(32) NOT NULL,
    currency VARCHAR(8) NOT NULL DEFAULT 'CNY',
    order_amount DECIMAL(18, 2) NOT NULL,
    discount_amount DECIMAL(18, 2) NOT NULL DEFAULT 0.00,
    pay_channel VARCHAR(32) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_orders_user_id (user_id),
    INDEX idx_orders_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 场景 B 表：原始业务日志事件表（用于质量规则分流与 >20% Reject Rate 熔断验证）
CREATE TABLE IF NOT EXISTS raw_event_logs (
    event_id VARCHAR(64) PRIMARY KEY,
    tenant_code VARCHAR(32) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    latency_ms INT NULL,
    client_ip VARCHAR(64) NULL,
    payload_json TEXT NULL,
    event_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 写入初始演示种子数据
INSERT IGNORE INTO users (user_id, username, email, phone, region_code, vip_tier) VALUES
    (1001, 'alice_chen', 'alice.chen@example.com', '13800138001', 'CN-EAST', 'GOLD'),
    (1002, 'bob_wang', 'bob.wang@example.com', '13900139002', 'CN-NORTH', 'PLATINUM'),
    (1003, 'charlie_li', 'charlie.li@example.com', '13700137003', 'CN-SOUTH', 'STANDARD');

INSERT IGNORE INTO orders (order_id, user_id, order_status, currency, order_amount, discount_amount, pay_channel) VALUES
    (50001, 1001, 'PAID', 'CNY', 299.00, 20.00, 'ALIPAY'),
    (50002, 1002, 'SHIPPED', 'CNY', 1299.50, 100.00, 'WECHAT'),
    (50003, 1001, 'COMPLETED', 'CNY', 89.90, 0.00, 'CARD'),
    (50004, 1003, 'PAID', 'CNY', 459.00, 30.00, 'ALIPAY');
