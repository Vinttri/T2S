import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const appJs = fs.readFileSync("bench_app/static/app.js", "utf8");
const indexHtml = fs.readFileSync("bench_app/static/index.html", "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));

assert.match(indexHtml, /onclick="uploadDatasetFile\(this\)"/);
assert.match(indexHtml, /data-tab="datasets"/);
assert.match(indexHtml, /id="tab-datasets"/);
{
  function sectionById(id) {
    const start = indexHtml.indexOf(`id="${id}"`);
    assert.notEqual(start, -1, `missing ${id}`);
    const end = indexHtml.indexOf("</section>", start);
    assert.notEqual(end, -1, `unterminated ${id}`);
    return indexHtml.slice(start, end);
  }
  const datasetsSection = sectionById("tab-datasets");
  const runSection = sectionById("tab-run");
  assert.match(datasetsSection, /id="dsList"/);
  assert.match(datasetsSection, /id="datasetEditor"/);
  assert.match(datasetsSection, /onclick="uploadDatasetFile\(this\)"/);
  assert.doesNotMatch(datasetsSection, /<label>db_id<\/label>/);
  assert.match(datasetsSection, /type="hidden" id="d_dbid"/);
  assert.doesNotMatch(runSection, /id="dsList"|id="datasetEditor"|onclick="uploadDatasetFile\(this\)"/);
}

function extractBlock(marker) {
  const start = appJs.indexOf(marker);
  assert.notEqual(start, -1, `missing marker ${marker}`);
  const arrow = appJs.indexOf("=>", start);
  const bodySearchStart = marker.startsWith("const ") && arrow !== -1 ? arrow : start;
  const braceStart = appJs.indexOf("{", bodySearchStart);
  assert.notEqual(braceStart, -1, `missing block start for ${marker}`);
  let depth = 0;
  let inString = null;
  let escaped = false;
  for (let i = braceStart; i < appJs.length; i += 1) {
    const ch = appJs[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === inString) {
        inString = null;
      }
      continue;
    }
    if (ch === "'" || ch === '"' || ch === "`") {
      inString = ch;
      continue;
    }
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        let end = i + 1;
        while (appJs[end] && /[\s;]/.test(appJs[end])) end += 1;
        return appJs.slice(start, end);
      }
    }
  }
  throw new Error(`unterminated block for ${marker}`);
}

function makeElement(initial = {}) {
  return {
    value: "",
    textContent: "",
    disabled: false,
    files: [],
    classList: {
      values: new Set(),
      add(value) { this.values.add(value); },
      remove(value) { this.values.delete(value); },
      contains(value) { return this.values.has(value); },
    },
    ...initial,
  };
}

function makeContext(fetchImpl) {
  const elements = {
    d_file: makeElement({
      files: [{
        name: "uploaded.jsonl",
        text: async () => "{\"case_id\":\"c1\",\"question\":\"q\",\"gold_sql\":\"SELECT 1\"}\n",
      }],
    }),
    d_name: makeElement(),
    d_dbid: makeElement(),
    d_dsn: makeElement(),
    d_dbtype: makeElement({ value: "auto" }),
    d_id: makeElement(),
    d_path: makeElement(),
    toast: makeElement(),
  };
  const calls = [];
  const context = {
    console,
    JSON,
    Number,
    Error,
    String,
    Array,
    setTimeout: (fn) => fn(),
    fetch: fetchImpl,
    document: {
      querySelector(selector) {
        if (!selector.startsWith("#")) return null;
        return elements[selector.slice(1)] || null;
      },
      createElement() {
        return makeElement();
      },
    },
    window: {},
    __calls: calls,
  };
  const source = `
const $ = s => document.querySelector(s);
${extractBlock("const api = async")}
${extractBlock("const toast =")}
function currentDatasetId(){ return $('#d_id') ? $('#d_id').value : ''; }
function setDatasetEditorMode(d){ __calls.push(['setDatasetEditorMode', d && d.id]); }
function setDatasetPathView(d){ __calls.push(['setDatasetPathView', d && d.benchmark_path]); }
function loadDatasetCases(id){ __calls.push(['loadDatasetCases', id]); }
function loadDatasets(){ __calls.push(['loadDatasets']); }
function loadRunSelectors(){ __calls.push(['loadRunSelectors']); }
function loadChatConnectors(){ __calls.push(['loadChatConnectors']); }
${extractBlock("function normaliseDatasetDbId")}
${extractBlock("function inferDatasetDbId")}
${extractBlock("function datasetDbIdGuess")}
${extractBlock("function fillDatasetFromFile")}
${extractBlock("async function uploadDatasetFile")}
globalThis.__test = { uploadDatasetFile, inferDatasetDbId, datasetDbIdGuess };
`;
  vm.runInNewContext(source, context);
  return { context, elements, calls };
}

