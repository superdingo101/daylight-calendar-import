import assert from "node:assert/strict";
import test from "node:test";
import {readReturnSession, saveReturnResult} from "./return_session.js";

const id = "a".repeat(32);
const key = `daylight:return:v1:${id}`;
const now = 10000;
const record = {version: 1, id, created_at: 9000, expires_at: 11000,
  return_path: "/dashboard-family/calendar?x=1#view"};
function store(value = record) {
  let data = JSON.stringify(value);
  return {getItem: k => k === key ? data : null, setItem: (k, v) => {assert.equal(k, key); data = v;}};
}

test("return sessions preserve a validated same-origin dashboard and result", () => {
  const storage = store();
  const session = readReturnSession(storage, id, "https://ha.test", now);
  assert.equal(session.returnPath, record.return_path);
  assert.equal(saveReturnResult(storage, session, "completed", now), true);
  assert.deepEqual(JSON.parse(storage.getItem(key)).result,
    {status: "completed", calendar_may_have_changed: true, completed_at: now});
});

test("return sessions reject malformed, unsafe, missing and expired data", () => {
  for (const path of ["https://evil.test", "//evil.test", "/%2f%2fevil.test", "/\\evil.test", "/%5cevil.test", "/auth/authorize", "/daylight-calendar-import/review", "/bad%zz"]) {
    assert.equal(readReturnSession(store({...record, return_path: path}), id, "https://ha.test", now), null, path);
  }
  for (const changes of [{expires_at: now}, {created_at: now + 1}, {version: 2}, {id: "b"}]) {
    assert.equal(readReturnSession(store({...record, ...changes}), id, "https://ha.test", now), null);
  }
  assert.equal(readReturnSession(store(), "bad", "https://ha.test", now), null);
  assert.equal(readReturnSession({getItem() {throw Error();}}, id, "https://ha.test", now), null);
  assert.equal(saveReturnResult({setItem() {throw Error();}}, {key, value: record}, "completed", now), false);
});
