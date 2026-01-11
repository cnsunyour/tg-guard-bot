/**
 * CAPTCHA 验证 API
 *
 * 验证各 provider 的 CAPTCHA 响应，生成 HMAC 签名
 * 支持: turnstile, friendly, hcaptcha, mtcaptcha
 * 注意: ALTCHA 由独立 PHP 后端处理
 */

export async function onRequestPost(context) {
    const { request, env } = context;

    try {
        const data = await request.json();
        const {
            provider,
            captcha_response,
            chat_id,
            user_id,
            verify_token,
            key_index = 0,
        } = data;

        // 参数验证
        if (!provider || !captcha_response || !chat_id || !user_id || !verify_token) {
            return Response.json({ success: false, error: 'Missing required parameters' });
        }

        // 验证 CAPTCHA
        let verified = false;

        switch (provider) {
            case 'turnstile':
                verified = await verifyTurnstile(captcha_response, env.TURNSTILE_SECRET_KEY);
                break;

            case 'friendly':
                verified = await verifyFriendly(captcha_response, key_index, env.FRIENDLY_KEYS);
                break;

            case 'hcaptcha':
                verified = await verifyHCaptcha(captcha_response, env.HCAPTCHA_SECRET_KEY);
                break;

            case 'mtcaptcha':
                verified = await verifyMTCaptcha(captcha_response, env.MTCAPTCHA_PRIVATE_KEY);
                break;

            default:
                return Response.json({ success: false, error: 'Unknown or unsupported provider: ' + provider });
        }

        if (!verified) {
            return Response.json({ success: false, error: 'Verification failed' });
        }

        // 生成 HMAC 签名
        const timestamp = Math.floor(Date.now() / 1000);
        const message = `${chat_id}:${user_id}:${verify_token}:${timestamp}`;
        const signature = await generateHMAC(message, env.SIGNATURE_KEY);

        return Response.json({
            success: true,
            signature,
            timestamp,
        });
    } catch (error) {
        console.error('Verify API error:', error);
        return Response.json({ success: false, error: error.message });
    }
}

/**
 * 验证 Cloudflare Turnstile
 */
async function verifyTurnstile(token, secretKey) {
    try {
        const response = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({ secret: secretKey, response: token }),
        });

        const result = await response.json();
        return result.success === true;
    } catch (error) {
        console.error('Turnstile verification error:', error);
        return false;
    }
}

/**
 * 验证 Friendly Captcha
 */
async function verifyFriendly(solution, keyIndex, friendlyKeysJson) {
    try {
        const friendlyKeys = JSON.parse(friendlyKeysJson);
        const actualIndex = parseInt(keyIndex, 10) % friendlyKeys.length;
        const keyPair = friendlyKeys[actualIndex];

        if (!keyPair.apikey || !keyPair.sitekey) {
            throw new Error('Invalid Friendly Captcha key configuration');
        }

        const response = await fetch('https://api.friendlycaptcha.com/api/v1/siteverify', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-API-Key': keyPair.apikey,
            },
            body: JSON.stringify({
                solution: solution,
                sitekey: keyPair.sitekey,
            }),
        });

        const result = await response.json();
        return result.success === true;
    } catch (error) {
        console.error('Friendly Captcha verification error:', error);
        return false;
    }
}

/**
 * 验证 hCaptcha
 */
async function verifyHCaptcha(token, secretKey) {
    try {
        const response = await fetch('https://api.hcaptcha.com/siteverify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({ secret: secretKey, response: token }),
        });

        const result = await response.json();
        return result.success === true;
    } catch (error) {
        console.error('hCaptcha verification error:', error);
        return false;
    }
}

/**
 * 验证 MTCaptcha
 */
async function verifyMTCaptcha(token, privateKey) {
    try {
        const url = `https://service.mtcaptcha.com/mtcv1/api/checktoken?privatekey=${encodeURIComponent(privateKey)}&token=${encodeURIComponent(token)}`;
        const response = await fetch(url, { method: 'GET' });

        const result = await response.json();
        return result.success === true;
    } catch (error) {
        console.error('MTCaptcha verification error:', error);
        return false;
    }
}

/**
 * 生成 HMAC-SHA256 签名
 */
async function generateHMAC(message, secretKey) {
    const encoder = new TextEncoder();
    const keyData = encoder.encode(secretKey);
    const messageData = encoder.encode(message);

    const cryptoKey = await crypto.subtle.importKey(
        'raw',
        keyData,
        { name: 'HMAC', hash: 'SHA-256' },
        false,
        ['sign']
    );

    const signature = await crypto.subtle.sign('HMAC', cryptoKey, messageData);
    const hashArray = Array.from(new Uint8Array(signature));
    return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
}
