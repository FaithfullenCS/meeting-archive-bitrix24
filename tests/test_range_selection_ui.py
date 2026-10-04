"""Exercise the actual range selection function without opening a browser."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_shift_ranges_in_both_directions_and_missing_anchor():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    path=Path(__file__).resolve().parents[1]/'meeting_archive/static/app.js'
    script=r'''
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const fn=source.slice(source.indexOf('  function selectMeeting('),source.indexOf('  async function loadMeetings()'));
const state={items:[{id:90},{id:10},{id:70},{id:20},{id:50}],selected:new Set([999]),selectionAnchor:null};
const inputs=state.items.map(item=>({dataset:{select:String(item.id)}}));
let rendered=0;
const context=vm.createContext({state,$$:()=>inputs,renderSelection:()=>rendered++});
vm.runInContext(fn,context);
const chosen=()=>[...state.selected].sort((a,b)=>a-b);
context.selectMeeting(10,true,false); context.selectMeeting(50,true,true);
assert.deepEqual(chosen(),[10,20,50,70,999]); assert.equal(state.selectionAnchor,10);
assert.deepEqual(inputs.map(x=>x.checked),[false,true,true,true,true]);
context.selectMeeting(50,false,false); context.selectMeeting(10,false,true);
assert.deepEqual(chosen(),[999]); // Shift can also clear a range backwards.
context.selectMeeting(50,true,false); context.selectMeeting(10,true,true);
assert.deepEqual(chosen(),[10,20,50,70,999]);
state.selectionAnchor=123456; context.selectMeeting(90,true,true);
assert.equal(state.selectionAnchor,90); assert.equal(state.selected.has(90),true);
assert.equal(rendered,7);
'''
    subprocess.run([node,'-e',script,str(path)],check=True,capture_output=True,text=True)
