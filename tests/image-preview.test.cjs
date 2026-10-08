'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../web/image-preview.js'), 'utf8');
const context = vm.createContext({URL, location: {href: 'http://127.0.0.1:8766/', origin: 'http://127.0.0.1:8766'}});
vm.runInContext(source.slice(0, source.indexOf('(function ()')), context);

test('preview accepts only this workbench image route and keeps cache version', () => {
    assert.equal(context.listingPreviewUrl('/api/workbench/products/P000007/media/output/main.png?v=abc'),
        'http://127.0.0.1:8766/api/workbench/products/P000007/media/output/main.png?v=abc');
    assert.ok(context.listingPreviewUrl('/api/workbench/products/P000007/media/input/sku-images/001.jpg'));
    for (const value of ['https://detail.1688.com/offer/1.html', 'javascript:alert(1)',
        '//evil.test/api/workbench/products/P000007/media/a.png', '/api/workbench/products/P000007/media/video.mp4',
        '/api/workbench/products/P000007/media/input/a.svg', '/api/workbench/products/P000007/media/../../secrets.png']) {
        assert.equal(context.listingPreviewUrl(value), '', value);
    }
});
