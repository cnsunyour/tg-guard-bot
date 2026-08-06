-- Migration: 语言代码 zh-CN → zh-Hans
-- Date: 2026-07-26
-- Description: 简体语言码从地区码 zh-CN 迁移到脚本码 zh-Hans（与 zh-Hant 对称），更新已有数据与列默认值

-- 群组语言数据 + 列默认
UPDATE groups SET locale = 'zh-Hans' WHERE locale = 'zh-CN';
ALTER TABLE groups ALTER COLUMN locale SET DEFAULT 'zh-Hans';

-- 用户设置语言数据 + 列默认
UPDATE user_settings SET locale = 'zh-Hans' WHERE locale = 'zh-CN';
ALTER TABLE user_settings ALTER COLUMN locale SET DEFAULT 'zh-Hans';
