const {test}=require('node:test');
const assert=require('node:assert/strict');
const {captureDouyinDom}=require('../scripts/douyin-dom.cjs');

function activeFixture({id='123456',urlId='123456',text='音乐节买了票，有结伴的吗',author='@公开样例',authorCount=1}={}) {
  global.location={href:`https://www.douyin.com/jingxuan/search/test?modal_id=${urlId}`};
  const active={innerText:`${author}\n· 2天前\n${text}`,getAttribute:()=>id,
    querySelectorAll:s=>s.includes('video-desc')?[{innerText:text}]:s.includes('feed-video-nickname')?Array.from({length:authorCount},()=>({innerText:author})):[],
    querySelector:()=>({href:'https://www.douyin.com/user/public-example?tracking=remove'})};
  global.document={querySelectorAll:s=>s.includes('feed-active-video')?[active]:[]};
}

test('active post separates caption and author, binds ID and removes tracking',()=>{
  activeFixture();const r=captureDouyinDom().records[0];
  assert.equal(r.text,'音乐节买了票，有结伴的吗');assert.equal(r.author,'公开样例');
  assert.equal(r.content_id,'123456');assert.equal(r.author_url,'https://www.douyin.com/user/public-example');
});
test('mismatched current URL and DOM ID fail closed',()=>{
  activeFixture({urlId:'999999'});assert.throws(()=>captureDouyinDom(),/did not validate/);
});
test('nickname mistakenly selected as caption fails closed',()=>{
  activeFixture({text:'公开样例'});assert.throws(()=>captureDouyinDom(),/did not validate/);
});
test('ambiguous authors fail closed',()=>{
  activeFixture({authorCount:2});assert.throws(()=>captureDouyinDom(),/did not validate/);
});
test('feed layout reads caption DIV, not shared nickname SPAN',()=>{
  const author={innerText:'公开样例',href:'https://www.douyin.com/user/public-example'};
  const card={innerText:'公开样例\n· 2天前\n音乐节求同行',
    querySelectorAll:s=>s==='[data-e2e-vid]'?[{getAttribute:()=> '123456'}]:s==='div.RvxQrxux'?[{innerText:'音乐节求同行'}]:s==='a[href*="/user/"]'?[author]:[],
    querySelector:s=>s==='span.m53pwJvW'?author:null};
  global.location={href:'https://www.douyin.com/jingxuan/search/test'};
  global.document={querySelectorAll:s=>s==='.search-result-card'?[card]:[]};
  const r=captureDouyinDom().records[0];assert.equal(r.text,'音乐节求同行');assert.notEqual(r.text,r.author);
});
