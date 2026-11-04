// Component tests use an in-memory DOM and mocked fetch; no browser or network.
const {test,beforeEach,afterEach}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),Module=require('node:module');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://component-test.invalid'});
Object.assign(global,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,localStorage:dom.window.localStorage,IS_REACT_ACT_ENVIRONMENT:true});
Object.defineProperty(global,'navigator',{value:dom.window.navigator,configurable:true});
const React=require('react');
const {render,screen,waitFor,cleanup,fireEvent}=require('@testing-library/react');
function load(entry){
 const root=path.resolve(__dirname,'..');
 const result=require('esbuild').buildSync({entryPoints:[path.join(root,entry)],write:false,bundle:true,format:'cjs',platform:'node',jsx:'automatic',alias:{'@':path.join(root,'src')},external:['react','react-dom']});
 const m=new Module(path.join(root,'control-test.cjs'),module);m.filename=path.join(root,'control-test.cjs');m.paths=module.paths;m._compile(result.outputFiles[0].text,m.filename);return m.exports;
}
const {default:App,makeTask}=load('src/App.tsx');
const read=f=>JSON.parse(fs.readFileSync(path.resolve(__dirname,'../..',f),'utf8').replace(/^﻿/,''));
const baseline={policy:read('profiles/default.json'),collection:read('config/collection.json')};baseline.task=makeTask(baseline);
const clone=x=>JSON.parse(JSON.stringify(x));
let state,saves,calls,routes;
beforeEach(()=>{
 state={bundle:clone(baseline),revision:'baseline',etag:'e0',token:'t'};saves=[];calls=[];routes={};localStorage.clear();
 global.fetch=async(url,options={})=>{
  calls.push(url);const body=options.body?JSON.parse(options.body):null;
  if(routes[url])return {ok:true,json:async()=>routes[url](body)};
  if(url==='/api/state')return {ok:true,json:async()=>clone(state)};
  if(url==='/api/versions')return {ok:true,json:async()=>[]};
  if(url==='/api/tasks/latest')return {ok:true,json:async()=>null};
  if(url==='/api/platforms')return {ok:true,json:async()=>['douyin','xhs','kuaishou','bilibili','weibo','tieba','zhihu'].map(id=>({id,browser_configured:id==='douyin',login_status:'not_verified'}))};
  if(url==='/api/preview')return {ok:true,json:async()=>({accepted:true,score:62,topic:'relevant',stance:'positive',intent:'none',promotion:'no',author_role:'unknown',reason:'本地测试',quality_flags:[]})};
  if(url==='/api/save'){saves.push(clone(body));state={bundle:clone(body.bundle),revision:'r'+saves.length,etag:'e'+saves.length,token:'t'};return {ok:true,json:async()=>clone(state)}}
  throw Error('Unexpected request: '+url);
 };
});
afterEach(()=>cleanup());

test('distribution includes no personalized presets',async()=>{
 render(React.createElement(App));await screen.findByLabelText('筛选目标');
 assert.equal(document.querySelectorAll('.preset').length,0);assert.equal(saves.length,0);
});

test('sandbox verdict and theme switch work without network',async()=>{
 render(React.createElement(App));await screen.findByLabelText('筛选目标');
 assert.equal(document.documentElement.dataset.theme,'dark');
 fireEvent.click(screen.getByRole('button',{name:'运行判定'}));
 await screen.findByText('潜在沾边');assert.ok(screen.getByText('本地测试'));
 fireEvent.click(screen.getByRole('button',{name:'切换明暗主题'}));
 assert.equal(document.documentElement.dataset.theme,'light');assert.equal(localStorage.getItem('npl_theme'),'light');
});

test('platform tiles stay a draft and never open login windows automatically',async()=>{
 const opened=[];routes['/api/platforms/open-login']=b=>{opened.push(b.platform);return {message:'窗口已显示'}};
 render(React.createElement(App));await screen.findByLabelText('筛选目标');
 fireEvent.click(screen.getByRole('button',{name:'全选'}));
 await waitFor(()=>assert.equal(screen.getByLabelText('采集知乎').checked,true));
 assert.deepEqual(opened,[]);assert.equal(saves.length,0);
 fireEvent.click(screen.getAllByRole('button',{name:'登录'})[1]);
 await screen.findByText('窗口已显示');assert.deepEqual(opened,['xhs']);
});

test('monitor streams new records into the feed with a single polling source',async()=>{
 const run={run_id:'ab'.repeat(16),status:'running',phase:'collecting'};let polls=0;
 routes['/api/tasks/latest']=()=>run;routes['/api/tasks/status']=()=>run;
 routes['/api/tasks/live']=()=>{polls++;const more=polls>1;return {run_id:run.run_id,status:'running',
  captured:[{id:'1',platform:'douyin',kind:'comment',text:'<script>literal text</script>',keyword:'搭子'},...(more?[{id:'2',platform:'xhs',kind:'post',text:'第二条真实记录'}]:[])],
  candidates:more?[{id:'c1',platform:'xhs',text:'明确找同行的原句',reason:'本人表达',evidence_level:'explicit',score:88,author:'小王'}]:[],
  captured_total:more?2:1,candidate_total:more?1:0,processed:more?2:0,needs_review:0,
  collection:{requests:3,platforms:['douyin','xhs'],platform_statuses:[{platform:'douyin',status:'running',requests:3}]},report_url:null}};
 render(React.createElement(App));
 fireEvent.click(await screen.findByRole('button',{name:'实时情报流'}));
 await screen.findByText('<script>literal text</script>');
 assert.equal(document.querySelectorAll('.feed-row script').length,0);
 assert.equal(screen.queryByText('第二条真实记录'),null);
 await waitFor(()=>assert.ok(screen.getByText('第二条真实记录')),{timeout:4000});
 assert.ok(document.querySelector('.feed-row.fresh'));
 assert.ok(screen.getByText('明确找同行的原句'));assert.ok(screen.getByText('采集中'));
 const live=calls.filter(u=>u==='/api/tasks/live').length,status=calls.filter(u=>u==='/api/tasks/status').length;
 assert.ok(live<=status+1,`live ${live} vs status ${status}`);
});
