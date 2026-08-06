/**
 * WebApp 验证页 i18n：zh-Hans（默认）/ zh-Hant / en。
 *
 * locale 由 bot 在 WebApp URL 注入（?locale=...），取自用户私聊 locale
 * （LocaleResolver.for_private_from_group），与 bot 出站消息 locale 体系一致；
 * 缺失或非法值回退 zh-Hans。locale 仅影响展示，不参与签名。
 *
 * 用法：
 *   <script src="/i18n.js"></script>
 *   <h1 data-i18n="heading">入群验证</h1>          // 静态文案：applyTranslations() 填充
 *   <title data-i18n="title.turnstile">Turnstile 验证</title>
 *   showStatus('loading', t('status.loading_widget'))  // JS 动态文案
 *
 * 各 HTML 需在引入本脚本后、执行业务逻辑前调用 applyTranslations()。
 */

const MESSAGES = {
    "zh-Hans": {
        "title.turnstile": "Turnstile 验证",
        "title.friendly": "Friendly Captcha 验证",
        "title.hcaptcha": "hCaptcha 验证",
        "title.mtcaptcha": "MTCaptcha 验证",
        "title.altcha": "ALTCHA 验证",
        "heading": "🔐 入群验证",
        "subtitle": "请完成以下验证",
        "status.loading_widget": "正在加载验证组件...",
        "status.verifying": "正在验证...",
        "status.success": "✅ 验证成功！",
        "status.failed": "❌ 验证失败，请重试",
        "status.failed_refresh": "❌ 验证失败，请刷新后重试",
        "status.network_error": "❌ 网络错误，请重试",
        "status.widget_load_failed": "❌ 验证组件加载失败",
        "status.init_failed": "❌ 加载失败，请刷新后重试",
        "index.title": "Telegram Guard Bot - 验证服务",
        "index.heading": "🔐 Telegram Guard Bot",
        "index.subtitle": "多验证码集成服务",
        "index.supported_methods": "支持的验证方式",
        "index.methods.turnstile": "✅ Cloudflare Turnstile",
        "index.methods.friendly": "✅ Friendly Captcha v2",
        "index.methods.hcaptcha": "✅ hCaptcha",
        "index.methods.mtcaptcha": "✅ MTCaptcha",
        "index.methods.altcha": "✅ ALTCHA（工作量证明）",
        "index.architecture": "技术架构",
        "index.arch.pages": "🚀 Cloudflare Pages（前端托管）",
        "index.arch.functions": "⚡ Cloudflare Functions（验证 API）",
        "index.arch.hmac": "🔒 HMAC-SHA256 签名验证",
        "index.arch.isolated": "🌐 独立页面设计（避免 SDK 冲突）",
        "index.footer": "此页面仅供系统使用，请通过 Telegram Bot 访问验证服务",
    },
    "zh-Hant": {
        "title.turnstile": "Turnstile 驗證",
        "title.friendly": "Friendly Captcha 驗證",
        "title.hcaptcha": "hCaptcha 驗證",
        "title.mtcaptcha": "MTCaptcha 驗證",
        "title.altcha": "ALTCHA 驗證",
        "heading": "🔐 入群驗證",
        "subtitle": "請完成以下驗證",
        "status.loading_widget": "正在載入驗證元件...",
        "status.verifying": "正在驗證...",
        "status.success": "✅ 驗證成功！",
        "status.failed": "❌ 驗證失敗，請再試一次",
        "status.failed_refresh": "❌ 驗證失敗，請重新整理後再試一次",
        "status.network_error": "❌ 網路錯誤，請再試一次",
        "status.widget_load_failed": "❌ 驗證元件載入失敗",
        "status.init_failed": "❌ 載入失敗，請重新整理後再試一次",
        "index.title": "Telegram Guard Bot - 驗證服務",
        "index.heading": "🔐 Telegram Guard Bot",
        "index.subtitle": "多重驗證碼整合服務",
        "index.supported_methods": "支援的驗證方式",
        "index.methods.turnstile": "✅ Cloudflare Turnstile",
        "index.methods.friendly": "✅ Friendly Captcha v2",
        "index.methods.hcaptcha": "✅ hCaptcha",
        "index.methods.mtcaptcha": "✅ MTCaptcha",
        "index.methods.altcha": "✅ ALTCHA（工作量證明）",
        "index.architecture": "技術架構",
        "index.arch.pages": "🚀 Cloudflare Pages（前端託管）",
        "index.arch.functions": "⚡ Cloudflare Functions（驗證 API）",
        "index.arch.hmac": "🔒 HMAC-SHA256 簽章驗證",
        "index.arch.isolated": "🌐 獨立頁面設計（避免 SDK 衝突）",
        "index.footer": "本頁面僅供系統使用，請透過 Telegram Bot 使用驗證服務",
    },
    "en": {
        "title.turnstile": "Turnstile Verification",
        "title.friendly": "Friendly Captcha Verification",
        "title.hcaptcha": "hCaptcha Verification",
        "title.mtcaptcha": "MTCaptcha Verification",
        "title.altcha": "ALTCHA Verification",
        "heading": "🔐 Group verification",
        "subtitle": "Complete the verification below",
        "status.loading_widget": "Loading the verification widget...",
        "status.verifying": "Verifying...",
        "status.success": "✅ Verification successful!",
        "status.failed": "❌ Verification failed. Please try again.",
        "status.failed_refresh": "❌ Verification failed. Refresh the page and try again.",
        "status.network_error": "❌ Network error. Please try again.",
        "status.widget_load_failed": "❌ Failed to load the verification widget.",
        "status.init_failed": "❌ Failed to load. Refresh the page and try again.",
        "index.title": "Telegram Guard Bot - Verification Service",
        "index.heading": "🔐 Telegram Guard Bot",
        "index.subtitle": "Multi-provider CAPTCHA service",
        "index.supported_methods": "Supported verification methods",
        "index.methods.turnstile": "✅ Cloudflare Turnstile",
        "index.methods.friendly": "✅ Friendly Captcha v2",
        "index.methods.hcaptcha": "✅ hCaptcha",
        "index.methods.mtcaptcha": "✅ MTCaptcha",
        "index.methods.altcha": "✅ ALTCHA (Proof of Work)",
        "index.architecture": "Architecture",
        "index.arch.pages": "🚀 Cloudflare Pages (frontend hosting)",
        "index.arch.functions": "⚡ Cloudflare Functions (verification API)",
        "index.arch.hmac": "🔒 HMAC-SHA256 signature verification",
        "index.arch.isolated": "🌐 Isolated pages to prevent SDK conflicts",
        "index.footer": "This page is for system use only. Open the verification service through the Telegram bot.",
    },
};