async function testSuccessShowsProgressAndSavesFields() {
  let resolveFetch;
  const fetchCalls = [];
  const { context, elements, calls } = makeContext(async (path, opts) => {
    fetchCalls.push({ path, opts, disabled: button.disabled, label: button.textContent });
    return new Promise((resolve) => {
      resolveFetch = () => resolve({
        ok: true,
        json: async () => ({
          id: "ds1",
          benchmark_path: "/data/datasets/uploaded.jsonl",
          dsn: "postgresql://<redacted>@db/mock",
          cases_count: 1,
        }),
      });
    });
  });
  const button = makeElement({ textContent: "Загрузить файл и сохранить" });
  const pending = context.__test.uploadDatasetFile(button);
  await tick();

  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Загружаю…");
  assert.equal(fetchCalls[0].path, "/api/datasets/upload");
  assert.equal(fetchCalls[0].disabled, true);
  assert.equal(fetchCalls[0].label, "Загружаю…");
  const body = JSON.parse(fetchCalls[0].opts.body);
  assert.equal(body.name, "uploaded");
  assert.equal(body.db_id, "uploaded");
  assert.equal(body.db_type, "auto");
  assert.equal(Object.hasOwn(body, "dsn"), false);
  assert.match(body.content, /SELECT 1/);

  resolveFetch();
  await pending;

  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Загрузить файл и сохранить");
  assert.equal(elements.d_id.value, "ds1");
  assert.equal(elements.d_path.value, "/data/datasets/uploaded.jsonl");
  assert.equal(elements.d_dsn.value, "postgresql://<redacted>@db/mock");
  assert.equal(elements.toast.textContent, "Датасет загружен: 1 кейсов");
  assert.deepEqual(calls.map((c) => c[0]), [
    "setDatasetPathView",
    "setDatasetEditorMode",
    "setDatasetPathView",
    "loadDatasetCases",
    "loadDatasets",
    "loadRunSelectors",
    "loadChatConnectors",
  ]);
}

async function testErrorRestoresButtonAndShowsBackendDetail() {
  const { context, elements } = makeContext(async () => ({
    ok: false,
    statusText: "Bad Request",
    text: async () => JSON.stringify({ detail: "DSN scoring-базы не задан. Укажите env." }),
  }));
  const button = makeElement({ textContent: "Загрузить файл и сохранить" });

  await context.__test.uploadDatasetFile(button);

  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Загрузить файл и сохранить");
  assert.equal(elements.d_path.value, "");
  assert.equal(elements.toast.textContent, "DSN scoring-базы не задан. Укажите env.");
}

async function testNameAndFileAreEnoughForUpload() {
  const fetchCalls = [];
  const { context, elements } = makeContext(async (path, opts) => {
    fetchCalls.push({ path, opts });
    return {
      ok: true,
      json: async () => ({
        id: "named-ds",
        benchmark_path: "/data/datasets/questions.jsonl",
        dsn: "postgresql://<redacted>@db/sports",
        cases_count: 1,
      }),
    };
  });
  elements.d_name.value = "Sports upload";
  elements.d_file.files[0].name = "questions.jsonl";
  const button = makeElement({ textContent: "Загрузить файл и сохранить" });

  await context.__test.uploadDatasetFile(button);

  const body = JSON.parse(fetchCalls[0].opts.body);
  assert.equal(body.name, "Sports upload");
  assert.equal(body.file_name, "questions.jsonl");
  assert.equal(body.db_id, "sports_upload");
  assert.equal(body.db_type, "auto");
  assert.equal(Object.hasOwn(body, "dsn"), false);
  assert.equal(elements.toast.textContent, "Датасет загружен: 1 кейсов");
}

async function testKnownDatasetFileInfersCanonicalDbId() {
  const fetchCalls = [];
  const { context, elements } = makeContext(async (path, opts) => {
    fetchCalls.push({ path, opts });
    return {
      ok: true,
      json: async () => ({
        id: "sports",
        benchmark_path: "/data/datasets/training_e2e_test_sports_events.jsonl",
        dsn: "postgresql://<redacted>@db/sports",
        cases_count: 1,
      }),
    };
  });
  elements.d_file.files[0].name = "training_e2e_test_sports_events.jsonl";
  const button = makeElement({ textContent: "Загрузить файл и сохранить" });

  await context.__test.uploadDatasetFile(button);

  const body = JSON.parse(fetchCalls[0].opts.body);
  assert.equal(body.name, "training_e2e_test_sports_events");
  assert.equal(body.db_id, "sports_events_large");
  assert.equal(elements.d_dbid.value, "sports_events_large");
  assert.equal(context.__test.inferDatasetDbId("BENCHMARK_dm_mis_impala_10"), "dm_mis");
  assert.equal(context.__test.inferDatasetDbId("BENCHMARK_cybermarket_pattern_large"), "cybermarket_pattern_large");
  assert.equal(context.__test.datasetDbIdGuess("questions.jsonl", "dm_mis questions"), "dm_mis");
}

