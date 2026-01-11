/**
 * Cloudflare Pages Functions - 配置 API
 *
 * 功能：返回前端所需的公开配置（如 Turnstile Site Key）
 */

export async function onRequestGet(context) {
    const { env } = context;

    try {
        // Site Key 是公开的，可以安全地返回给前端
        return Response.json({
            success: true,
            turnstile_site_key: env.TURNSTILE_SITE_KEY || '',
        }, {
            headers: {
                'Content-Type': 'application/json',
                'Cache-Control': 'public, max-age=3600', // 缓存 1 小时
            }
        });
    } catch (e) {
        console.error('获取配置失败:', e);
        return Response.json(
            { success: false, error: '服务器错误' },
            { status: 500 }
        );
    }
}
