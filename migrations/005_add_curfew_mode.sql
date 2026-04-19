-- Migration: 添加宵禁模式功能
-- Date: 2026-04-19
-- Description: 在 groups 表添加宵禁模式相关字段，支持基于时间和活跃度的消息限制

-- 添加宵禁模式字段（如果不存在）
ALTER TABLE groups ADD COLUMN IF NOT EXISTS curfew_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS curfew_start_hour INTEGER;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS curfew_start_minute INTEGER NOT NULL DEFAULT 0;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS curfew_end_hour INTEGER;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS curfew_end_minute INTEGER NOT NULL DEFAULT 0;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS curfew_timezone_offset INTEGER NOT NULL DEFAULT 8;

-- 添加字段注释
COMMENT ON COLUMN groups.curfew_enabled IS '是否启用宵禁模式';
COMMENT ON COLUMN groups.curfew_start_hour IS '宵禁开始小时 (0-23)';
COMMENT ON COLUMN groups.curfew_start_minute IS '宵禁开始分钟 (0-59)';
COMMENT ON COLUMN groups.curfew_end_hour IS '宵禁结束小时 (0-23)';
COMMENT ON COLUMN groups.curfew_end_minute IS '宵禁结束分钟 (0-59)';
COMMENT ON COLUMN groups.curfew_timezone_offset IS '时区偏移（相对UTC小时数）';
