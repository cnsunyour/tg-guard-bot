/**
 * CAPTCHA 配置 API
 *
 * 根据 provider 和 key_index 参数返回对应的 CAPTCHA 配置
 * 支持: turnstile, friendly, hcaptcha, mtcaptcha, altcha
 */

export async function onRequestGet(context) {
    const { request, env } = context;
    const url = new URL(request.url);
    const provider = url.searchParams.get('provider') || 'turnstile';
    const keyIndex = parseInt(url.searchParams.get('key_index') || '0', 10);

    try {
        let config = { success: true };

        switch (provider) {
            case 'turnstile':
                if (!env.TURNSTILE_SITE_KEY) {
                    return Response.json({ success: false, error: 'Turnstile not configured' });
                }
                config.site_key = env.TURNSTILE_SITE_KEY;
                break;

            case 'friendly':
                if (!env.FRIENDLY_KEYS) {
                    return Response.json({ success: false, error: 'Friendly Captcha not configured' });
                }

                try {
                    const friendlyKeys = JSON.parse(env.FRIENDLY_KEYS);
                    if (!Array.isArray(friendlyKeys) || friendlyKeys.length === 0) {
                        return Response.json({ success: false, error: 'Invalid FRIENDLY_KEYS configuration' });
                    }

                    // 使用 keyIndex 获取对应的 key pair
                    const actualIndex = keyIndex % friendlyKeys.length;
                    const keyPair = friendlyKeys[actualIndex];

                    if (!keyPair.sitekey) {
                        return Response.json({ success: false, error: 'Missing sitekey in FRIENDLY_KEYS' });
                    }

                    config.site_key = keyPair.sitekey;
                } catch (e) {
                    return Response.json({ success: false, error: 'Failed to parse FRIENDLY_KEYS: ' + e.message });
                }
                break;

            case 'hcaptcha':
                if (!env.HCAPTCHA_SITE_KEY) {
                    return Response.json({ success: false, error: 'hCaptcha not configured' });
                }
                config.site_key = env.HCAPTCHA_SITE_KEY;
                break;

            case 'mtcaptcha':
                if (!env.MTCAPTCHA_SITE_KEY) {
                    return Response.json({ success: false, error: 'MTCaptcha not configured' });
                }
                config.site_key = env.MTCAPTCHA_SITE_KEY;
                break;

            case 'altcha':
                if (!env.ALTCHA_API_URL) {
                    return Response.json({ success: false, error: 'ALTCHA not configured' });
                }
                config.challenge_url = env.ALTCHA_API_URL + '/challenge.php';
                config.verify_url = env.ALTCHA_API_URL + '/verify.php';
                break;

            default:
                return Response.json({ success: false, error: 'Unknown provider: ' + provider });
        }

        return Response.json(config);
    } catch (error) {
        console.error('Config API error:', error);
        return Response.json({ success: false, error: error.message });
    }
}
