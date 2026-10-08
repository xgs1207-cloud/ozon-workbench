'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/listing-flow.js'), 'utf8');
const start = source.indexOf("        if(action==='submit'){");
const body = source.slice(start, source.indexOf("        if(action==='verify')", start));
function harness(options = {}) {
    const writes = [], confirmations = [], rendered = [], refreshed = [], notices = [];
    const target = {warehouse: {name: '轻小仓', warehouse_id: '22142605386000'},
        items: [{offer_id: 'xzj.jp.10.8.1', stock: 100}]};
    const context = vm.createContext({action: 'submit', id: 'P000001',
        state: {product: 'P000001'}, productDraft: () => options.draft || {},
        listingDraft: () => ({}), selectedReadStore: () => ({id: 'qa-store', display_name: '测试店铺'}),
        publicationRequireReady: options.requireReady || (() => target),
        confirm: message => {confirmations.push(message); return options.accept !== false},
        json: (method, payload) => ({method, payload}),
        api: async (url, request) => {writes.push({url, request}); if (options.navigate) context.state.product='P000002'; return options.result || {ok:true, report:{state:'imported'}}},
        flowShowResult: result => rendered.push(result),
        publicationRefreshAfterSubmit: async (product, shop) => refreshed.push({product, shop}),
        notice: message => notices.push(message), encodeURIComponent});
    return {context, writes, confirmations, rendered, refreshed, notices,
        run: () => vm.runInContext(`(async()=>{${body}})()`, context)};
}
test('publish confirmation names exact warehouse, offer and stock before any live write', async () => {
    const h = harness(); await h.run();
    assert.equal(h.confirmations.length, 1);
    assert.match(h.confirmations[0], /轻小仓（22142605386000）/);
    assert.match(h.confirmations[0], /xzj\.jp\.10\.8\.1：100 件/);
    assert.match(h.confirmations[0], /导入且价格处理完成后/);
    assert.equal(h.writes.length, 1);
    assert.equal(h.writes[0].url, '/api/workbench/products/P000001/guided/submit');
    assert.equal(h.writes[0].request.payload.confirm, 'SUBMIT');
});
test('unsaved card or publication settings cannot reach submit', async () => {
    const card = harness({draft: {dirty:true}});
    await assert.rejects(card.run(), /保存所有未保存/); assert.equal(card.writes.length, 0);
    const config = harness({requireReady: () => {throw Error('仓库配置未保存')}});
    await assert.rejects(config.run(), /仓库配置未保存/); assert.equal(config.confirmations.length, 0); assert.equal(config.writes.length, 0);
});
test('cancelled confirmation never publishes or changes stock', async () => {
    const h = harness({accept:false}); await h.run(); assert.equal(h.writes.length, 0); assert.equal(h.refreshed.length, 0);
});
test('navigation during config verification cannot retarget the submission', async () => {
    let h; h = harness({requireReady: async () => {h.context.state.product='P000002'; return {}}});
    await assert.rejects(h.run(), /商品或店铺已切换/); assert.equal(h.writes.length, 0);
});
test('a late submit response refreshes the original ledger and never renders another product', async () => {
    const h = harness({navigate:true}); await h.run();
    assert.equal(h.writes[0].url, '/api/workbench/products/P000001/guided/submit');
    assert.equal(h.rendered.length, 0);
    assert.deepEqual(h.refreshed, [{product:'P000001', shop:'qa-store'}]);
});
test('an imported product awaiting inventory is directed to its ledger without a second import', async () => {
    const h = harness({result:{ok:false,report:{state:'imported',stock_update:{pending:true,status:'stock_pending'}}}});
    await h.run();
    assert.equal(h.writes.length,1);
    assert.match(h.notices[0],/库存仍待完成或回读/);
    assert.match(h.notices[0],/不要重复导入商品/);
});
