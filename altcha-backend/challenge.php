<?php
/**
 * ALTCHA 挑战生成端点
 *
 * GET /challenge.php
 *
 * 返回一个 Proof-of-Work 挑战供前端解答
 */

require_once __DIR__ . '/vendor/autoload.php';
require_once __DIR__ . '/config.php';

use AltchaOrg\Altcha\Algorithm\Pbkdf2;
use AltchaOrg\Altcha\Altcha;
use AltchaOrg\Altcha\CreateChallengeOptions;
use AltchaOrg\Altcha\HmacAlgorithm;

// 设置响应头
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: ' . ALLOWED_ORIGIN);
header('Access-Control-Allow-Methods: GET, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

// 处理 OPTIONS 预检请求
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit();
}

// 只允许 GET 请求
if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    http_response_code(405);
    echo json_encode(['success' => false, 'error' => 'Method Not Allowed']);
    exit();
}

try {
    // 挑战成本：兼容未更新 config.php 的存量部署（缺省 5000，与 config.php.example 一致）
    $powCost = defined('POW_COST') ? POW_COST : 5000;

    // 创建 ALTCHA 实例（PoW v2，PBKDF2 概率模式：客户端解题贵、服务端一次重派生即验证）
    $altcha = new Altcha(hmacSignatureSecret: ALTCHA_HMAC_KEY);
    $pbkdf2 = new Pbkdf2(HmacAlgorithm::SHA256);

    // 创建挑战选项（expiresAt 进入签名覆盖范围，客户端不可篡改）
    $options = new CreateChallengeOptions(
        algorithm: $pbkdf2,
        cost: $powCost,
        expiresAt: time() + POW_EXPIRES
    );

    // 生成挑战
    $challenge = $altcha->createChallenge($options);

    // 返回挑战（toJson 输出 {"parameters": {...}, "signature": "..."}，widget v3 直接消费）
    echo $challenge->toJson();

    // 调试日志
    if (defined('DEBUG_MODE') && DEBUG_MODE) {
        error_log('[ALTCHA] 生成挑战: ' . $challenge->toJson());
    }

} catch (Exception $e) {
    http_response_code(500);
    echo json_encode([
        'success' => false,
        'error' => 'Failed to generate challenge: ' . $e->getMessage(),
    ]);

    if (defined('DEBUG_MODE') && DEBUG_MODE) {
        error_log('[ALTCHA] 生成挑战失败: ' . $e->getMessage());
    }
}
