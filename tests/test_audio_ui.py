"""Check the audio setting using the actual UI function without a browser."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_auto_local_forces_audio_and_unlocks_it_when_disabled():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI logic test")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/app.js"
    script = r'''
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const fn = source.slice(source.indexOf("  function updateAudioDownloadPolicy()"), source.indexOf("  function updateDiarization()"));
const audio = {checked:false}, local = {checked:false}, hint = {};
const context = vm.createContext({$: selector => selector === "#settings-form" ? {elements:{auto_download_audio:audio, auto_local:local}} : hint});
vm.runInContext(fn, context);
context.updateAudioDownloadPolicy();
assert.equal(audio.checked, false); assert.equal(audio.disabled, false);
local.checked = true; context.updateAudioDownloadPolicy();
assert.equal(audio.checked, true); assert.equal(audio.disabled, true);
local.checked = false; context.updateAudioDownloadPolicy();
assert.equal(audio.disabled, false); // User can now choose whether to keep audio.
audio.checked = false; context.updateAudioDownloadPolicy();
assert.equal(audio.checked, false); assert.match(hint.textContent, /только текст Follow-up/);
'''
    subprocess.run([node, "-e", script, str(source)], check=True, capture_output=True, text=True)


def test_download_labels_reflect_saved_policy_for_both_entry_points():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the UI logic test')
    source = Path(__file__).resolve().parents[1] / 'meeting_archive/static/app.js'
    script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const fn = source.slice(source.indexOf('  function updateDownloadButtons()'), source.indexOf('  function updateDiarization()'));
const buttons = {'#download-selected':{}, '#detail-download':{}};
const state = {bootstrap:{settings:{auto_download_audio:false, auto_local:false}}};
const context = vm.createContext({state, $:selector=>buttons[selector], setButtonLabel:(button,label)=>button.label=label});
vm.runInContext(fn,context);
context.updateDownloadButtons();
for (const button of Object.values(buttons)) { assert.match(button.label,/Follow-up/); assert.match(button.title,/без аудио/); }
state.bootstrap.settings.auto_download_audio=true;
context.updateDownloadButtons();
for (const button of Object.values(buttons)) { assert.doesNotMatch(button.label,/Follow-up/); assert.match(button.title,/аудиозапись/); }
state.bootstrap.settings.auto_download_audio=false; state.bootstrap.settings.auto_local=true;
context.updateDownloadButtons(); assert.match(buttons['#detail-download'].title,/аудиозапись/);
'''
    subprocess.run([node, '-e', script, str(source)], check=True, capture_output=True, text=True)
