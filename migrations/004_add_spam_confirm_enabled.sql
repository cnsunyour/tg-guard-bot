-- Migration: 添加垃圾消息管理员确认开关
-- Date: 2026-02-19
-- Description: 在 groups 表添加 spam_confirm_enabled 字段，支持管理员确认模式（检测到垃圾后等待管理员确认再处罚）

-- 添加 spam_confirm_enabled 字段（如果不存在）
ALTER TABLE groups ADD COLUMN IF NOT EXISTS spam_confirm_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- 添加字段注释
COMMENT ON COLUMN groups.spam_confirm_enabled IS '是否启用管理员确认模式（检测到垃圾后等待管理员确认再处罚）';
