import test from 'node:test';
import assert from 'node:assert/strict';
import {safeClick,assertUrl,stateOf,BUILTIN_TOOLS} from '../policy.mjs';
const node=(text,extra={})=>({text,accessibleName:text,label:text,context:'',tag:'button',type:'button',visible:true,...extra});
test('navigation cannot click publishing, agreement, deleting, choices or account controls',()=>{
  for(const t of ['发布','提交审核','删除','同意服务协议','切换账号','编辑'])assert.equal(safeClick(node(t),{navigation:true}),false,t);
  assert.equal(safeClick(node('爱情',{type:'option'}),{navigation:true}),false);
  assert.equal(safeClick(node('内容上传'),{navigation:true}),true);
});
test('program can choose known form options, final submission requires explicit scope',()=>{
  assert.equal(safeClick(node('制作协议',{tag:'li',type:'option'})),true);
  assert.equal(safeClick(node('下一步',{label:'单集信息 制作协议'})),true);
  assert.equal(safeClick(node('提交审核')),false);
  assert.equal(safeClick(node('提交审核'),{final:true}),true);
  assert.equal(safeClick(node('删除'),{final:true}),false);
});
test('domains and failed states stop before agent actions',()=>{
  assert.throws(()=>assertUrl('https://evil.example/', ['fixture.test']));
  assert.throws(()=>assertUrl('javascript:alert(1)', ['fixture.test']));
  assertUrl('https://fixture.test/upload',['fixture.test']);
  const s={text:'平台菜单',controls:[],rows:[],dialogs:[]};
  assert.equal(stateOf(s),'shell');
  assert.equal(stateOf({...s,dialogs:['同意服务协议']}),'agreement');
  assert.equal(stateOf({...s,text:'验证码'}),'challenge');
});
test('native agent tools with bypass capabilities are excluded',()=>{
  for(const name of ['act','goto','keys','fillForm','fillFormVision','click','type','extract','navback','search'])assert.ok(BUILTIN_TOOLS.includes(name));
});