function makeProgressContext(fetchImpl) {
  const elements = { toast: makeElement() };
  const calls = [];
  const context = {
    console,
    JSON,
    Number,
    Error,
    String,
    Array,
    Date,
    Set,
    fetch: fetchImpl,
    setTimeout: (fn) => fn(),
    document: {
      querySelector(selector) {
        if (!selector.startsWith("#")) return null;
        return elements[selector.slice(1)] || null;
      },
    },
    __calls: calls,
  };
const source = `
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s);
${extractBlock("const api = async")}
${extractBlock("const toast =")}
let runsMap={}, casesMap={}, rerunning=new Set(), rerunPendingSeen=new Set(), progOpen=new Set(), runLastSeen={}, casesLoading=new Set();
const ACTIVE_ST=['queued','running','paused','judging'];
const CASE_STATUS_META={
  api_waiting:['l3','ждем ответ API',true],
  llm_queued:['l2','в очереди на LLM-оценку',false],
  awaiting_judge:['l2','ожидает LLM-оценку',false],
  sent_to_judge:['l3','отправлен на оценку',true],
  judging:['l3','оценивается LLM',true],
  judged:['l4','оценка готова',false],
};
function renderProgress(){ __calls.push(['renderProgress']); }
function renderCaseStatusModal(){ __calls.push(['renderCaseStatusModal']); }
function loadRunCases(id){ __calls.push(['loadRunCases', id]); casesLoading.delete(id); }
${extractBlock("function inferCaseStatus")}
${extractBlock("function caseStatusBadge")}
${extractBlock("function rerunKey")}
${extractBlock("function rerunStatusBadge")}
${extractBlock("function progressCaseStatusBadge")}
${extractBlock("function markRerunningCase")}
${extractBlock("function clearRerunningCase")}
${extractBlock("function clearRerunningForRun")}
${extractBlock("function reloadOpenRunCases")}
${extractBlock("function handleFinishedRun")}
${extractBlock("function caseUpdateStillPending")}
${extractBlock("function mergeProgressCase")}
${extractBlock("function handleProgressCaseUpdate")}
${extractBlock("function handleProgressMessage")}
${extractBlock("function caseNeedsRerun")}
${extractBlock("async function rerunFailed")}
${extractBlock("async function rerunCase")}
globalThis.__test = {
  handleProgressMessage,
  progressCaseStatusBadge,
  rerunFailed,
  rerunCase,
  state: { runsMap, casesMap, rerunning, rerunPendingSeen, progOpen, runLastSeen },
};
`;
  vm.runInNewContext(source, context);
  return { context, elements, calls };
}

