-- Migration: 添加活跃度跳过垃圾检测阈值字段
-- Date: 2026-01-10
-- Description: 在 groups 表添加 activity_skip_threshold 字段，支持按群组配置活跃度跳过垃圾检测的阈值

-- 添加 activity_skip_threshold 字段（如果不存在）
ALTER TABLE groups ADD COLUMN IF NOT EXISTS activity_skip_threshold INTEGER NOT NULL DEFAULT 0;

-- 添加字段注释
COMMENT ON COLUMN groups.activity_skip_threshold IS '活跃度跳过垃圾检测阈值（0=禁用，>0=启用并使用此阈值）';
