/* Shared by the popup and worker. Recover a missing receiver, never replay a
 * page-changing command: a lost response must not cause an extra page click. */
const SEERFAR_BRIDGE_VERSION = '0.4.31';
const seerfarBridgeConnections = new Map();

function seerfarReceiverMissing(error) {
    return /Receiving end does not exist/i.test(String(error?.message || error || ''));
}

async function seerfarReportTab(tabId) {
    let tab;
    try { tab = await chrome.tabs.get(tabId); }
    catch { throw new Error('采集标签页已关闭，请重新打开 Seerfar 报表'); }
    if (!/^https:\/\/(?:www\.)?seerfar\.cn\/admin\//.test(tab?.url || ''))
        throw new Error('当前标签页不是 Seerfar HTTPS 报表，请回到报表页再采集');
    if (/\/admin\/login(?:[/?#]|$)/.test(tab.url))
        throw new Error('Seerfar 登录已失效，请在采集窗口重新登录');
    return tab;
}

async function connectSeerfarBridge(tabId) {
    await seerfarReportTab(tabId);
    let ready;
    try { ready = await chrome.tabs.sendMessage(tabId, { type: 'SEERFAR_MARKET_PING' }); }
    catch (error) {
        if (!seerfarReceiverMissing(error))
            throw new Error('Seerfar 页面连接已中断，请等待加载完成后重试；若仍失败，请刷新报表页');
        // The manifest may not have injected into an already-open page after
        // extension reload. Recheck the destination before loading our file.
        await seerfarReportTab(tabId);
        try {
            await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] },
                files: ['seerfar-content.js'] });
            ready = await chrome.tabs.sendMessage(tabId, { type: 'SEERFAR_MARKET_PING' });
        } catch {
            throw new Error('无法加载 Seerfar 采集脚本，请在扩展设置允许访问此网站，再刷新报表页重试');
        }
    }
    // An older, still-live listener must be refreshed, not overlaid with a
    // second listener that could handle NEXT_PAGE twice.
    if (ready?.ready !== true || ready.version !== SEERFAR_BRIDGE_VERSION)
        throw new Error('Seerfar 页面仍使用旧版采集脚本，请刷新报表页后重新采集');
}

async function ensureSeerfarBridge(tabId) {
    if (!seerfarBridgeConnections.has(tabId)) {
        const pending = connectSeerfarBridge(tabId);
        seerfarBridgeConnections.set(tabId, pending);
        try { await pending; }
        finally { if (seerfarBridgeConnections.get(tabId) === pending) seerfarBridgeConnections.delete(tabId); }
    } else await seerfarBridgeConnections.get(tabId);
}

async function sendSeerfarTabMessage(tabId, message) {
    await ensureSeerfarBridge(tabId);
    try { return await chrome.tabs.sendMessage(tabId, message); }
    catch {
        throw new Error('Seerfar 页面在采集过程中断开；已停止以免重复翻页。请等待加载完成或刷新页面后继续');
    }
}
