<?php
/**
 * ALTCHA 验证端点
 *
 * POST /verify.php
 *
 * 请求格式:
 * {
 *     "payload": "altcha_solution_payload",
 *     "chat_id": 123,
 *     "user_id": 456,
 *     "verify_token": "abc123"
 * }
 *
 * 成功响应:
 * {
 *     "success": true,
 *     "signature": "hmac_signature",
 *     "timestamp": 1234567890
 * }
 */

require_once __DIR__ . '/vendor/autoload.php';
require_once __DIR__ . '/config.php';

use AltchaOrg\Altcha\Algorithm\Pbkdf2;
use AltchaOrg\Altcha\Altcha;
use AltchaOrg\Altcha\HmacAlgorithm;
use AltchaOrg\Altcha\VerifySolutionOptions;

// 设置响应头
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: ' . ALLOWED_ORIGIN);
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

// 处理 OPTIONS 预检请求
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit();
}

// 只允许 POST 请求
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'error' => 'Method Not Allowed']);
    exit();
}

try {
    // 读取请求数据
    $input = json_decode(file_get_contents('php://input'), true);

    if (!$input) {
        throw new Exception('Invalid JSON input');
    }

    // 验证必需参数
    $required_fields = ['payload', 'chat_id', 'user_id', 'verify_token'];
    foreach ($required_fields as $field) {
        if (!isset($input[$field])) {
            throw new Exception("Missing required field: {$field}");
        }
    }

    // payload 必须是 base64 字符串（widget v3 恒发字符串；非字符串会让库抛 TypeError 绕过 catch (Exception)）
    if (!is_string($input['payload'])) {
        throw new Exception('Invalid payload type');
    }

    // 调试日志
    if (defined('DEBUG_MODE') && DEBUG_MODE) {
        error_log('[ALTCHA] 收到验证请求: ' . json_encode([
            'chat_id' => $input['chat_id'],
            'user_id' => $input['user_id'],
            'payload_length' => strlen($input['payload']),
        ]));
    }

    // 创建 ALTCHA 实例并验证解答（PoW v2：先验挑战签名防伪造，再重派生密钥核对解答；
    // 垃圾 payload 会抛 InvalidArgumentException，由下方 catch (Exception) 统一返回 400）
    $altcha = new Altcha(hmacSignatureSecret: ALTCHA_HMAC_KEY);
    $pbkdf2 = new Pbkdf2(HmacAlgorithm::SHA256);
    $result = $altcha->verifySolution(new VerifySolutionOptions(
        payload: $input['payload'],
        algorithm: $pbkdf2,
    ));

    if (!$result->verified || $result->expired) {
        echo json_encode([
            'success' => false,
            'error' => 'Invalid solution or challenge expired',
        ]);
        exit();
    }

    // 验证成功，生成 HMAC 签名
    $timestamp = time();
    $message = "{$input['chat_id']}:{$input['user_id']}:{$input['verify_token']}:{$timestamp}";
    $signature = hash_hmac('sha256', $message, BOT_SIGNATURE_KEY);

    // 返回签名
    echo json_encode([
        'success' => true,
        'signature' => $signature,
        'timestamp' => $timestamp,
    ]);

    // 调试日志
    if (defined('DEBUG_MODE') && DEBUG_MODE) {
        error_log('[ALTCHA] 验证成功，已生成签名');
    }

} catch (Exception $e) {
    http_response_code(400);
    echo json_encode([
        'success' => false,
        'error' => $e->getMessage(),
    ]);

    if (defined('DEBUG_MODE') && DEBUG_MODE) {
        error_log('[ALTCHA] 验证失败: ' . $e->getMessage());
    }
}
