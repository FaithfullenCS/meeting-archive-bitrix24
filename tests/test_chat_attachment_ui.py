"""Exercise the shipped attachment click handler with an old chat position."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_download_does_not_reload_messages_or_discard_old_position():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the shipped JavaScript")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/chat-archive.js"
    script = r'''
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
const start=src.indexOf('    $("#ca-messages").addEventListener("click", e=> {');
const end=src.indexOf('    $("#ca-messages").addEventListener("toggle"',start);
assert(start>=0 && end>start);
let handler, pending, reloads=0;
const scroll={scrollTop:240,addEventListener:(name,fn)=>handler=fn};
const button={dataset:{fileChat:'101',caFile:'77'}};
const context={ $:()=>scroll, action:(button,fn)=>{pending=fn()}, api:async()=>({queued:true}),
  notify(){}, loadMessages:async()=>{reloads++;scroll.scrollTop=900},
  openMaterialDelete(){}, showError(){}, resetForContext(){},choose(){},trackAttachment(){},Number };
vm.createContext(context);vm.runInContext(src.slice(start,end),context);
handler({target:{closest:s=>s==='[data-ca-file]'?button:null}});
Promise.resolve(pending).then(()=>{assert.equal(scroll.scrollTop,240);assert.equal(reloads,0)}).catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
