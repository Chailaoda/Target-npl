const {test, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<html><body></body></html>', {url:'http://review.invalid'});
Object.assign(global,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,localStorage:dom.window.localStorage,IS_REACT_ACT_ENVIRONMENT:true});
Object.defineProperty(global,'navigator',{value:dom.window.navigator,configurable:true});
const React = require('react');
const {render,screen,fireEvent,act,cleanup,waitFor} = require('@testing-library/react');
function load(entry) {
  const root=path.resolve(__dirname,'..');
  const result=require('esbuild').buildSync({entryPoints:[path.join(root,entry)],write:false,bundle:true,format:'cjs',platform:'node',jsx:'automatic',alias:{'@':path.join(root,'src')},external:['react','react-dom']});
  const m=new Module(path.join(root,'memory-test.cjs'),module);m.filename=path.join(root,'memory-test.cjs');m.paths=module.paths;m._compile(result.outputFiles[0].text,m.filename);return m.exports;
}
const {patternsToTags,updatePatterns}=load('src/presets.ts');
const {default:App,makeTask}=load('src/App.tsx');
const baseline={policy:JSON.parse(fs.readFileSync(path.resolve(__dirname,'../../profiles/default.json'),'utf8')),collection:JSON.parse(fs.readFileSync(path.resolve(__dirname,'../../config/collection.json'),'utf8'))};baseline.task=makeTask(baseline);
afterEach(cleanup);
test('complex expressions survive adding literal tags, including escaped punctuation',()=>{
  const original=['有.{0,8}(?:结伴|同行|一起|搭子).{0,5}(?:吗|么)','C\\+\\+|找搭子'];
  const updated=updatePatterns(original,[...patternsToTags(original),'求队友']);
  assert.ok(updated.includes(original[0]));
  assert.equal(updated.some(p=>new RegExp(p).test('怎么配置')),false);
  assert.ok(updated.some(p=>new RegExp(p).test('C++')));
  assert.ok(updated.some(p=>new RegExp(p).test('求队友')));
  assert.equal(updatePatterns(original,[]).length,0);
});
function setup({hold=false,run=false}={}) {
  let state={bundle:structuredClone(baseline),etag:'e0',revision:'baseline',token:'mock'};
  const saves=[];let release;
  const live={run_id:'a'.repeat(32),status:'completed',collection:{},captured:[],captured_total:0,candidates:[],candidate_total:0,processed:0,needs_review:0,report_url:null};
  global.fetch=async(url,options={})=>{
    if(url==='/api/state')return {ok:true,json:async()=>structuredClone(state)};
    if(url==='/api/versions'||url==='/api/platforms')return {ok:true,json:async()=>[]};
    if(url==='/api/tasks/latest')return {ok:true,json:async()=>run?{run_id:live.run_id,status:'completed'}:null};
    if(url==='/api/tasks/live')return {ok:true,json:async()=>live};
    if(url==='/api/save'){
      const b=JSON.parse(options.body).bundle;saves.push(structuredClone(b));
      if(hold){hold=false;await new Promise(r=>release=r);}
      b.task.human_overrides=['/policy/topic_patterns'];state={bundle:b,etag:'e'+saves.length,revision:'r'+saves.length};
      return {ok:true,json:async()=>structuredClone(state)};
    }
    throw Error('Unexpected '+url);
  };
  render(React.createElement(App));
  return {saves,get release(){return release}};
}
function addTag(label,text){let e=screen.getByText(label);while(!e.querySelector('input'))e=e.parentElement;const input=e.querySelector('input');fireEvent.change(input,{target:{value:text}});fireEvent.keyDown(input,{key:'Enter',code:'Enter'});}
test('save preserves newer edits AND server-created manual locks',async()=>{
  const env=setup({hold:true});const goal=await screen.findByLabelText('筛选目标');
  addTag('主题核心词','新主题');fireEvent.click(screen.getByRole('button',{name:'保存草稿'}));
  await waitFor(()=>assert.ok(env.release));fireEvent.change(goal,{target:{value:'保存期间的新目标'}});
  await act(async()=>env.release());await screen.findByText('已保存，下次抓取将使用这一版本');
  assert.equal(goal.value,'保存期间的新目标');
  fireEvent.click(screen.getByRole('button',{name:'保存草稿'}));await waitFor(()=>assert.equal(env.saves.length,2));
  assert.ok(env.saves[1].task.human_overrides.includes('/policy/topic_patterns'));
  assert.deepEqual(env.saves[0].policy.rules,baseline.policy.rules);
});
test('bulk blacklist preserves promotion and cancellation expressions',async()=>{
  const env=setup({run:true});await screen.findByLabelText('筛选目标');
  fireEvent.click(screen.getByRole('button',{name:'实时情报流'}));
  const input=await screen.findByPlaceholderText('排除包含，如：教练 卖课');fireEvent.change(input,{target:{value:'教练 卖课'}});
  fireEvent.click(screen.getByRole('button',{name:'加入黑名单'}));fireEvent.click(screen.getByRole('button',{name:'保存草稿'}));
  await waitFor(()=>assert.equal(env.saves.length,1));const saved=env.saves[0];
  assert.deepEqual(saved.task.exclusions.terms,['教练','卖课']);
  for(const p of baseline.policy.rules.promotion_yes)assert.ok(saved.policy.rules.promotion_yes.includes(p));
  assert.deepEqual(saved.policy.decision_gates.cancelled_plan,baseline.policy.decision_gates.cancelled_plan);
});
