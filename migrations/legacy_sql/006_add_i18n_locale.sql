-- Migration: 添加 i18n 多语言支持
-- Date: 2026-07-26
-- Description: 为群组和私聊用户添加语言偏好字段，支持简体中文/繁体中文（台湾·香港）/英文，为后续多语言文案奠定基础

-- 群组消息语言字段
ALTER TABLE groups ADD COLUMN IF NOT EXISTS locale VARCHAR(16) NOT NULL DEFAULT 'zh-Hans';
COMMENT ON COLUMN groups.locale IS '群组消息语言（BCP 47，如 zh-Hans/zh-Hant/en）';

-- 用户私聊设置表（稀疏写入：无记录即默认语言）
CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT PRIMARY KEY,
    locale VARCHAR(16) NOT NULL DEFAULT 'zh-Hans',
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE user_settings IS '用户私聊设置，稀疏写入（无记录即使用默认语言）';
COMMENT ON COLUMN user_settings.user_id IS 'Telegram 用户 ID';
COMMENT ON COLUMN user_settings.locale IS '用户私聊语言偏好（BCP 47）';
COMMENT ON COLUMN user_settings.updated_at IS '最后更新时间';
