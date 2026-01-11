/**
 * Cloudflare Pages Functions - Turnstile 验证 API
 *
 * 功能：
 * 1. 验证 Cloudflare Turnstile Token
 * 2. 生成 HMAC-SHA256 签名
 * 3. 返回签名数据给前端
 */

export async function onRequestPost(context) {
    const { request, env } = context;

    try {
        // 解析请求体
        const body = await request.json();
        const { chat_id, user_id, verify_token, cf_token } = body;

        // 参数验证
        if (!chat_id || !user_id || !verify_token || !cf_token) {
            return Response.json(
                { success: false, error: '缺少必要参数' },
                { status: 400 }
            );
        }

        // 1. 验证 Turnstile Token
        const turnstileResponse = await fetch(
            'https://challenges.cloudflare.com/turnstile/v0/siteverify',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({
                    secret: env.TURNSTILE_SECRET_KEY,
                    response: cf_token,
                }),
            }
        );

        const turnstileResult = await turnstileResponse.json();

        if (!turnstileResult.success) {
            return Response.json(
                {
                    success: false,
                    error: '人机验证失败',
                    details: turnstileResult['error-codes']
                },
                { status: 400 }
            );
        }

        // 2. 生成签名（HMAC-SHA256）
        const timestamp = Math.floor(Date.now() / 1000);
        const message = `${chat_id}:${user_id}:${verify_token}:${timestamp}`;

        const encoder = new TextEncoder();
        const key = await crypto.subtle.importKey(
            'raw',
            encoder.encode(env.SIGNATURE_KEY),
            { name: 'HMAC', hash: 'SHA-256' },
            false,
            ['sign']
        );

        const signatureBuffer = await crypto.subtle.sign(
            'HMAC',
            key,
            encoder.encode(message)
        );

        const signature = Array.from(new Uint8Array(signatureBuffer))
            .map(b => b.toString(16).padStart(2, '0'))
            .join('');

        // 3. 返回签名数据
        return Response.json({
            success: true,
            signature: signature,
            timestamp: timestamp,
        });

    } catch (e) {
        console.error('验证失败:', e);
        return Response.json(
            { success: false, error: '服务器错误' },
            { status: 500 }
        );
    }
}
