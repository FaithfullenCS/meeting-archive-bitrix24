from pathlib import Path
import shutil
import subprocess

import pytest


def test_participant_toggle_keeps_unchanged_buttons_and_install_icon_mounted():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for UI regression checks")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/app.js"
    script = r'''
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const render = code.slice(code.indexOf("  function renderParticipants()"), code.indexOf("  function participantCatalogueKey()"));
const toggle = code.slice(code.indexOf("  function addParticipant("), code.indexOf("  const isoDay"));
const label = code.slice(code.indexOf("  function setButtonLabel("), code.indexOf("  function decorateStaticIcons()"));
const readiness = code.slice(code.indexOf("  function renderTranscriptionControls()"), code.indexOf("  function renderLocalRun()"));
const targets = {}, state = {participants: [{id:"41",label:"Иван",count:3},{id:"42",label:"Анна",count:1}], participantIds:new Set()};
let filters=0;
function element() {let html="";const parent={};return {writes:0,value:"",dataset:{},closest(){return parent;},focus(){},set innerHTML(s){this.writes++;html=s;},get innerHTML(){return html;}};}
const context = vm.createContext({state, icon:n=>`<svg>${n}</svg>`, escapeHtml:String, requestFilter:()=>filters++,
  $:(s,root)=>root ? root.span : (targets[s]??=element())});
vm.runInContext(render+toggle+label+readiness, context);
context.renderParticipants(); const rows=targets["#participant-options"], chips=targets["#participant-chips"];
assert.equal(rows.writes,1);
state.participantsLoading=true; context.renderParticipants();
assert.equal(rows.writes,1,"Focus-triggered refresh must not remove the pending click target");
context.addParticipant("42"); context.addParticipant("41");
assert.equal(state.participantIds.size,2);
context.addParticipant("42"); assert.deepEqual([...state.participantIds],["41"]);
assert.equal(filters,3); assert(rows.innerHTML.includes('aria-pressed="true"'));
const before=rows.writes, chipBefore=chips.writes;
context.renderParticipants(); assert.equal(rows.writes,before); assert.equal(chips.writes,chipBefore);
const button=element();context.setButtonLabel(button,"Установить","download");
button.span={textContent:"Установить"};
context.setButtonLabel(button,"Установить","download");context.setButtonLabel(button,"Установка","download");
assert.equal(button.writes,1,"Polling must update the label without removing the icon");
assert.equal(button.span.textContent,"Установка");
state.bootstrap={transcription:{ready:false,reason:"Установите модель"}};state.detail={files:[{kind:"audio"}]};
context.renderTranscriptionControls();
assert(targets["#transcribe-button"].innerHTML.includes("Настроить расшифровку"));
assert.equal(targets["#transcribe-button"].disabled,false);
state.bootstrap.transcription.ready=true;context.renderTranscriptionControls();
assert(targets["#transcribe-button"].innerHTML.includes("Расшифровать локально"));
assert.equal(targets["#transcribe-mode"].closest().hidden,true);
state.detail.files.push({kind:"audio"});context.renderTranscriptionControls();
assert.equal(targets["#transcribe-mode"].closest().hidden,false);
state.detail.files=[];context.renderTranscriptionControls();
assert.equal(targets["#transcribe-button"].disabled,true);
'''
    subprocess.run([node, "-e", script, str(source)], check=True, capture_output=True, text=True)
def test_player_unknown_duration_seek_and_drag_keep_actual_position():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for UI regression checks")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/app.js"
    script = r"""
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const code=fs.readFileSync(process.argv[1],'utf8');
const functionCode=code.slice(code.indexOf('  function bindMediaControls('),code.indexOf('  function renderTranscriptionControls('));
const elements={}, events={}, media={duration:NaN,currentTime:0,paused:true,seekable:{length:0},addEventListener:(n,f)=>events[n]=f};
const context=vm.createContext({icon:String,notify:()=>{},$:s=>elements[s]??={value:'0',setAttribute(){}}});
vm.runInContext(functionCode,context);context.bindMediaControls(media,{});
const seek=elements['.media-seek'];assert.equal(seek.disabled,true);assert.equal(seek.value,0);assert.equal(seek.max,1);
media.duration=Infinity;events.loadedmetadata();assert.equal(media.currentTime,Number.MAX_SAFE_INTEGER);
media.duration=20;events.durationchange();assert.equal(media.currentTime,0);assert.equal(seek.max,20);assert.equal(seek.value,0);
seek.onpointerdown();seek.value='10';seek.oninput();assert.equal(media.currentTime,10);
media.currentTime=2;events.timeupdate();assert.equal(seek.value,'10','Playback updates must not move a thumb under the pointer');
seek.onchange();assert.equal(media.currentTime,10);events.seeked();
media.currentTime=11;events.timeupdate();assert.equal(seek.value,11);
seek.onpointerdown();seek.onpointerup();media.currentTime=12;events.timeupdate();assert.equal(seek.value,12,'A click without a change must release drag state');
seek.value='5';seek.oninput();assert.equal(media.currentTime,5);events.seeked();assert.equal(seek.value,5);
"""
    subprocess.run([node, "-e", script, str(source)], check=True, capture_output=True, text=True)