const DEFAULT_LOCALE = "zh-Hans";

/**
 * 从 URL ?locale= 读取当前 locale；非法/缺失回退默认。
 *
 * 暴露为全局供各页面在需要时直接读取（如 Friendly Captcha widget 的 lang 属性映射）。
 */
function getCurrentLocale() {
    const locale = new URLSearchParams(window.location.search).get("locale");
    return Object.prototype.hasOwnProperty.call(MESSAGES, locale) ? locale : DEFAULT_LOCALE;
}

/**
 * 按 key 取当前 locale 的文案；缺失时回退默认 locale，仍缺失返回 key 本身（便于发现遗漏）。
 *
 * vars 中的 {name} 占位会被替换；占位在 vars 中缺失时保留原占位（不静默吞）。
 */
function t(key, vars = {}) {
    const locale = getCurrentLocale();
    const template = MESSAGES[locale][key] ?? MESSAGES[DEFAULT_LOCALE][key] ?? key;
    return template.replace(/\{([^{}]+)\}/g, (placeholder, name) =>
        Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : placeholder
    );
}

/**
 * 遍历 [data-i18n] / [data-i18n-title] 元素填充文案，并设置 <html lang>。
 *
 * <html lang> 初始值（zh-Hans）仅作 no-JS 兜底；JS 执行后覆盖为当前 locale，提升无障碍准确性。
 */
function applyTranslations(root = document) {
    document.documentElement.lang = getCurrentLocale();
    root.querySelectorAll("[data-i18n]").forEach((element) => {
        element.textContent = t(element.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-title]").forEach((element) => {
        element.title = t(element.dataset.i18nTitle);
    });
}
