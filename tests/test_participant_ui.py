"""Exercise the real UI polling functions across asynchronous catalogue updates."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_participants_refresh_after_catalogue_pages_completion_and_failed_request():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the browser polling regression test")
    source = Path(__file__).resolve().parents[1] / "meeting_archive/static/app.js"
    script = r'''
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const participants = source.slice(source.indexOf("  function participantCatalogueKey()"), source.indexOf("  function openParticipants("));
const bootstrap = source.slice(source.indexOf("  async function bootstrap("), source.indexOf('  document.addEventListener("click"'));
let now = 100000, calls = 0, readyCalls = 0, fail = false, people = [];
let data = {connected: true, settings: {portal: "synthetic", user_id: 41}, catalogue: {total: 0, count: 0, running: true}};
const state = {route: "archive", lastListAt: now, participants: [], participantIds: new Set(["synthetic:user:42"])};
const context = vm.createContext({state, Date: {now: () => now},
  chatsNeedRefresh: () => false, renderParticipants: () => {}, renderBootstrap: value => {state.bootstrap = value;},
  loadMeetings: async () => {state.lastListAt = now;},
  api: async route => {
    if (route === "/api/bootstrap") return structuredClone(data);
    if (route === "/api/desktop/ready") { readyCalls++; return {ready:true}; }
    assert.equal(route, "/api/participants", "Polling must never fetch meeting materials");
    calls++;
    if (fail) throw new Error("temporary failure");
    return {items: people};
  }
});
vm.runInContext(participants + bootstrap, context);
(async () => {
  await context.bootstrap(); // The first page has not arrived yet.
  assert.equal(state.participants.length, 0);
  assert.equal(calls, 1);
  await context.bootstrap();
  assert.equal(calls, 1, "Unchanged catalogue should reuse a fresh list");
  people = [{id: "synthetic:user:42", label: "Анна", count: 1}];
  data.catalogue.total = 1; data.catalogue.count = 1;
  await context.bootstrap();
  assert.equal(state.participants.length, 1, "A new page must replace the empty list");
  assert.equal(calls, 2);
  data.catalogue.running = false;
  await context.bootstrap();
  assert.equal(calls, 3, "Completion must refresh even if total is unchanged");
  now += 13000; people = [...people, {id: "synthetic:user:43", label: "Иван"}];
  await context.bootstrap();
  assert.equal(state.participants.length, 2, "Same-sized catalogue metadata must refresh too");
  fail = true; data.settings.user_id = 99;
  await context.bootstrap();
  assert.equal(state.participantsLoaded, false);
  assert.equal(state.participantError, "temporary failure");
  fail = false; await context.bootstrap();
  assert.equal(state.participantsLoaded, true, "A failed load must retry without reloading the page");
  assert.equal(state.participantError, "");
  assert.equal(state.participantIds.has("synthetic:user:42"), true, "Refresh must preserve the selected filter");
  assert.equal(readyCalls, 1, "Window registration must not become a background polling loop");
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    subprocess.run([node, "-e", script, str(source)], check=True, capture_output=True, text=True)
