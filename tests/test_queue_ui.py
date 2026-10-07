"""Use the actual queue renderer across polling and pagination, with synthetic data."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_queue_groups_preserve_expansion_totals_actions_and_escape_titles():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the queue renderer regression test")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/app.js"
    script = r'''
const assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
const source=fs.readFileSync(process.argv[1],"utf8");
const functions=source.slice(source.indexOf("  function queueGroups("),source.indexOf("  function renderModule("));
const state={bootstrap:{jobs:[]},route:"jobs"};
const elements=new Map(); let visibleGroups=[];
function $(selector) {
  if(!elements.has(selector)) elements.set(selector,{innerHTML:"",textContent:"",contains:()=>false});
  return elements.get(selector);
}
const context=vm.createContext({state,Date,Map,document:{activeElement:null},$,
  $$: selector=>selector==="details.queue-group" ? visibleGroups : [],
  icon:name=>`<svg data-icon="${name}"></svg>`,badge:state=>`<span>${state}</span>`,
  escapeHtml:value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
  dateString:value=>value,kinds:{fetch:"Сохранение записи и текста"}});
vm.runInContext(functions,context);
const overview=[{key:"chat:101",total:9,pending:8,running:1,failed:0}];
const history={id:"history",chat:101,group_key:"chat:101",title:"Demo <script>",group:"tasks",task_id:900,state:"running",kind:"history",label:"История чата",messages:800,pages:4};
const file={...history,id:"file",state:"queued",kind:"file",label:"Вложение",filename:"demo.txt"};
context.renderChatJobs({account:"synthetic",items:[history,file],groups:overview,total:9,pending:8});
let html=$("#chat-jobs-list").innerHTML;
assert.equal((html.match(/class="queue-group"/g)||[]).length,1,"History and files belong to the same chat group");
assert.match(html,/Чат задачи №900/); assert.match(html,/Работ: 9/); assert.match(html,/показано: 2/);
assert.match(html,/Обработано сообщений: 800/); assert.match(html,/data-chat-job-action="cancel"/);
assert.match(html,/Demo &lt;script&gt;/); assert.ok(!html.includes("Demo <script>"));
assert.match(html,/data-queue-group="chat:chat:101" open/);
visibleGroups=[{dataset:{queueGroup:"chat:chat:101"},open:false}];
context.renderChatJobs({account:"synthetic",items:[history,file],groups:overview,total:9,pending:8});
assert.ok(!$("#chat-jobs-list").innerHTML.includes('data-queue-group="chat:chat:101" open'),"Polling must preserve a deliberately collapsed group");
visibleGroups=[];
context.renderChatJobs({account:"synthetic",items:[{...file,id:"other",chat:102,group_key:"chat:102",state:"failed"}],total:10,pending:8},true);
assert.equal(state.chatQueueItems.length,3,"Pagination keeps the existing chat group");
assert.match($("#chat-jobs-list").innerHTML,/data-chat-job-action="retry"/);
context.renderChatJobs({account:"other-account",items:[{...history,state:"done"}],total:1,pending:0},false,true);
assert.equal(state.chatQueueItems.length,1,"Account switch must clear old jobs");
assert.ok(!$("#chat-jobs-list").innerHTML.includes('data-queue-group="chat:chat:101" open'),"Completed groups start collapsed");
context.renderJobs([{id:1,meeting_id:10,title:"Demo meeting",state:"queued",kind:"fetch"},
                    {id:2,meeting_id:10,title:"Demo meeting",state:"done",kind:"transcribe"}]);
assert.equal(($("#jobs-list").innerHTML.match(/class="queue-group"/g)||[]).length,1);
assert.match($("#jobs-list").innerHTML,/Demo meeting/); assert.match($("#jobs-list").innerHTML,/data-job-cancel="1"/);
context.renderChatBackground({connected:true,worker_running:true,paused:true,last_progress:{at:1,chat:101}});
assert.match($("#queue-chat-status").innerHTML,/Ручные загрузки продолжаются/);
context.renderChatBackground({connected:true,worker_running:false,last_progress:{}});
assert.match($("#queue-chat-status").innerHTML,/не запущен/);
'''
    subprocess.run([node, "-e", script, str(source)], check=True, capture_output=True, text=True)
