"""Exercise the real queue render and selection handlers without a real profile."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_cancel_button_tracks_all_pages_individual_changes_and_account_switch():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the shipped JavaScript")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/app.js"
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = new Map();
function $(name) {
  if (!elements.has(name)) elements.set(name, {disabled:name==='#chat-jobs-cancel-selected', checked:false, hidden:false});
  return elements.get(name);
}
const all = {account:'synthetic-account', total:779, pending:779, items:Array.from({length:779},(_,i)=>({id:`synthetic-${i}`,kind:'history',state:'queued'}))};
const page = {...all,items:all.items.slice(0,100)};
const changes = {};
const state = {route:'jobs',bootstrap:{jobs:[]}};
const context = {state,$,Set,Map,console,Date,Number,String,escapeHtml:String,
  renderQueueGroups(){},renderChatBackground(){},
  action:async(button,callback)=>callback(),
  api:async()=>all,bootstrap:async()=>context.renderChatJobs(page),
  document:{addEventListener(name,handler){changes[name]=handler}},
  confirmAction:async()=>false,notify(){},icon(){return ''},badge(){return ''},dateString:String};
vm.createContext(context);
const render = source.slice(source.indexOf('  function renderChatJobs('),source.indexOf('  function renderModule('));
const start = source.indexOf('  function updateChatQueueSelection(');
if (start>=0) vm.runInContext(source.slice(start,source.indexOf('  function renderChatJobs(',start)),context);
vm.runInContext(render,context);
const handlerStart = source.indexOf('  $("#chat-jobs-select-all").onchange');
const handlerEnd = source.indexOf('  document.addEventListener("click",event=>{ const button=event.target.closest("[data-chat-job-action]")',handlerStart);
assert(handlerStart>=0 && handlerEnd>handlerStart);
vm.runInContext(source.slice(handlerStart,handlerEnd),context);
(async()=>{
  context.renderChatJobs({...page,held:4,blocked:2,history:300,sync:{stopped:true,metrics:{batch_size:20},rest:{batch:{requests:7}}}});
  assert.equal($('#chat-work-stop').hidden,true);
  assert.equal($('#chat-work-resume').hidden,false);
  assert.equal($('#chat-work-recovered').hidden,true);
  assert($('#chat-work-metrics').textContent.includes('4'));
  context.renderChatJobs({...page,held:4,sync:{stopped:false}});
  assert.equal($('#chat-work-stop').hidden,false);
  assert.equal($('#chat-work-recovered').hidden,false);
  assert.equal($('#chat-jobs-cancel-selected').disabled,true);
  $('#chat-jobs-select-all').checked=true;
  await $('#chat-jobs-select-all').onchange({currentTarget:$('#chat-jobs-select-all')});
  assert.equal(state.chatQueueSelected.size,779,'Select all must include unloaded pages');
  assert.equal($('#chat-jobs-cancel-selected').disabled,false,'Select all must enable Cancel');
  context.renderChatJobs(page);
  assert.equal($('#chat-jobs-cancel-selected').disabled,false,'Background refresh must keep Cancel enabled');
  const input = {checked:false,dataset:{chatJobSelect:'synthetic-0'}};
  changes.change({target:{closest:()=>input}});
  assert.equal($('#chat-jobs-select-all').indeterminate,true,'Partial selection must update Select all');
  $('#chat-jobs-select-all').checked=false;
  await $('#chat-jobs-select-all').onchange({currentTarget:$('#chat-jobs-select-all')});
  assert.equal(state.chatQueueSelected.size,0);
  assert.equal($('#chat-jobs-cancel-selected').disabled,true);
  input.checked=true;
  changes.change({target:{closest:()=>input}});
  assert.equal($('#chat-jobs-cancel-selected').disabled,false);
  context.renderChatJobs({...page,account:'other-synthetic-account'});
  assert.equal(state.chatQueueSelected.size,0,'Selections must not leak into another account');
  assert.equal($('#chat-jobs-cancel-selected').disabled,true);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_stop_resume_and_recovered_handlers_send_confirmed_account_commands():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the shipped JavaScript")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/app.js"
    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1],'utf8'),elements=new Map(),requests=[];
const context={state:{chatQueueAccount:'synthetic'},$:(id)=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);},action:async(button,fn)=>fn(),api:async(url,request)=>requests.push({url,...request}),bootstrap:async()=>{},confirmAction:async()=>true};
vm.createContext(context);
const start=text.indexOf('  for(const [id,command] of [["chat-work-stop"'),end=text.indexOf('  $("#chat-jobs-more").onclick',start);
assert(start>=0 && end>start);
vm.runInContext(text.slice(start,end),context);
(async()=>{
 for(const [id,action] of [['chat-work-stop','stop'],['chat-work-resume','resume'],['chat-work-recovered','continue_recovered']]){
  await context.$('#'+id).onclick({currentTarget:context.$('#'+id)});
  const last=requests.at(-1);
  assert.equal(last.url,'/api/chat-archive/control');
  assert.equal(last.data.account,'synthetic');assert.equal(last.data.action,action);assert.equal(last.data.confirm,true);
 }
 context.confirmAction=async()=>false;
 await context.$('#chat-work-stop').onclick({currentTarget:context.$('#chat-work-stop')});
 assert.equal(requests.length,3);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
