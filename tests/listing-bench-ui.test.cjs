'use strict';

// Executes the production handlers in an isolated VM. API calls are deferred
// memory fixtures; this file never starts a browser, calls a model, or publishes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const media = fs.readFileSync(path.join(root, 'web/listing-media.js'), 'utf8');
const card = fs.readFileSync(path.join(root, 'web/listing-card.js'), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));

function section(source, first, next) {
  const start = source.indexOf(first), end = source.indexOf(next, start);
  assert.ok(start >= 0 && end > start, `Production function anchors exist: ${first}`);
  return source.slice(start, end);
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function harness() {
  const editor = {dataset: {slot: 'slot1'}, value: 'first prompt'};
  const draft = {edits: {'prompt:slot1': 'first prompt', 'refs:slot1': 'R1'},
    pending: new Set(['prompt:slot1', 'refs:slot1']), details: {}, prices: {}, touched: new Set()};
  const callbacks = [];
  let context;
  context = vm.createContext({
    Map, Set, structuredClone,
    state: {product: 'P1', view: 'product', shop: 'shop-a', skus: {selected: ['S1']}, guided: {
      category_selection: {category_id: 100, type_id: 10},
      image_plan: {main_images: [], detail_images: [{slot: 'slot1', origin: 'ai', workspace: 'single'}],
        selected_slots: ['P1-old']},
      generated_image_paths: [], media_sets: [{id: 'setA', unstarted: 2}]}},
    listingFlow: {skuDrafts: new Map(), factDrafts: new Map(), studioSlots: new Map([['P1', 'slot1']]),
      mediaDrafts: new Map(), studioRequests: new Set(), steps: new Map([['P1', 'media']]),
      errors: new Map(), jobProducts: new Set()},
    listingBench: {activeSlots: new Map(), workspaces: new Map(), sets: new Map(), counts: new Map(),
      documents: new Map(), profiles: new Map(), loading: new Set(), errors: new Map()},
    productEditor: {drafts: new Map([['P1', draft]])},
    document: {querySelectorAll: () => [editor], querySelector: () => editor,
      addEventListener: (type, callback) => callbacks.push({type, callback})},
    $: selector => ({value: selector === '#benchOriginalRole' ? 'detail' : selector === '#benchSetCount' ? '2' : 'S1'}),
    productDraft: () => context.productEditor.drafts.get(context.state.product),
    flowStudioSlots: (g = context.state.guided) => [...(g.image_plan?.main_images || []), ...(g.image_plan?.detail_images || [])],
    flowStudioReferenceIds: slot => String(context.productEditor.drafts.get(context.state.product)?.edits[`refs:${slot}`] || 'R1').split(',').map(value => value.trim()),
    flowStudioSelection: (g = context.state.guided) => context.listingFlow.mediaDrafts.get(context.state.product)
      || {order: [...(g.image_plan?.selected_slots || [])], dirty: false},
    flowStudioActive: () => context.listingFlow.studioSlots.get(context.state.product) || 'new',
    benchWorkspace: () => context.listingBench.workspaces.get(context.state.product) || 'single',
    benchSetId: () => context.listingBench.sets.get(context.state.product) || 'setA',
    benchWorkspaceKey: () => `${context.state.product}:${context.benchWorkspace()}:${context.benchWorkspace() === 'set' ? context.benchSetId() : ''}`,
    benchWorkspaceSlots: (g = context.state.guided) => context.flowStudioSlots(g).filter(row =>
      (row.workspace || 'single') === context.benchWorkspace()
      && (context.benchWorkspace() !== 'set' || row.set_id === context.benchSetId())),
    benchSets: () => context.state.guided.media_sets || [],
    benchReadMedia: async () => ({}), benchShop: () => context.state.shop, benchProfileId: () => 'qa-profile',
    benchImageTile: spec => `<i data-image="${spec.slot}">${spec.slot}</i>`,
    flowButton: () => '', flowStep: () => 'media', flowStudioSlotBusy: () => false,
    flowStudioStartPolling: () => {}, flowStudioRefreshReferences: () => {}, flowStudioAction: () => {},
    imageBackendReady: () => true, confirm: () => true, renderProduct: () => {}, refreshProduct: async () => {},
    captureProductFields: () => {}, esc: value => String(value), json: (method, body) => ({method, body: JSON.stringify(body)}),
    notices: [], notice: message => context.notices.push(message),
    api: async () => { throw Error('API fixture was not installed; live calls are forbidden'); }
  });
  return {context, draft, editor, callbacks,
    load: (source, first, next) => vm.runInContext(section(source, first, next), context),
    click: async (action, extra = {}) => {
      const handler = callbacks.find(row => row.type === 'click').callback;
      const button = {dataset: {benchAction: action, ...extra}, isConnected: false};
      return handler({target: {closest: () => button}});
    }};
}

function loadMediaHandler(h) {
  h.load(media, 'function benchPaidPrerequisite(product){', "\ndocument.addEventListener('click',event=>{");
  h.load(media, "document.addEventListener('click',async event=>{", '\nconst benchShowResult=');
}

test('a late save response retains a newer prompt and its pending flag', async () => {
  const h = harness(), waiting = deferred();
  h.load(media, 'flowStudioSaveSlot=async function', '\nconst mediaStudioAction=');
  h.context.api = () => waiting.promise;
  const pending = h.context.flowStudioSaveSlot('slot1', 'P1');
  h.draft.edits['prompt:slot1'] = 'new edit while request pending';
  waiting.resolve({image_plan: {}, slot: 'slot1'});
  await pending;
  assert.equal(h.draft.edits['prompt:slot1'], 'new edit while request pending');
  assert.equal(h.draft.pending.has('prompt:slot1'), true);
  assert.equal(h.draft.pending.has('refs:slot1'), false, 'unchanged saved references can be cleared');
});

test('new-image edits made while saving stay in the new editor', async () => {
  const h = harness(), waiting = deferred();
  h.editor.dataset.slot = 'new';
  h.draft.edits = {'prompt:new': 'first prompt', 'refs:new': 'R1'};
  h.draft.pending = new Set(['prompt:new', 'refs:new']);
  h.context.listingFlow.studioSlots.set('P1', 'new');
  h.load(media, 'flowStudioSaveSlot=async function', '\nconst mediaStudioAction=');
  h.context.api = () => waiting.promise;
  const pending = h.context.flowStudioSaveSlot('new', 'P1');
  h.draft.edits['prompt:new'] = 'next image prompt';
  waiting.resolve({image_plan: {}, slot: 'created-image'});
  assert.equal(await pending, 'created-image');
  assert.equal(h.context.listingFlow.studioSlots.get('P1'), 'new');
  assert.equal(h.context.listingBench.activeSlots.get('P1:single:'), 'new');
  assert.equal(h.draft.edits['prompt:new'], 'next image prompt');
});

test('a save response does not retarget the active editor after a workspace switch', async () => {
  const h = harness(), waiting = deferred();
  h.load(media, 'flowStudioSaveSlot=async function', '\nconst mediaStudioAction=');
  h.context.api = () => waiting.promise;
  const pending = h.context.flowStudioSaveSlot('slot1', 'P1');
  h.context.listingBench.workspaces.set('P1', 'set');
  h.context.listingFlow.studioSlots.set('P1', 'set-existing');
  h.context.listingBench.activeSlots.set('P1:set:setA', 'set-existing');
  waiting.resolve({image_plan: {}, slot: 'slot1'});
  await pending;
  assert.equal(h.context.listingFlow.studioSlots.get('P1'), 'set-existing');
  assert.equal(h.context.listingBench.activeSlots.get('P1:set:setA'), 'set-existing');
  assert.equal(h.context.listingBench.activeSlots.get('P1:single:'), 'slot1');
});

test('adopting P1 original during navigation never imports P2 selections', async () => {
  const h = harness(), waiting = deferred();
  loadMediaHandler(h);
  h.context.api = () => waiting.promise;
  const pending = h.click('adopt-original', {reference: 'R1'});
  h.context.state.product = 'P2';
  h.context.state.guided.image_plan.selected_slots = ['P2-old'];
  h.context.listingFlow.studioSlots.set('P2', 'P2-editor');
  waiting.resolve({slot: 'P1-new'});
  await pending;
  assert.deepEqual(plain(h.context.listingFlow.mediaDrafts.get('P1').order), ['P1-old', 'P1-new']);
  assert.equal(h.context.listingFlow.mediaDrafts.has('P2'), false);
  assert.equal(h.context.listingFlow.studioSlots.get('P2'), 'P2-editor');
  assert.equal(h.context.listingBench.activeSlots.has('P2:single:'), false);
});

test('adopting an original preserves selection edits made while the request runs', async () => {
  const h = harness(), waiting = deferred();
  loadMediaHandler(h);
  h.context.api = () => waiting.promise;
  const pending = h.click('adopt-original', {reference: 'R1'});
  h.context.listingFlow.mediaDrafts.set('P1', {order: ['user-reordered'], dirty: true});
  waiting.resolve({slot: 'P1-new'});
  await pending;
  assert.deepEqual(plain(h.context.listingFlow.mediaDrafts.get('P1').order), ['user-reordered', 'P1-new']);
});

test('the publishing contact sheet remains global when editors are workspace-filtered', () => {
  const h = harness();
  const slots = [{slot: 'single-1', workspace: 'single', output_path: 'single.png'},
    {slot: 'set-1', workspace: 'set', set_id: 'setA', output_path: 'set.png'},
    {slot: 'original-1', workspace: 'set', set_id: 'setB', origin: 'captured', output_path: 'original.png'},
    {slot: 'not-selected', workspace: 'set', set_id: 'setA', output_path: 'unused.png'}];
  const full = {image_plan: {main_images: [], detail_images: slots,
    selected_slots: ['single-1', 'set-1', 'original-1']},
    generated_image_paths: ['single.png', 'set.png', 'original.png', 'unused.png']};
  h.context.state.guided = full;
  h.load(media, 'flowStudioResultsHtml=function(g){', '\nflowStudioSaveSlot=');
  const filtered = {...full, image_plan: {...full.image_plan, detail_images: [slots[0]]}};
  const html = h.context.flowStudioResultsHtml(filtered), contact = html.split('bench-publish-contact')[1];
  assert.match(contact, /已选上架图片 · 3 张/);
  for (const name of ['single-1', 'set-1', 'original-1']) assert.ok(contact.includes(name));
  assert.equal(contact.includes('not-selected'), false);
  assert.equal(h.context.flowStudioSelection().order.length, 3);
});

test('paid batch generation and retries reject unsaved SKU choices without an API call', async () => {
  for (const action of ['generate-set', 'retry-image']) {
    const h = harness();
    loadMediaHandler(h);
    h.context.listingFlow.skuDrafts.set('P1', ['S2']);
    let calls = 0;
    h.context.api = async () => { calls++; return {}; };
    await h.click(action, {slot: 'slot1', set: 'setA'});
    assert.equal(calls, 0, action);
    assert.match(h.context.listingFlow.errors.get('P1').message, /规格勾选尚未保存/);
  }
});

test('paid retry rejects an unsaved prompt instead of charging for old settings', async () => {
  const h = harness();
  loadMediaHandler(h);
  let calls = 0;
  h.context.api = async () => { calls++; return {}; };
  await h.click('retry-image', {slot: 'slot1'});
  assert.equal(calls, 0);
  assert.match(h.context.listingFlow.errors.get('P1').message, /先保存本张提示词和参考图/);
});

test('media confirmation does not discard new selections or advance the step', async () => {
  const h = harness(), waiting = deferred();
  h.load(media, 'const mediaStudioAction=flowStudioAction;', '\nflowStudioPollJobs=');
  h.context.listingFlow.mediaDrafts.set('P1', {order: ['first-image'], dirty: true});
  let calls = 0;
  h.context.api = () => ++calls === 1 ? waiting.promise : Promise.resolve({});
  const pending = h.context.flowStudioAction('studio-confirm', {dataset: {}, isConnected: false});
  h.context.listingFlow.mediaDrafts.set('P1', {order: ['new-image', 'first-image'], dirty: true});
  waiting.resolve({});
  await pending;
  assert.deepEqual(plain(h.context.listingFlow.mediaDrafts.get('P1').order), ['new-image', 'first-image']);
  assert.equal(h.context.listingFlow.steps.get('P1'), 'media');
});

test('late canonical documents cannot replace a different shop/category scope', async () => {
  const h = harness(), responses = new Map();
  h.load(card, 'function benchScopeKey(', '\nconst benchScalar=');
  h.load(card, 'async function benchLoadDocument(', '\nconst benchFetchProduct=');
  h.context.api = url => {
    if (url.includes('/offer-prefix')) return Promise.resolve({profile: {saved: false}});
    const shop = new URL(url, 'https://fixture.invalid').searchParams.get('shop');
    const response = deferred(); responses.set(shop, response); return response.promise;
  };
  const old = h.context.benchLoadDocument('P1', 'shop-a');
  h.context.state.shop = 'shop-b';
  h.context.state.guided.category_selection = {category_id: 200, type_id: 20};
  const current = h.context.benchLoadDocument('P1', 'shop-b');
  responses.get('shop-b').resolve({document: {shop: 'shop-b', card: {category: {category_id: 200, type_id: 20}}}});
  await current;
  responses.get('shop-a').resolve({document: {shop: 'shop-a', card: {category: {category_id: 100, type_id: 10}}}});
  await old;
  assert.equal(h.context.benchDocument().shop, 'shop-b');
  assert.equal(h.context.listingBench.documents.size, 2, 'old result may be cached only in its own scope');
  h.context.state.guided.category_selection.type_id = 99;
  assert.equal(h.context.benchDocument(), undefined, 'new category must not display an old document');
});

test('operational save preserves new packaging and price inputs received during the request', async () => {
  const h = harness(), waiting = deferred();
  h.load(card, "document.addEventListener('click',async event=>{", '\nfunction captureProductFields()');
  h.context.listingDetailKeys = ['package_weight_g'];
  h.draft.details = {package_weight_g: '120'};
  h.draft.touched.add('package_weight_g');
  h.draft.prices = {S1: {price: '99', currency: 'CNY'}};
  h.draft.pending.add('prices');
  h.context.document.querySelectorAll = () => [{dataset: {sku: 'S1'}, value: '99',
    parentElement: {querySelector: () => ({value: 'CNY'})}}];
  const calls = [];
  h.context.api = (url, request) => {
    calls.push({url, body: JSON.parse(request.body)});
    return url.endsWith('/listing-details') ? waiting.promise : Promise.resolve({});
  };
  const pending = h.click('save-operational');
  h.draft.details.package_weight_g = '130';
  h.draft.prices.S1.price = '199';
  waiting.resolve({details: {package_weight_g: 120}, provenance: {}});
  await pending;
  assert.equal(h.draft.details.package_weight_g, '130');
  assert.equal(h.draft.touched.has('package_weight_g'), true);
  assert.equal(h.draft.dirty, true);
  assert.equal(h.draft.prices.S1.price, '199');
  assert.equal(h.draft.pending.has('prices'), true);
  assert.equal(calls[1].body.prices[0].price, 99, 'only the request snapshot was saved');
});
