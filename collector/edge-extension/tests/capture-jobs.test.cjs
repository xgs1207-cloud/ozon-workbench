const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname,'..',name),'utf8');

function backgroundHarness(fetcher) {
    const store={factoryBaseUrl:'http://127.0.0.1:8766',factoryDeviceId:'test-device-1234'};
    const listeners=[];
    const opened=[];
    const context=vm.createContext({URL,URLSearchParams,fetch:fetcher,btoa:value=>Buffer.from(value).toString('base64'),
        importScripts:()=>{},chrome:{
            storage:{local:{get:async keys=>Object.fromEntries(keys.map(key=>[key,store[key]])),set:async data=>Object.assign(store,data)}},
            runtime:{onMessage:{addListener:fn=>listeners.push(fn)}},
            tabs:{create:async value=>opened.push(value)}
        }});
    vm.runInContext(read('background.js'),context);
    const send=(message,callback)=>listeners[0](message,{},callback);
    return {store,opened,send};
}

test('job acknowledgement survives a closed popup and never opens a workbench tab',async()=>{
    let payload;
    const h=backgroundHarness(async(url,options)=>{
        assert.equal(url,'http://127.0.0.1:8766/api/collector/jobs');
        payload=JSON.parse(options.body);
        return {ok:true,status:202,text:async()=>JSON.stringify({request_id:payload.request_id,state:'queued'})};
    });
    h.send({type:'FACTORY_FETCH',path:'/api/collector/jobs',options:{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({request_id:'popup-closed-request1',capture:{source_url:'https://detail.1688.com/offer/1053070588316.html',
            title_cn:'锅',skus:[{sku_id:'RED24',sku_name:'红色/内白 24cm/3.5L'}]}})}},()=>{});
    for(let i=0;i<30&&h.store.collectorCaptureJobs?.[0]?.state!=='queued';i++) await new Promise(resolve=>setImmediate(resolve));
    assert.equal(h.store.collectorCaptureJobs[0].state,'queued');
    assert.equal(h.store.collectorCaptureJobs[0].source_url,'https://detail.1688.com/offer/1053070588316.html');
    assert.equal(h.store.collectorCaptureJobs[0].skus,undefined);
    assert.equal(h.opened.length,0);
});

test('concurrent job acknowledgements keep both immutable request identities',async()=>{
    const h=backgroundHarness(async(_url,options)=>({ok:true,status:202,
        text:async()=>JSON.stringify({request_id:JSON.parse(options.body).request_id,state:'queued'})}));
    const send=id=>new Promise(resolve=>h.send({type:'FACTORY_FETCH',path:'/api/collector/jobs',options:{method:'POST',
        body:JSON.stringify({request_id:id,capture:{title_cn:id,source_url:'https://detail.1688.com/offer/1053070588316.html'}})}},resolve));
    await Promise.all([send('concurrent-request-one'),send('concurrent-request-two')]);
    assert.deepEqual(new Set(h.store.collectorCaptureJobs.map(job=>job.request_id)),
        new Set(['concurrent-request-one','concurrent-request-two']));
});

test('a lost POST response is recorded as unconfirmed and is never automatically resubmitted',async()=>{
    let calls=0;
    const h=backgroundHarness(async()=>{calls++;throw new Error('connection closed');});
    const result=await new Promise(resolve=>h.send({type:'FACTORY_FETCH',path:'/api/collector/jobs',options:{method:'POST',
        body:JSON.stringify({request_id:'uncertain-request-one',capture:{title_cn:'锅'}})}},resolve));
    assert.equal(result.ok,false);
    assert.equal(h.store.collectorCaptureJobs[0].state,'unconfirmed');
    assert.equal(calls,1);
});

test('read-only job refresh updates history without re-sending the capture',async()=>{
    const h=backgroundHarness(async(url,options)=>{
        assert.match(url,/\/jobs\/read-only-request1$/);
        assert.equal(options.method,'GET');
        assert.equal(options.body,undefined);
        return {ok:true,status:200,text:async()=>JSON.stringify({request_id:'read-only-request1',state:'completed',result:{product_id:'P000002'}})};
    });
    h.store.collectorCaptureJobs=[{request_id:'read-only-request1',title:'锅',state:'queued'}];
    await new Promise(resolve=>h.send({type:'FACTORY_FETCH',path:'/api/collector/jobs/read-only-request1'},resolve));
    assert.equal(h.store.collectorCaptureJobs[0].state,'completed');
    assert.equal(h.store.collectorCaptureJobs[0].title,'锅');
    assert.equal(h.opened.length,0);
});
