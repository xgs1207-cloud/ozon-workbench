'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/listing-flow.js'), 'utf8');
const renderer = source.slice(source.indexOf('function flowOperatorPointsHtml'), source.indexOf('function flowAnalysisHtml'));
function render(payload, display) {
    const context = vm.createContext({state: {product: 'P1'}, listingBench: {summaryTranslations: new Map(),loading:new Set()},
        benchScopeKey: () => 'P1:shop',
        benchDocument: () => ({summary: {display_zh: display}}),
        esc: text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
        flowButton: label => `<button>${label}</button>`});
    vm.runInContext(renderer, context);
    return context.flowOperatorPointsHtml({workflow: {analysis: {payload}}});
}
test('operator display prefers Chinese fields without showing Russian claims', () => {
    const html = render({selling_points: [{text: 'русский текст', point_cn: '中文卖点'}]});
    assert.match(html, /中文卖点/);
    assert.doesNotMatch(html, /русский/);
    assert.doesNotMatch(html, /转为中文/);
});
test('legacy Russian points invite explicit translation instead of inventing facts', () => {
    const html = render({selling_points: [{text: 'русский текст'}]}, {status: 'needs_translation'});
    assert.match(html, /卖点转为中文/);
    assert.match(html, /首次转换会调用文本模型/);
    assert.doesNotMatch(html, /русский/);
});
test('cached translations are presentation only and escaped', () => {
    const html = render({selling_points: [{text: 'русский текст'}]}, {status: 'ready', is_translation: true,
        selling_points: [{text: '中文<script>卖点'}]});
    assert.match(html, /中文&lt;script&gt;卖点/);
    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /原摘要和俄文上架文案保持不变/);
    assert.doesNotMatch(html, /转为中文/);
});
