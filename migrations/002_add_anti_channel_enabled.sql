-- Migration: 添加反频道马甲功能字段
-- Date: 2026-01-08
-- Description: 在 groups 表添加 anti_channel_enabled 字段，禁止用户以频道身份发言

-- 添加 anti_channel_enabled 字段（如果不存在）
ALTER TABLE groups ADD COLUMN IF NOT EXISTS anti_channel_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- 添加字段注释
COMMENT ON COLUMN groups.anti_channel_enabled IS '是否启用反频道马甲(禁止用户以频道身份发言)';
