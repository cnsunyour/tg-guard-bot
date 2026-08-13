-- Migration: 添加群组活跃度系统开关字段
-- Date: 2026-01-07
-- Description: 在 groups 表添加 activity_enabled 字段，支持群组级别控制活跃度系统

-- 添加 activity_enabled 字段（如果不存在）
ALTER TABLE groups ADD COLUMN IF NOT EXISTS activity_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- 添加字段注释
COMMENT ON COLUMN groups.activity_enabled IS '是否启用活跃度系统';
