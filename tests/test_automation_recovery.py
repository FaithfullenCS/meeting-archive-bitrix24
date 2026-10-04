import json
from pathlib import Path

import pytest

from test_service import service as service_fixture, record
from test_app import ui as ui_fixture, login

service = service_fixture
ui = ui_fixture


def test_auto_transcription_uses_available_borrowed_model(service, monkeypatch):
    service.settings.auto_local = True
    meeting = service.db.upsert(service.settings.portal, record())
    folder = service.archive.ensure(meeting)
    audio = folder / 'audio/voice.wav'
    audio.write_bytes(b'synthetic audio')
    service.db.update_meeting(meeting['id'], folder=str(folder), audio='saved')
    monkeypatch.setattr(service.module, 'require_engine', lambda _: None)
    def unavailable_own_runtime(*_):
        raise ValueError('Owned runtime absent; borrowed model is available')
    monkeypatch.setattr(service.module, 'python', unavailable_own_runtime)
    service.defer_local(meeting['id'], [audio.name])
    jobs = service.db.rows("SELECT * FROM jobs WHERE kind='transcribe'")
    assert len(jobs) == 1
    assert json.loads(jobs[0]['payload'])['automatic']


@pytest.mark.asyncio
async def test_diarization_cannot_enable_with_stale_verification_without_token(ui):
    client, service, _ = ui
    csrf = await login(client)
    service.db.set_state('hf_verified', 'true')
    response = await client.post('/api/settings', json={'diarization': True}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 400
    assert not service.settings.diarization


def test_auto_wait_has_visible_readiness_reason(service, monkeypatch):
    service.settings.auto_local = True
    monkeypatch.setattr(service.module, 'require_engine', lambda _: (_ for _ in ()).throw(ValueError('Install selected model')))
    meeting = service.db.upsert(service.settings.portal, record())
    service.defer_local(meeting['id'], ['voice.wav'])
    assert service.db.get_state('auto_local_error') == 'Install selected model'


@pytest.mark.asyncio
async def test_manual_bitrix_download_automatically_queues_borrowed_model_once(service, monkeypatch):
    import httpx
    from unittest.mock import AsyncMock
    service.settings.auto_local = True
    meeting = service.db.upsert(service.settings.portal, record())
    monkeypatch.setattr(service.module, 'require_engine', lambda _: None)
    monkeypatch.setattr(service.module, 'python', lambda *_: (_ for _ in ()).throw(ValueError('No owned runtime')))
    monkeypatch.setattr(service.client, 'followup', AsyncMock(return_value=record(tracks=[{'trackId': 1, 'fileName': 'voice.wav', 'fileSize': 5, 'relUrl': '/recording'}])))
    await service.client.http.aclose()
    service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'voice')))
    job_id = service.request_download(meeting['id'], automatic=False)
    job = service.db.rows('SELECT * FROM jobs WHERE id=?', (job_id,))[0]
    await service.fetch(job, lambda *_: None)
    assert service.db.meeting(meeting['id'])['local'] == 'queued'
    await service.fetch(job, lambda *_: None)
    assert len(service.db.rows("SELECT * FROM jobs WHERE kind='transcribe'")) == 1


def test_picker_toggle_search_and_diarization_gate_without_browser():
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    source = Path(__file__).resolve().parents[1] / 'meeting_archive/static/app.js'
    script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const controls = {}, state = {chats: [{id:'42',label:'Project A',count:2},{id:'43',label:'Project B',count:1}], chatIds: new Set(), bootstrap:{secret_status:{hf_token_saved:false},hf_verified:false}};
const diarization = {checked:true,disabled:false}; let requests=0;
function $(key) { if(key === '#settings-form') return {elements:{diarization}}; return controls[key] ||= {value:'',innerHTML:'',hidden:false,textContent:'',focus:()=>{}}; }
const context=vm.createContext({state,$,clearTimeout:()=>{},icon:()=>'',escapeHtml:String,requestFilter:()=>requests++});
vm.runInContext(source.slice(source.indexOf('  function updateDiarization()'),source.indexOf('  function updateScheduleVisibility()'))+source.slice(source.indexOf('  function renderChats()'),source.indexOf('  const isoDay')),context);
context.updateDiarization(); assert.equal(diarization.disabled,true);assert.equal(diarization.checked,false);
state.bootstrap={secret_status:{hf_token_saved:true},hf_verified:true};context.updateDiarization();assert.equal(diarization.disabled,false);
context.addChatChoice('42');assert.equal(state.chatIds.has('42'),true);
context.addChatChoice('42');assert.equal(state.chatIds.has('42'),false);
context.addChatChoice('42');context.addChatChoice('43');assert.equal(state.chatIds.size,2);
$('#chat-search').value='42';context.renderChats();assert.ok($('#chat-options').innerHTML.includes('Project A'));assert.ok(!$('#chat-options').innerHTML.includes('Project B'));assert.equal(requests,4);
'''
    subprocess.run([node, '-e', script, str(source)], check=True, capture_output=True, text=True)
