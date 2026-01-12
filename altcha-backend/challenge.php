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

use AltchaOrg\Altcha\Altcha;
use AltchaOrg\Altcha\ChallengeOptions;
use AltchaOrg\Altcha\Algorithm;

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
    // 创建 ALTCHA 实例
    $altcha = new Altcha(ALTCHA_HMAC_KEY);

    // 创建挑战选项
    $options = new ChallengeOptions(
        algorithm: Algorithm::SHA256,
        maxNumber: POW_MAX_NUMBER,
        expires: (new \DateTimeImmutable())->setTimestamp(time() + POW_EXPIRES)
    );

    // 生成挑战
    $challenge = $altcha->createChallenge($options);

    // 返回挑战
    echo json_encode($challenge);

    // 调试日志
    if (defined('DEBUG_MODE') && DEBUG_MODE) {
        error_log('[ALTCHA] 生成挑战: ' . json_encode($challenge));
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