function rerunKeys(context) {
  return [...context.__test.state.rerunning].sort();
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

async function testSnapshotFinishedRunClearsRerunSpinnerAndReloadsOpenCases() {
  const { context, calls } = makeProgressContext(async () => {
    throw new Error("fetch not expected");
  });
  context.__test.state.progOpen.add("r1");
  context.__test.state.rerunning.add("r1::c1");
  context.__test.state.casesMap.r1 = [{ idx: 1, case_id: "c1", level: 0, predicted_sql: "" }];

  context.__test.handleProgressMessage({
    type: "snapshot",
    runs: [{ id: "r1", status: "done", total_cases: 1, done_cases: 1 }],
    cases: [],
  });

  assert.deepEqual(rerunKeys(context), []);
  assert.deepEqual(plain(calls.filter((c) => c[0] === "loadRunCases")), [["loadRunCases", "r1"]]);
}

async function testStoppedRunEventClearsRerunSpinner() {
  const { context } = makeProgressContext(async () => {
    throw new Error("fetch not expected");
  });
  context.__test.state.rerunning.add("r2::c1");

  context.__test.handleProgressMessage({ type: "run", run: { id: "r2", status: "stopped" } });

  assert.deepEqual(rerunKeys(context), []);
}

async function testPendingCaseEventKeepsRerunSpinnerUntilFinalStatus() {
  const { context } = makeProgressContext(async () => {
    throw new Error("fetch not expected");
  });
  context.__test.state.rerunning.add("r3::c1");

  context.__test.handleProgressMessage({
    type: "case",
    run_id: "r3",
    case: { idx: 1, case_id: "c1", case_status: "llm_queued" },
  });
  assert.deepEqual(rerunKeys(context), ["r3::c1"]);

  context.__test.handleProgressMessage({
    type: "case",
    run_id: "r3",
    case: { idx: 1, case_id: "c1", case_status: "judged", level: 4, predicted_sql: "select 1" },
  });
  assert.deepEqual(rerunKeys(context), []);
}

async function testStaleJudgedSnapshotKeepsQueuedRerunSpinner() {
  const { context } = makeProgressContext(async () => {
    throw new Error("fetch not expected");
  });
  context.__test.state.rerunning.add("r6::c1");

  context.__test.handleProgressMessage({
    type: "snapshot",
    runs: [{ id: "r6", status: "queued", total_cases: 1, done_cases: 1 }],
    cases: [{
      run_id: "r6",
      case: { idx: 1, case_id: "c1", case_status: "judged", case_status_label: "оценка готова", level: 0 },
    }],
  });

  assert.deepEqual(rerunKeys(context), ["r6::c1"]);
  assert.match(context.__test.progressCaseStatusBadge("r6", context.__test.state.casesMap.r6[0]), /перезапуск/);
}

async function testSnapshotFinalAfterPendingClearsRerunSpinner() {
  const { context } = makeProgressContext(async () => {
    throw new Error("fetch not expected");
  });
  context.__test.state.rerunning.add("r7::c1");

  context.__test.handleProgressMessage({
    type: "snapshot",
    runs: [{ id: "r7", status: "running", total_cases: 1, done_cases: 0 }],
    cases: [{ run_id: "r7", case: { idx: 1, case_id: "c1", case_status: "api_waiting" } }],
  });
  assert.deepEqual(rerunKeys(context), ["r7::c1"]);
  assert.equal(context.__test.state.rerunPendingSeen.has("r7::c1"), true);

  context.__test.handleProgressMessage({
    type: "snapshot",
    runs: [{ id: "r7", status: "running", total_cases: 1, done_cases: 1 }],
    cases: [{
      run_id: "r7",
      case: { idx: 1, case_id: "c1", case_status: "judged", case_status_label: "оценка готова", level: 4 },
    }],
  });

  assert.deepEqual(rerunKeys(context), []);
  assert.match(context.__test.progressCaseStatusBadge("r7", context.__test.state.casesMap.r7[0]), /оценка готова/);
}

async function testRerunFailedNoTargetsDoesNotFakeRunningCases() {
  const { context, elements } = makeProgressContext(async () => ({
    ok: true,
    json: async () => ({ ok: true, status: "no_targets", targets: 0 }),
  }));
  context.__test.state.progOpen.add("r4");
  context.__test.state.casesMap.r4 = [{ idx: 1, case_id: "c1", level: 4, predicted_sql: "select 1" }];

  await context.__test.rerunFailed("r4");

  assert.deepEqual(rerunKeys(context), []);
  assert.equal(elements.toast.textContent, "Нет неуспешных вопросов для перепрогона");
}

async function testRerunFailedQueuedMarksOnlyFailedCasesAndRunQueued() {
  const { context, elements } = makeProgressContext(async () => ({
    ok: true,
    json: async () => ({ ok: true, status: "queued", targets: 1, job_id: "j1" }),
  }));
  context.__test.state.casesMap.r5 = [
    { idx: 1, case_id: "ok", level: 4, predicted_sql: "select 1" },
    { idx: 2, case_id: "bad", level: 0, predicted_sql: "" },
  ];

  await context.__test.rerunFailed("r5");

  assert.equal(context.__test.state.runsMap.r5.status, "queued");
  assert.deepEqual(rerunKeys(context), ["r5::bad"]);
  assert.equal(elements.toast.textContent, "Перепрогон поставлен в очередь: 1");
}

await testSuccessShowsProgressAndSavesFields();
await testErrorRestoresButtonAndShowsBackendDetail();
await testNameAndFileAreEnoughForUpload();
await testKnownDatasetFileInfersCanonicalDbId();
await testSnapshotFinishedRunClearsRerunSpinnerAndReloadsOpenCases();
await testStoppedRunEventClearsRerunSpinner();
await testPendingCaseEventKeepsRerunSpinnerUntilFinalStatus();
await testStaleJudgedSnapshotKeepsQueuedRerunSpinner();
await testSnapshotFinalAfterPendingClearsRerunSpinner();
await testRerunFailedNoTargetsDoesNotFakeRunningCases();
await testRerunFailedQueuedMarksOnlyFailedCasesAndRunQueued();
