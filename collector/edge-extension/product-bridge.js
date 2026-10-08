/* Product-page connection recovery. Inspect readiness in the isolated world:
 * legacy content.js held unknown ping messages open indefinitely. */
const PRODUCT_BRIDGE_VERSION = '0.4.35';
const productBridgeConnections = new Map();
const PRODUCT_PAGE_COMMANDS = new Set(['COLLECTOR_PREVIEW', 'COLLECTOR_CAPTURE',
    'COLLECTOR_OZON_PREVIEW', 'COLLECTOR_OZON_CAPTURE', 'OPEN_SKU_SELECTOR', 'EXPORT_SKU_DEBUG']);

function productPageKind(value) {
    try {
        const u = new URL(value);
        if (u.protocol !== 'https:') return null;
        if (u.hostname === 'detail.1688.com' && /^\/offer\/\d{6,}\.html$/.test(u.pathname)) return '1688';
        if (/(^|\.)ozon\.ru$/i.test(u.hostname) && /^\/product\/[^/]+/.test(u.pathname)) return 'ozon';
    } catch { /* not a URL */ }
    return null;
}

async function productSourceTab(tabId) {
    let tab;
    try { tab = await chrome.tabs.get(tabId); }
    catch { throw new Error('商品标签页已关闭，请重新打开商品详情页'); }
    if (!productPageKind(tab?.url)) throw new Error('当前不是 1688 或 Ozon HTTPS 商品详情页，请回到商品页重试');
    return tab;
}

function inspectProductReceiver(expectedVersion) {
    const bridge = globalThis.__workbenchProductBridge;
    try {
        if (bridge?.version === expectedVersion && bridge.isCurrent?.()) return { ready: true };
    } catch { /* old extension context */ }
    return { ready: false, legacy: !bridge && typeof globalThis.buildReadyCapture === 'function' };
}

async function connectProductBridge(tabId) {
    await productSourceTab(tabId);
    try {
        const probe = async () => (await chrome.scripting.executeScript({
            target: { tabId, frameIds: [0] }, func: inspectProductReceiver,
            args: [PRODUCT_BRIDGE_VERSION] }))[0]?.result;
        let state = await probe();
        if (state?.ready) return;
        if (state?.legacy) throw new Error('LEGACY_RECEIVER');
        await productSourceTab(tabId);
        await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: ['content.js'] });
        state = await probe();
        if (!state?.ready) throw new Error('NOT_READY');
    } catch (error) {
        if (error.message === 'LEGACY_RECEIVER')
            throw new Error('商品页仍运行旧版采集脚本，请刷新商品页后重新打开插件');
        throw new Error('无法连接商品页采集脚本，请允许插件访问此网站，并在页面加载完成后重试');
    }
}

async function sendProductTabMessage(tabId, message) {
    if (!PRODUCT_PAGE_COMMANDS.has(message?.type)) throw new Error('不支持的商品采集操作');
    const tab = await productSourceTab(tabId);
    const kind = productPageKind(tab.url);
    if ((message.type.startsWith('COLLECTOR_OZON_') ? kind !== 'ozon' : kind !== '1688'))
        throw new Error('当前商品页类型已改变，请重新打开插件读取预览');
    if (message.type === 'OPEN_SKU_SELECTOR') {
        let captured;
        try { captured = new URL(message.capture?.source_url); } catch { /* missing source */ }
        if (captured?.hostname !== 'detail.1688.com' || captured.pathname !== new URL(tab.url).pathname)
            throw new Error('商品页面已切换，不能使用上一件商品的规格；请重新采集');
    }
    if (!productBridgeConnections.has(tabId)) {
        const pending = connectProductBridge(tabId);
        productBridgeConnections.set(tabId, pending);
        try { await pending; }
        finally { if (productBridgeConnections.get(tabId) === pending) productBridgeConnections.delete(tabId); }
    } else await productBridgeConnections.get(tabId);
    try {
        const capture = await chrome.tabs.sendMessage(tabId, message);
        if (message.type === 'COLLECTOR_CAPTURE' && capture?.is_collectable) {
            const source = new URL(capture.source_url);
            const expected = new URL(tab.url);
            if (source.origin !== expected.origin || source.pathname !== expected.pathname)
                throw new Error('SOURCE_CHANGED');
        }
        return capture;
    }
    catch { throw new Error('商品页读取中断，请等待页面加载完成后重新打开插件；若刚升级插件，请刷新商品页'); }
}
