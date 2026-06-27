const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const api = async (path, opts={}) => {
  const r = await fetch(path, {headers:{'Content-Type':'application/json'}, ...opts});
  if(!r.ok){
    let t=await r.text();
    try{
      const parsed=JSON.parse(t);
      if(parsed && parsed.detail) t = Array.isArray(parsed.detail) ? parsed.detail.map(x=>x.msg||JSON.stringify(x)).join('; ') : parsed.detail;
    }catch(e){}
    throw new Error(String(t||r.statusText).slice(0,600));
  }
  return r.json();
};
const esc = s => { const d=document.createElement('div'); d.textContent=s==null?'':s; return d.innerHTML; };
const sqlBlock = (sql, fallback='(нет SQL)') => {
  const val = (sql==null || String(sql).trim()==='') ? fallback : sql;
  return `<div class="codeblk sqlblk">${esc(val)}</div>`;
};
const toast = (m, ms=2200) => { const t=$('#toast'); t.textContent=m; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),ms); };
async function copyText(text){
  try{
    if(navigator.clipboard && window.isSecureContext){ await navigator.clipboard.writeText(text||''); }
    else{
      const ta=document.createElement('textarea'); ta.value=text||''; ta.style.position='fixed'; ta.style.left='-9999px';
      document.body.appendChild(ta); ta.focus(); ta.select(); document.execCommand('copy'); ta.remove();
    }
    toast('Скопировано');
  }catch(e){ toast('Не удалось скопировать'); }
}
function askConfirm(msg){ return new Promise(res=>{
  $('#confirmMsg').textContent=msg; $('#confirmModal').classList.remove('hide');
  const done=v=>{ $('#confirmModal').classList.add('hide'); $('#confirmYes').onclick=$('#confirmNo').onclick=$('#confirmModal').onclick=null; res(v); };
  $('#confirmYes').onclick=()=>done(true);
  $('#confirmNo').onclick=()=>done(false);
  $('#confirmModal').onclick=e=>{ if(e.target.id==='confirmModal') done(false); };  // click backdrop = Нет
}); }
// ---------- theme ----------
function setChartTheme(){ if(window.Chart){ const cs=getComputedStyle(document.documentElement);
  Chart.defaults.color=cs.getPropertyValue('--muted').trim()||'#888';
  Chart.defaults.borderColor=cs.getPropertyValue('--border').trim()||'#ddd'; } }
function applyTheme(t){ document.documentElement.setAttribute('data-theme',t);
  try{ localStorage.setItem('benchTheme',t); }catch(e){}
  const lbl=$('#themeLbl'); if(lbl) lbl.textContent = t==='dark'?'Тёмная':'Светлая';
  setChartTheme(); }
function toggleTheme(){ const cur=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  applyTheme(cur);
  if(!$('#tab-leaderboard').classList.contains('hide')) loadLeaderboard();  // redraw charts in new theme
}
const levelBadge = l => (l===null || l===undefined || Number.isNaN(+l))
  ? '<span class="badge l2">ждёт оценки</span>'
  : `<span class="badge l${l}">L${l}</span>`;
const effLevel = c => (c && c.human_level!=null) ? c.human_level : (c ? (c.auto_level!=null?c.auto_level:c.level) : null);
// dual badge: when a human override is set, show BOTH (человек + ЛЛМ/авто)
function gradeBadge(c){
  const auto = (c.auto_level!=null?c.auto_level:c.level);
  if(c.human_level!=null)
    return `<span class="badge l${c.human_level}" title="оценка человека">👤 L${c.human_level}</span> <span class="muted" style="font-size:.7rem">· ЛЛМ ${levelBadge(auto)}</span>`;
  return levelBadge(auto);
}
// labelled editor with explicit Save button — "Оценка человека"
function gradeControl(runId, caseId, c){
  const cur = c.human_level;
  const opts = ['<option value="">— нет (авто) —</option>']
    .concat([4,3,2,1,0].map(n=>`<option value="${n}" ${cur===n?'selected':''}>L${n}</option>`)).join('');
  return `<div class="kv" style="margin:8px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap" onclick='event.stopPropagation()'>
    <b>✍️ Оценка человека:</b>
    <select title="поставить оценку вручную">${opts}</select>
    <button class="btn sm" onclick='saveGrade(this,${JSON.stringify(runId)},${JSON.stringify(caseId)})'>💾 Сохранить</button>
    <span class="muted" style="margin-left:6px">сейчас: ${gradeBadge(c)}</span>
  </div>`;
}
window.saveGrade = async function(btn, runId, caseId){
  const sel = btn.parentElement.querySelector('select'); if(!sel) return;
  const val = sel.value, level = val==='' ? null : +val;
  try{
    await api('/api/runs/'+runId+'/grade',{method:'POST',body:JSON.stringify({case_id:caseId,level})});
    toast(level==null?'Сохранено: оценка человека убрана (→ авто)':'Сохранено: оценка человека L'+level);
    reloadAfterGrade(runId);
  }catch(e){ toast(e.message); }
};
function reloadAfterGrade(runId){
  if(!$('#tab-results').classList.contains('hide')){ const rv=$('#res_revision'); loadResults(rv&&rv.value?rv.value:runId); }
  else if(!$('#tab-progress').classList.contains('hide')){ delete casesMap[runId]; loadRunCases(runId); }
  else if(!$('#tab-leaderboard').classList.contains('hide')){ lbState.cache={}; loadLeaderboard(); }
}

let progressTimer=null, currentRunId=null;

// ---------- tabs ----------
$$('.navi').forEach(b=>b.onclick=()=>{
  $$('.navi').forEach(x=>x.classList.toggle('active', x===b));
  $$('main > section').forEach(s=>s.classList.add('hide'));
  $('#tab-'+b.dataset.tab).classList.remove('hide');
  try{ localStorage.setItem('benchTab', b.dataset.tab); }catch(e){}
  if(b.dataset.tab==='run'){ loadRunSelectors(); loadSettings(); }
  if(b.dataset.tab==='datasets') loadDatasets();
  if(b.dataset.tab==='results') loadResultDatasets();
  if(b.dataset.tab==='connectors') loadConnectors();
  if(b.dataset.tab==='chat') loadChatConnectors();
  if(b.dataset.tab==='leaderboard') loadLeaderboard();
  if(b.dataset.tab==='review') loadReviews();
  if(b.dataset.tab==='settings') loadSettings();
  if(b.dataset.tab==='progress') startProgress();
  else clearInterval(progressTimer);
});

// ---------- runtime settings ----------
let runtimeSettings=null;
const ynBadge = (ok, yes='включено', no='выключено') =>
  `<span class="badge ${ok?'l4':'l1'}">${ok?yes:no}</span>`;
function settingsValue(v, fallback='—'){
  return (v===null || v===undefined || v==='') ? fallback : esc(v);
}
async function loadSettings(){
  try{
    runtimeSettings = await api('/api/settings');
    renderSettings();
    renderRunJudgeSummary();
  }catch(e){
    const msg=`<div class="badge l1">${esc(e.message)}</div>`;
    if($('#settingsJudgeOut')) $('#settingsJudgeOut').innerHTML=msg;
    if($('#runJudgeSummary')) $('#runJudgeSummary').innerHTML=msg;
  }
}
function renderSettings(){
  if(!runtimeSettings) return;
  const j=runtimeSettings.judge||{}, env=j.env||{};
  const limits=runtimeSettings.limits||{}, lenv=limits.env||{};
  if($('#settingsJudgeOut')){
    $('#settingsJudgeOut').innerHTML=`<div class="settings-grid">
      <div class="settings-item"><div class="lab">${esc(env.auto_judge||'BENCH_APP_AUTO_JUDGE')}</div><div class="val">${ynBadge(!!j.auto_judge)}</div></div>
      <div class="settings-item"><div class="lab">Готовность</div><div class="val">${ynBadge(!!j.ready,'готово','не настроено')}</div></div>
      <div class="settings-item"><div class="lab">${esc(env.base_url||'LLM_BASE_URL')}</div><div class="val mono">${settingsValue(j.base_url)}</div></div>
      <div class="settings-item"><div class="lab">${esc(env.model||'LLM_MODEL')}</div><div class="val mono">${settingsValue(j.model)}</div></div>
      <div class="settings-item"><div class="lab">${esc(env.api_key||'LLM_API_KEY')}</div><div class="val">${j.api_key_set?'<span class="badge l4">задан</span>':'<span class="badge l1">не задан</span>'}</div></div>
      <div class="settings-item"><div class="lab">${esc(env.auth_header||'LLM_AUTH_HEADER')} / ${esc(env.auth_scheme||'LLM_AUTH_SCHEME')}</div><div class="val mono">${settingsValue(j.auth_header)} · ${settingsValue(j.auth_scheme)}</div></div>
      <div class="settings-item"><div class="lab">${esc(env.timeout||'LLM_JUDGE_TIMEOUT')} / ${esc(env.test_timeout||'LLM_TEST_TIMEOUT')}</div><div class="val">${settingsValue(j.timeout)} сек judge · ${settingsValue(j.test_timeout)} сек test</div></div>
      <div class="settings-item"><div class="lab">${esc(env.concurrency||'LLM_JUDGE_CONCURRENCY')}</div><div class="val">${settingsValue(j.concurrency)} потоков</div></div>
      <div class="settings-item"><div class="lab">${esc(env.max_retries||'LLM_JUDGE_MAX_RETRIES')} / ${esc(env.retry_delay||'LLM_JUDGE_RETRY_DELAY')}</div><div class="val">${settingsValue(j.max_retries)} ретраев · ${settingsValue(j.retry_delay)} сек</div></div>
      <div class="settings-item"><div class="lab">${esc(lenv.api_concurrency||'BENCH_APP_MAX_API_CONCURRENCY')}</div><div class="val">${settingsValue(limits.api_concurrency)} вопрос API одновременно</div></div>
      <div class="settings-item"><div class="lab">${esc(lenv.judge_concurrency||'LLM_JUDGE_CONCURRENCY')}</div><div class="val">${settingsValue(limits.judge_concurrency)} вопрос на LLM-оценке одновременно</div></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn sm" id="llmTestBtn" onclick="testLLMConnection()" ${j.ready?'':'disabled'}>Проверить LLM</button>
      <span class="muted">Короткий реальный запрос к <span class="mono">${settingsValue(j.model)}</span></span>
    </div>
    <div id="llmTestOut" style="margin-top:10px"></div>`;
  }
  if($('#settingsStoreOut')){
    const s=runtimeSettings.store||{};
    const lg=runtimeSettings.logging||{}, lenv=lg.env||{};
    $('#settingsStoreOut').innerHTML=`<div class="settings-grid">
      <div class="settings-item"><div class="lab">Тип</div><div class="val">${settingsValue(s.type)}</div></div>
      <div class="settings-item"><div class="lab">BENCH_STORE_URL</div><div class="val mono">${settingsValue(s.url)}</div></div>
      <div class="settings-item"><div class="lab">data dir</div><div class="val mono">${settingsValue(s.data_dir)}</div></div>
      <div class="settings-item"><div class="lab">runs / answers / judged</div><div class="val mono">${settingsValue(s.runs_dir)}<br>${settingsValue(s.answers_dir)}<br>${settingsValue(s.judged_dir)}</div></div>
      <div class="settings-item"><div class="lab">logs</div><div class="val mono">${settingsValue(s.logs_dir)}</div></div>
      <div class="settings-item"><div class="lab">uploaded datasets</div><div class="val mono">${settingsValue(s.datasets_dir)}</div></div>
      <div class="settings-item"><div class="lab">YAML sync</div><div class="val">${ynBadge(!!runtimeSettings.connector_yaml_sync)}</div></div>
      <div class="settings-item"><div class="lab">${esc(lenv.stdout_run_logs||'BENCH_APP_STDOUT_RUN_LOGS')}</div><div class="val">${ynBadge(!!lg.stdout_run_logs,'docker logs on','docker logs off')}</div></div>
      <div class="settings-item"><div class="lab">${esc(lenv.stdout_max_chars||'BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS')}</div><div class="val">${settingsValue(lg.stdout_max_chars)} символов на строку</div></div>
    </div>`;
  }
  if($('#settingsDbOut')){
    const db=runtimeSettings.scoring_db||{}, ssl=runtimeSettings.ssl||{};
    $('#settingsDbOut').innerHTML=`<div class="settings-grid">
      <div class="settings-item"><div class="lab">Источник</div><div class="val mono">${settingsValue(db.source)}</div></div>
      <div class="settings-item"><div class="lab">DB type</div><div class="val">${settingsValue(db.db_type)}</div></div>
      <div class="settings-item"><div class="lab">Dataset</div><div class="val">${settingsValue(db.dataset_name||db.dataset_id)}</div></div>
      <div class="settings-item"><div class="lab">DSN</div><div class="val mono">${settingsValue(db.safe_dsn)}</div></div>
      <div class="settings-item"><div class="lab">${esc(ssl.env||'BENCH_APP_SSL_VERIFY')}</div><div class="val">${ynBadge(!!ssl.http_verify,'verify on','verify off')}</div></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn sm" id="dbTestBtn" onclick="testDbConnection()" ${db.safe_dsn?'':'disabled'}>Проверить DB</button>
      <span class="muted">Реальный короткий SELECT 1 из backend-а</span>
    </div>
    <div id="dbTestOut" style="margin-top:10px"></div>`;
  }
}
async function testLLMConnection(){
  const out=$('#llmTestOut'), btn=$('#llmTestBtn');
  if(out) out.innerHTML='<div class="muted">Отправляю тестовый запрос в LLM…</div>';
  if(btn) btn.disabled=true;
  try{
    const r=await api('/api/settings/llm-test',{method:'POST'});
    if(r.ok){
      if(out) out.innerHTML=`<div class="badge l4">LLM отвечает</div>
        <div class="kv"><b>Model</b> <span class="mono">${settingsValue(r.model)}</span></div>
        <div class="kv"><b>Latency</b> ${settingsValue(r.elapsed_s)} сек</div>
        <label>Ответ LLM</label><div class="codeblk" style="max-height:120px">${esc(r.content||'')}</div>`;
    }else{
      if(out) out.innerHTML=`<div class="badge l1">LLM недоступна</div>
        <div class="kv"><b>Model</b> <span class="mono">${settingsValue(r.model)}</span></div>
        <div class="kv"><b>Error</b> <span>${esc(r.error||'unknown error')}</span></div>`;
    }
  }catch(e){
    if(out) out.innerHTML=`<div class="badge l1">${esc(e.message)}</div>`;
  }finally{
    if(btn) btn.disabled=false;
  }
}
async function testDbConnection(){
  const out=$('#dbTestOut'), btn=$('#dbTestBtn');
  if(out) out.innerHTML='<div class="muted">Проверяю подключение к scoring DB…</div>';
  if(btn) btn.disabled=true;
  try{
    const r=await api('/api/settings/db-test',{method:'POST'});
    if(r.ok){
      if(out) out.innerHTML=`<div class="badge l4">DB отвечает</div>
        <div class="kv"><b>Source</b> <span class="mono">${settingsValue(r.source)}</span></div>
        <div class="kv"><b>DSN</b> <span class="mono">${settingsValue(r.dsn)}</span></div>
        <div class="kv"><b>Latency</b> ${settingsValue(r.elapsed_s)} сек</div>
        <div class="kv"><b>Rows</b> ${settingsValue(r.row_count)}</div>`;
    }else{
      if(out) out.innerHTML=`<div class="badge l1">DB недоступна</div>
        <div class="kv"><b>Source</b> <span class="mono">${settingsValue(r.source)}</span></div>
        <div class="kv"><b>DSN</b> <span class="mono">${settingsValue(r.dsn)}</span></div>
        <div class="kv"><b>Error</b> <span>${esc(r.error||'unknown error')}</span></div>`;
    }
  }catch(e){
    if(out) out.innerHTML=`<div class="badge l1">${esc(e.message)}</div>`;
  }finally{
    if(btn) btn.disabled=false;
  }
}
function renderRunJudgeSummary(){
  const el=$('#runJudgeSummary'); if(!el) return;
  const j=(runtimeSettings&&runtimeSettings.judge)||{};
  const limits=(runtimeSettings&&runtimeSettings.limits)||{};
  const status = !j.auto_judge ? ynBadge(false,'','выключено') : ynBadge(!!j.ready,'готово','не настроено');
  el.innerHTML=`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <b>Оценивать L0–L4 отдельной моделью</b> ${status}
    <span class="muted">env-only</span>
  </div>
  <div class="kv"><b>Model</b> <span class="mono">${settingsValue(j.model)}</span></div>
  <div class="kv"><b>Base URL</b> <span class="mono">${settingsValue(j.base_url)}</span></div>
  <div class="kv"><b>API key</b> ${j.api_key_set?'<span class="badge l4">задан</span>':'<span class="badge l1">не задан</span>'}</div>
  <div class="kv"><b>Глобальные лимиты</b> API: ${settingsValue(limits.api_concurrency)} · LLM: ${settingsValue(limits.judge_concurrency)}</div>
  <div class="kv"><b>Judge limits</b> ${settingsValue(j.timeout)} сек · ${settingsValue(j.concurrency)} потоков</div>
  <div class="kv"><b>Judge retries</b> ${settingsValue(j.max_retries)} · пауза ${settingsValue(j.retry_delay)} сек</div>`;
  applyRunConcurrencyLimit();
}

function apiConcurrencyLimit(){
  const v=runtimeSettings&&runtimeSettings.limits&&runtimeSettings.limits.api_concurrency;
  const n=Number(v);
  return Number.isFinite(n) && n>0 ? Math.floor(n) : null;
}
function applyRunConcurrencyLimit(){
  const input=$('#r_concurrency'), note=$('#runConcurrencyNote');
  const limit=apiConcurrencyLimit();
  if(!input) return;
  if(limit){
    input.max=String(limit);
    const cur=Math.max(1, Number(input.value||1));
    if(cur>limit) input.value=String(limit);
    if(note) note.innerHTML=`Максимум из env <span class="mono">BENCH_APP_MAX_API_CONCURRENCY</span>: ${esc(limit)} вопрос(ов) одновременно.`;
  }else if(note){
    note.innerHTML='';
  }
}

// ---------- solution reviews ----------
let _reviews=null;
async function loadReviews(){
  if(!_reviews){ try{ _reviews=await api('/api/reviews'); }catch(e){ $('#reviewOut').innerHTML='<div class="muted">Не удалось загрузить обзоры.</div>'; return; } }
  if(!_reviews.length){ $('#reviewOut').innerHTML='<div class="muted">Обзоров пока нет.</div>'; return; }
  $('#reviewNav').innerHTML=_reviews.map((r,i)=>`<span class="pill ${i===0?'on':''}" data-i="${i}">${esc(r.title)}</span>`).join('');
  $$('#reviewNav .pill').forEach(el=>el.onclick=()=>showReview(+el.dataset.i));
  showReview(0);
}
let _reviewIdx=0;
function showReview(i){
  _reviewIdx=i;
  $$('#reviewNav .pill').forEach((el,j)=>el.classList.toggle('on', j===i));
  const r=_reviews[i], md=r.body;
  $('#reviewOut').innerHTML=`<div style="display:flex;justify-content:flex-end;margin-bottom:8px">
      <button class="btn sm ghost" onclick="editReview()">✏️ Редактировать</button></div>
    <div class="md-body">${window.marked?marked.parse(md):esc(md)}</div>`;
}
function editReview(){
  const r=_reviews[_reviewIdx];
  $('#reviewOut').innerHTML=`<label>Markdown обзора</label>
    <textarea id="revEdit" rows="22" style="font-family:'JetBrains Mono',monospace;font-size:.82rem">${esc(r.body)}</textarea>
    <div style="margin-top:10px;display:flex;gap:8px">
      <button class="btn sm" onclick="saveReview()">Сохранить</button>
      <button class="btn sm ghost" onclick="showReview(_reviewIdx)">Отмена</button></div>`;
}
async function saveReview(){
  const r=_reviews[_reviewIdx], body=$('#revEdit').value;
  try{ await api('/api/reviews/save',{method:'POST',body:JSON.stringify({id:r.id, body})});
    toast('Обзор сохранён'); _reviews=null; await loadReviews();
    const idx=_reviews.findIndex(x=>x.id===r.id); if(idx>=0) showReview(idx);
  }catch(e){ toast(e.message); }
}

// ---------- leaderboard ----------
const accColor = a => a==null?'var(--muted)': a>=50?'var(--green)': a>=20?'var(--amber)': a>0?'#dc2626':'var(--muted)';
let lbCharts = {}, lbState = {};
const CHART_COLORS=['#e30611','#16a34a','#2f74e0','#d97706','#7c3aed','#0891b2','#db2777','#65a30d'];
const LEVEL_LEGEND=[
  ['l4','L4','точное совпадение','результат запроса полностью совпал с эталоном (с учётом порядка/множества строк) — задача решена верно'],
  ['l3','L3','исполнился, но ответ не тот','SQL валиден и выполнился, но строки/набор колонок расходятся с эталоном — логическая ошибка (не та агрегация/фильтр/лишние колонки)'],
  ['l2','L2','эталон не выполнился','предсказанный SQL ок, но сам gold-запрос упал на этой БД — проблема кейса/окружения, не модели'],
  ['l1','L1','SQL не исполняется','SQL сгенерирован, но не выполняется: синтаксис, несуществующая таблица/колонка, несовпадение типов'],
  ['l0','L0','нет SQL','модель не вернула SQL вовсе — пустой ответ, отказ или таймаут'],
];

async function loadLeaderboard(){
  const ds = await api('/api/datasets');
  const sel = $('#lb_dataset');
  const dmId = (ds.find(x=>x.name==='dm_mis') || ds.find(x=>x.db_id==='dm_mis') || {}).id;   // open dm_mis by default
  if(sel.options.length !== ds.length){ const cur=sel.value; sel.innerHTML=ds.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join(''); sel.value = cur || dmId || (ds[0]&&ds[0].id); }
  if(!ds.length){ $('#lbCards').innerHTML='<div class="muted">Нет датасетов.</div>'; $('#lbTable').innerHTML=''; return; }
  const did = sel.value || dmId || ds[0].id; sel.value = did;
  const unfin = $('#lb_unfinished') && $('#lb_unfinished').checked;
  let d; try{ d=await api('/api/compare?dataset_id='+did+(unfin?'&include_unfinished=true':'')); }
  catch(e){ $('#lbCards').innerHTML='<div class="muted">Нет прогонов для этого бенчмарка.</div>'; $('#lbTable').innerHTML=''; return; }
  lbState = {did, parts:d.participants, tasks:d.tasks, dataset:d.dataset,
             active:new Set(d.participants.map(p=>p.name)), cache:{},
             levelShow:new Set([0,1,2,3,4])};
  lbRender();
}
function lbRender(){
  const {parts, tasks, dataset, active} = lbState;
  const shown = parts.filter(p=>active.has(p.name));
  const best = shown[0] || parts[0];
  const fmtT = s => s>=60? (s/60).toFixed(1)+' мин' : Math.round(s)+' сек';
  $('#lbCards').innerHTML=`
    <div class="summary-card"><div class="lab">База данных</div><div class="val">${esc(dataset.name)}</div><div class="sub2">${esc(dataset.db_id||'')} · ${esc(dataset.db_type||'postgres')}</div></div>
    <div class="summary-card"><div class="lab">Участников</div><div class="val">${shown.length}<span class="muted" style="font-size:.9rem">/${parts.length}</span></div><div class="sub2">показано в фильтре</div></div>
    <div class="summary-card"><div class="lab">Задач</div><div class="val">${lbState.tasks.length}</div></div>
    <div class="summary-card"><div class="lab">Лучшая accuracy</div><div class="val" style="color:var(--accent)">${best?(best.summary.accuracy??'—')+'%':'—'}</div><div class="sub2">${best?esc(best.name):''}</div></div>`;
  const lf=$('#lbLevelFilter');
  if(lf){ lf.innerHTML=[4,3,2,1,0].map(n=>`<span class="pill ${lbState.levelShow.has(n)?'on':'off'} badge-lvl" data-lvl="${n}">L${n}</span>`).join('');
    $$('#lbLevelFilter .pill').forEach(el=>el.onclick=()=>{ const n=+el.dataset.lvl;
      if(lbState.levelShow.has(n)){ if(lbState.levelShow.size>1) lbState.levelShow.delete(n);} else lbState.levelShow.add(n); lbRender(); }); }
  drawBar(shown); drawScatter(shown);
  $('#lbPills').innerHTML = parts.map(p=>`<span class="pill ${active.has(p.name)?'on':'off'}" data-m="${esc(p.name)}">${esc(p.name)} · ${p.summary.accuracy??'—'}%</span>`).join('');
  $$('#lbPills .pill').forEach(el=>el.onclick=()=>{ const n=el.dataset.m; if(active.has(n)){ if(active.size>1) active.delete(n);} else active.add(n); lbRender(); });
  $('#lbLegend').innerHTML = LEVEL_LEGEND.map(([c,b,t,desc])=>`<div class="legend-item"><span class="badge ${c}">${b}</span><span><b>${t}</b> <span class="muted">— ${desc}</span></span></div>`).join('');
  const revOpts = p => (p.revisions||[]).map(r=>`<option value="${r.run_id}" ${r.run_id===p.run_id?'selected':''}>${new Date(r.created_at*1000).toLocaleString('ru-RU')} · ${r.accuracy??'—'}%</option>`).join('');
  const revSel = p => `<br><select class="lbrev" data-m="${esc(p.name)}" title="ревизия прогона">${(p.revisions||[]).length? revOpts(p) : '<option value="">— нет прогонов —</option>'}</select>`;
  const stBadge=p=>(p.status&&p.status!=='done')?` <span class="badge l3"><span class="spin">↻</span> ${esc(p.status)}</span>`:'';
  const head=`<tr><th>Задача</th><th>Сложн.</th>${shown.map(p=>`<th title="${esc(p.name)}">${esc(p.name)}${stBadge(p)}<br><span class="muted" style="font-weight:500">${p.summary.accuracy??'—'}% · ${p.summary.passed??'?'}/${p.summary.total??'?'} · мед. ${fmtT(p.median_elapsed||0)}</span>${revSel(p)}</th>`).join('')}<th></th></tr>`;
  const rows=tasks.map(t=>`<tr><td class="mono">${esc(t.case_id)}</td><td>${esc(t.difficulty||'')}</td>${shown.map(p=>{const c=p.cases[t.case_id];return c?`<td title="${c.human_level!=null?'оценка человека (ЛЛМ: L'+(c.auto_level!=null?c.auto_level:c.level)+')':''}">${c.human_level!=null?'👤':''}${levelBadge(c.level)}</td>`:'<td class="muted">—</td>';}).join('')}<td><button class="btn sm ghost lb-more" data-case="${esc(t.case_id)}">подробнее ▸</button></td></tr>`).join('');
  $('#lbTable').innerHTML=`<table>${head}${rows}</table>`;
  $$('#lbTable .lb-more').forEach(b=>b.onclick=()=>lbExpand(b.closest('tr'), b.dataset.case, b));
  $$('#lbTable .lbrev').forEach(s=>s.onchange=()=>onRevChange(s.dataset.m, s.value));
}
async function onRevChange(model, runId){
  const p = lbState.parts.find(x=>x.name===model); if(!p) return;
  try{
    const run = await api('/api/runs/'+runId);
    const cmap={}, times=[];
    (run.cases||[]).forEach(c=>{ cmap[c.case_id]={level:c.level,predicted_sql:c.predicted_sql,error:c.error,elapsed_s:c.elapsed_s}; times.push(c.elapsed_s||0); });
    times.sort((a,b)=>a-b);
    const n=times.length, med = n? (n%2? times[(n-1)>>1] : (times[n/2-1]+times[n/2])/2) : 0;
    p.run_id=runId; p.when=run.created_at; p.summary=run.summary||{}; p.cases=cmap; p.median_elapsed=Math.round(med*10)/10;
    lbState.cache={};   // case-detail cache no longer matches selected revisions
    lbRender();
  }catch(e){ toast(e.message); }
}
function downloadLeaderboard(){
  if(!lbState.parts){ toast('Нет данных'); return; }
  const blob={dataset:lbState.dataset, task_count:lbState.tasks.length, tasks:lbState.tasks, participants:lbState.parts};
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([JSON.stringify(blob,null,2)],{type:'application/json'}));
  a.download=`results_${(lbState.dataset.db_id||lbState.dataset.name||'benchmark')}.json`;
  a.click(); URL.revokeObjectURL(a.href);
}
function resultTable(res, rows){
  const cols=res.columns||[];
  const head=cols.length ? `<tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>` : '';
  return `<table class="mono" style="margin-top:6px;font-size:.72rem">${head}${(rows||[]).map(r=>`<tr>${(r||[]).map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</table>`;
}
function resultBlock(res){
  if(!res) return '<div class="muted" style="font-size:.74rem">нет результата</div>';
  if(res.error) return `<div class="badge l1" style="margin-top:4px">ошибка: ${esc(res.error)}</div>`;
  if(!res.columns||!res.columns.length) return '<div class="muted" style="font-size:.74rem">пустой результат</div>';
  const allRows=res.rows||[];
  const total=(res.row_count===null || res.row_count===undefined) ? allRows.length : +res.row_count;
  const previewRows=allRows.slice(0,8);
  const preview=resultTable(res, previewRows);
  if(total<=previewRows.length && allRows.length<=previewRows.length) return preview;
  const olderTruncated=total>allRows.length;
  const label=olderTruncated
    ? `Показать сохранённые строки (${allRows.length} из ${total})`
    : `Показать весь ответ (${total} строк)`;
  const note=olderTruncated
    ? '<div class="result-full-note">Этот старый прогон был сохранён в усечённом формате. Для полного ответа перепрогоните вопрос или прогон.</div>'
    : '';
  return `${preview}
    <details class="result-full">
      <summary>${label}</summary>
      <div class="result-full-table">${resultTable(res, allRows)}</div>
      ${note}
    </details>`;
}
async function lbExpand(tr, caseId, btn){
  const nx=tr.nextElementSibling;
  $$('#lbTable .lb-more').forEach(b=>b.textContent='подробнее ▸');
  if(nx && nx.classList.contains('detail-row')){ nx.remove(); return; }
  $$('#lbTable .detail-row').forEach(e=>e.remove());
  let d=lbState.cache[caseId];
  if(!d){ try{ const rids=lbState.parts.map(p=>p.run_id).join(','); d=await api('/api/case?dataset_id='+lbState.did+'&case_id='+encodeURIComponent(caseId)+'&run_ids='+encodeURIComponent(rids)); lbState.cache[caseId]=d; }catch(e){ return; } }
  if(btn) btn.textContent='скрыть ▾';
  const shown=lbState.active;
  const cols=3+lbState.active.size;
  const models=d.models.filter(m=>shown.has(m.name));
  lbState.detailModels=models; lbState.detailCaseId=caseId;   // for the model selector + manual grading
  const opts=`<option value="__all__">— все модели —</option>`+models.map(m=>`<option value="${esc(m.name)}">${esc(m.name)} · ${m.level==null?'ждёт оценки':'L'+m.level}</option>`).join('');
  const html=`<tr class="detail-row"><td colspan="${cols}">
    <div style="margin-bottom:8px"><b>Вопрос:</b> ${esc(d.question||'')}</div>
    <div class="grid2">
      <div><label>✅ Gold SQL — как отправлен в БД</label>${sqlBlock(d.gold_sql,'')}${resultBlock(d.gold_result)}</div>
      <div><label>Ответ модели</label>
        <select id="lbModelSel" style="margin-bottom:8px" onchange="lbPickModel(this.value)">${opts}</select>
        <div id="lbModelAnswers"></div></div>
    </div></td></tr>`;
  tr.insertAdjacentHTML('afterend', html);
  lbPickModel(models.length?models[0].name:'__all__');   // по умолчанию — первая модель отдельно
}
function lbModelBlock(m){
  return `<div style="margin-bottom:10px"><b>${esc(m.name)}</b> ${gradeBadge(m)} <span class="muted" style="font-size:.72rem">${m.elapsed_s!=null?m.elapsed_s+'s':''}${m.reason?' · '+esc(m.reason):''}</span>
    ${gradeControl(m.run_id, lbState.detailCaseId, m)}
    ${sqlBlock(m.predicted_sql,'(нет SQL)')}${resultBlock(m.agent_result)}${m.error?`<div class="badge l1" style="margin-top:4px">${esc(m.error)}</div>`:''}
    ${m.raw_response?`<details style="margin-top:6px"><summary class="muted" style="cursor:pointer">🛈 сырой ответ API</summary><div class="codeblk" style="max-height:360px;white-space:pre-wrap;overflow:auto">${esc(m.raw_response)}</div></details>`:''}</div>`;
}
function lbPickModel(v){
  const models=lbState.detailModels||[];
  const list = v==='__all__' ? models : models.filter(m=>m.name===v);
  const el=$('#lbModelAnswers'); if(!el) return;
  el.innerHTML = list.map(lbModelBlock).join('') || '<div class="muted">нет данных</div>';
  const sel=$('#lbModelSel'); if(sel) sel.value=v;
}
const LEVEL_COLOR={4:'#16a34a',3:'#2f74e0',2:'#d97706',1:'#dc2626',0:'#9aa3b2'};
function drawBar(parts){ const el=document.getElementById('lbBar'); if(lbCharts.bar)lbCharts.bar.destroy();
  const show=lbState.levelShow||new Set([0,1,2,3,4]);
  // один столбик на модель, сегменты — выбранные уровни (стек)
  const datasets=[4,3,2,1,0].filter(n=>show.has(n)).map(n=>({
    label:'L'+n, data:parts.map(p=>(p.summary||{})['L'+n]||0),
    backgroundColor:LEVEL_COLOR[n], stack:'lv'}));
  lbCharts.bar=new Chart(el,{type:'bar',data:{labels:parts.map(p=>p.name),datasets},
    options:{plugins:{legend:{display:true,position:'bottom',labels:{boxWidth:10,font:{size:10}}},
      tooltip:{callbacks:{label:ctx=>` ${ctx.dataset.label}: ${ctx.parsed.y} кейсов`}}},
      scales:{x:{stacked:true},y:{stacked:true,beginAtZero:true,title:{display:true,text:'кейсов'}}},maintainAspectRatio:false}});}
function drawScatter(parts){ const el=document.getElementById('lbScatter'); if(lbCharts.sc)lbCharts.sc.destroy();
  const fmtT=s=>s>=60?(s/60).toFixed(1)+' мин':Math.round(s)+' с';
  lbCharts.sc=new Chart(el,{type:'scatter',data:{datasets:parts.map((p,i)=>({label:p.name,data:[{x:p.median_elapsed||0,y:p.summary.accuracy||0}],backgroundColor:CHART_COLORS[i%CHART_COLORS.length],pointRadius:8,pointHoverRadius:10}))},
    options:{plugins:{legend:{display:true,position:'bottom',labels:{boxWidth:10,font:{size:10}}},
      tooltip:{callbacks:{title:items=>items[0].dataset.label, label:ctx=>[`accuracy: ${ctx.parsed.y}%`,`медиана API: ${fmtT(ctx.parsed.x)}`]}}},
    scales:{x:{title:{display:true,text:'медианное время API, сек'},beginAtZero:true},y:{beginAtZero:true,max:100,title:{display:true,text:'accuracy %'}}},maintainAspectRatio:false}});}

// ---------- connectors ----------
async function loadConnectors(){
  const list = await api('/api/connectors');
  $('#connList').innerHTML = list.length ? list.map(c=>
    `<div class="list-item" onclick='editConnector(${JSON.stringify(c.id)})'><b>${esc(c.name)}</b>
     <span class="badge l3" style="margin-left:8px" title="тип БД / диалект">${esc(c.default_dialect||'postgres')}</span>
     ${c.db_id?`<span class="badge l4" title="db_id">${esc(c.db_id)}</span>`:''}
     <span class="muted mono" style="margin-left:auto;font-size:.72rem">${esc(c.method)} ${esc((c.url||'').slice(0,24))}</span>
     <button class="btn sm ghost" style="padding:2px 8px" title="Получить curl" onclick='event.stopPropagation();curlConnectorById(${JSON.stringify(c.id)})'>curl</button>
     <button class="btn sm ghost" style="color:#dc2626;padding:2px 8px" title="Удалить" onclick='event.stopPropagation();delConnectorById(${JSON.stringify(c.id)},${JSON.stringify(c.name)})'>×</button></div>`).join('')
    : '<div class="muted">Пока нет коннекторов.</div>';
  window._connectors = Object.fromEntries(list.map(c=>[c.id,c]));
}
function newConnector(){ ['c_id','c_name','c_url','c_headers','c_body','c_field','c_pattern','c_description','c_dbid','c_testdb'].forEach(i=>$('#'+i).value='');
  $('#c_testdialect').value='postgres';
  $('#c_method').value='POST'; $('#c_dialect').value='postgres'; $('#c_mode').value='json';
  $('#c_timeout').value=200; $('#c_attempts').value=1; $('#c_retry_delay').value=0; $('#connFormTitle').textContent='Новый коннектор';
  $('#delConnBtn').style.display='none'; $('#connTestOut').innerHTML=''; testSeq++; toggleRegexRow(); }
function toggleRegexRow(){ const r=$('#regexRow'); if(r) r.classList.toggle('hide', $('#c_mode').value!=='regex'); }
function editConnector(id){ const c=window._connectors[id]; if(!c)return;
  $('#c_id').value=c.id; $('#c_name').value=c.name||''; $('#c_method').value=c.method||'POST';
  $('#c_dialect').value=c.default_dialect||'postgres'; $('#c_url').value=c.url||'';
  $('#c_headers').value=JSON.stringify(c.headers||{},null,2); $('#c_body').value=c.body_template||'';
  $('#c_mode').value=(c.sql_extract||{}).mode||'sql_block'; $('#c_field').value=(c.sql_extract||{}).field||'';
  $('#c_pattern').value=(c.sql_extract||{}).pattern||''; $('#c_timeout').value=c.timeout||200; $('#c_attempts').value=(c.max_attempts!=null?c.max_attempts:1); $('#c_retry_delay').value=c.retry_delay||0;
  $('#c_description').value=c.description||''; $('#c_dbid').value=c.db_id||'';
  $('#c_testdialect').value=c.default_dialect||'postgres'; $('#c_testdb').value=c.db_id||'';
  fillTestQuestion(c.db_id||'');   // подставить первый вопрос бенчмарка этой БД
  $('#connTestOut').innerHTML=''; testSeq++;   // сброс тест-вывода прошлого коннектора
  $('#connFormTitle').textContent='Коннектор: '+c.name; $('#delConnBtn').style.display=''; toggleRegexRow();
  $$('.navi').find(b=>b.dataset.tab==='connectors').click(); }
function connectorFromForm(){
  let headers={}; try{ headers=JSON.parse($('#c_headers').value||'{}'); }catch(e){ throw new Error('Заголовки: невалидный JSON'); }
  return { id:$('#c_id').value||null, name:$('#c_name').value, method:$('#c_method').value, url:$('#c_url').value,
    headers, body_template:$('#c_body').value, default_dialect:$('#c_dialect').value,
    sql_extract:{mode:$('#c_mode').value, field:$('#c_field').value||undefined, pattern:$('#c_pattern').value||undefined},
    timeout:+$('#c_timeout').value, max_attempts:+$('#c_attempts').value, retry_delay:+($('#c_retry_delay').value||0), description:$('#c_description').value||'', db_id:$('#c_dbid').value||'' };
}
async function saveConnector(){ try{ const c=connectorFromForm(); if(!c.name||!c.url)return toast('Нужны название и URL');
  const saved=await api('/api/connectors',{method:'POST',body:JSON.stringify(c)}); $('#c_id').value=saved.id;
  $('#delConnBtn').style.display=''; toast('Сохранено'); loadConnectors(); _reviews=null; }catch(e){ toast(e.message); } }
async function deleteConnector(){ const id=$('#c_id').value; if(!id)return;
  if(!(await askConfirm('Удалить этот коннектор?')))return;
  await api('/api/connectors/'+id,{method:'DELETE'}); newConnector(); loadConnectors(); _reviews=null; toast('Удалено'); }
async function delConnectorById(id,name){ if(!(await askConfirm('Удалить коннектор «'+name+'»?')))return;
  await api('/api/connectors/'+id,{method:'DELETE'});
  if($('#c_id').value===id) newConnector();
  loadConnectors(); _reviews=null; toast('Удалено'); }
const SAMPLE_Q = 'How many rows are there in the main table?';
let testSeq = 0;   // bumped on connector switch — invalidates in-flight test/preview responses
const testQ = () => ($('#c_testq') && $('#c_testq').value.trim()) || SAMPLE_Q;
const testDialect = () => ($('#c_testdialect') && $('#c_testdialect').value.trim()) || $('#c_dialect').value || 'postgres';
const testDb = () => ($('#c_testdb') && $('#c_testdb').value.trim()) || $('#c_dbid').value || '';
async function fillTestQuestion(dbId){
  if($('#c_testdb')) $('#c_testdb').value = dbId || '';
  if(!dbId) return;
  try{ const r=await api('/api/first-question?db_id='+encodeURIComponent(dbId));
    if(r && r.question && $('#c_testq')) $('#c_testq').value = r.question; }
  catch(e){}
}
async function previewConnector(){ const seq=testSeq, cid=$('#c_id').value; try{ const c=connectorFromForm();
  const p=await api('/api/connectors/preview',{method:'POST',body:JSON.stringify({connector:c,question:testQ(),dialect:testDialect(),database:testDb()})});
  if(seq!==testSeq || $('#c_id').value!==cid) return;   // переключились на другой коннектор — не пишем
  $('#connTestOut').innerHTML=`<label>Превью запроса</label><div class="codeblk">${esc(p.method+' '+p.url+'\n'+JSON.stringify(p.headers)+'\n\n'+p.body)}</div>`;
  }catch(e){ if(seq===testSeq) toast(e.message); } }
function renderCurl(outEl, curl, note=''){
  const id='curl_'+Math.random().toString(16).slice(2);
  window[id]=curl||'';
  outEl.innerHTML=`<label>curl ${note?'<span class="muted">'+esc(note)+'</span>':''}</label>
    <div style="display:flex;justify-content:flex-end;margin-bottom:6px"><button class="btn sm ghost" onclick="copyText(window['${id}'])">Скопировать</button></div>
    <div class="codeblk" style="white-space:pre-wrap">${esc(curl||'')}</div>`;
}
async function curlConnectorFromForm(){ const seq=testSeq, cid=$('#c_id').value; try{ const c=connectorFromForm();
  const r=await api('/api/connectors/curl',{method:'POST',body:JSON.stringify({connector:c,question:testQ(),dialect:testDialect(),database:testDb()})});
  if(seq!==testSeq || $('#c_id').value!==cid) return;
  renderCurl($('#connTestOut'), r.curl, 'сохранённые секреты скрыты');
  }catch(e){ if(seq===testSeq) toast(e.message); } }
async function curlConnectorById(id){ const c=(window._connectors||{})[id]; if(!c)return;
  try{ const r=await api('/api/connectors/curl',{method:'POST',body:JSON.stringify({connector:c,question:($('#c_testq')&&$('#c_testq').value.trim())||SAMPLE_Q,dialect:c.default_dialect||'postgres',database:c.db_id||''})});
    editConnector(id);
    renderCurl($('#connTestOut'), r.curl, 'по сохранённому коннектору');
  }catch(e){ toast(e.message); } }
async function testConnector(){ const seq=testSeq, cid=$('#c_id').value; try{ const c=connectorFromForm();
  $('#connTestOut').innerHTML='<div class="muted">Запрос к модели…</div>';
  const r=await api('/api/connectors/test',{method:'POST',body:JSON.stringify({connector:c,question:testQ(),dialect:testDialect(),database:testDb()})});
  if(seq!==testSeq || $('#c_id').value!==cid) return;   // ответ от прошлого коннектора — игнорируем
  $('#connTestOut').innerHTML=`<label>Извлечённый SQL ${r.error?'<span class="badge l1">'+esc(r.error)+'</span>':'<span class="badge l4">ok</span>'}</label>
    ${sqlBlock(r.sql,'(нет SQL)')}
    <details style="margin-top:8px"><summary class="muted" style="cursor:pointer">сырой ответ</summary><div class="codeblk" style="max-height:420px">${esc(JSON.stringify(r.response,null,2))}</div></details>`;
  }catch(e){ if(seq===testSeq && $('#c_id').value===cid) $('#connTestOut').innerHTML='<div class="badge l1">'+esc(e.message)+'</div>'; } }

// ---------- datasets ----------
async function loadDatasets(){ const ds=await api('/api/datasets');
  window._datasets=Object.fromEntries(ds.map(d=>[d.id,d]));
  $('#dsList').innerHTML = ds.length? ds.map(d=>`<div class="list-item" onclick='editDataset(${JSON.stringify(d.id)})'><b>${esc(d.name)}</b>
    <span class="badge l3" style="margin-left:8px">${esc(d.db_type||'postgres')}</span>
    ${d.meta&&d.meta.cases_count!=null?`<span class="badge l4" title="кейсов">${esc(d.meta.cases_count)} кейсов</span>`:''}
    <span class="muted mono" style="margin-left:auto;font-size:.7rem">${esc(d.db_id)}</span>
    <button class="btn sm ghost" title="Редактировать" onclick='event.stopPropagation();editDataset(${JSON.stringify(d.id)})'>✎</button>
    <button class="btn sm ghost" title="Скачать benchmark JSONL" onclick='event.stopPropagation();downloadDataset(${JSON.stringify(d.id)})'>⬇</button>
    <button class="btn sm ghost" style="color:#dc2626" onclick='event.stopPropagation();delDataset(${JSON.stringify(d.id)})'>×</button></div>`).join('')
    : '<div class="muted">Нет датасетов — добавьте ниже.</div>'; }
function downloadDataset(id){ window.open('/api/datasets/'+encodeURIComponent(id)+'/download','_blank'); }
function currentDatasetId(){ return $('#d_id') ? $('#d_id').value : ''; }
function setDatasetEditorMode(d){
  $('#datasetFormTitle').textContent = d ? 'Редактирование датасета' : '+ добавить датасет';
  $('#downloadDsBtn').style.display = d ? '' : 'none';
  $('#deleteDsBtn').style.display = d ? '' : 'none';
  const panel=$('#datasetCasesPanel'); if(panel) panel.style.display = d ? '' : 'none';
}
function setDatasetPathView(d){
  const el=$('#d_path_view'); if(!el)return;
  const path=(d&&d.benchmark_path)||($('#d_path')&&$('#d_path').value)||'';
  if(path){
    el.innerHTML=`Файл сохранён: <span class="mono">${esc(path)}</span>`;
  }else{
    el.textContent='Файл будет сохранён автоматически в runtime-хранилище приложения (/data/datasets в Docker).';
  }
}
function newDataset(){
  ['d_id','d_name','d_path','d_dbid','d_dsn'].forEach(i=>{ const el=$('#'+i); if(el) el.value=''; });
  $('#d_dbtype').value='auto';
  const f=$('#d_file'); if(f) f.value='';
  window._datasetCases=[];
  fillDatasetCaseForm(null);
  const sel=$('#dc_case'); if(sel) sel.innerHTML='';
  const meta=$('#datasetCasesMeta'); if(meta) meta.textContent='';
  setDatasetEditorMode(null);
  setDatasetPathView(null);
  const ed=$('#datasetEditor'); if(ed) ed.open=true;
}
function editDataset(id){
  const d=(window._datasets||{})[id]; if(!d)return;
  $('#d_id').value=d.id||'';
  $('#d_name').value=d.name||'';
  $('#d_path').value=d.benchmark_path||'';
  $('#d_dbid').value=d.db_id||'';
  $('#d_dbtype').value=d.db_type||'postgres';
  $('#d_dsn').value=d.dsn||'';
  const f=$('#d_file'); if(f) f.value='';
  setDatasetEditorMode(d);
  setDatasetPathView(d);
  const ed=$('#datasetEditor'); if(ed) ed.open=true;
  loadDatasetCases(id);
}
function datasetCaseLabel(c){
  const q=(c.question||'').replace(/\s+/g,' ').trim();
  return `${c.case_id||'case'} · ${c.difficulty||'Unknown'} · ${q.slice(0,90)}${q.length>90?'…':''}`;
}
function datasetCaseConditionsText(c){
  const val=c&&c.conditions;
  if(val==null || val==='') return '';
  if(typeof val==='string') return val;
  try{ return JSON.stringify(val); }catch(e){ return String(val); }
}
function fillDatasetCaseForm(c){
  $('#dc_benchmark_id').value=c?c.benchmark_id||'':'';
  $('#dc_case_id').value=c?c.case_id||'':'';
  $('#dc_difficulty').value=c?c.difficulty||'':'';
  $('#dc_question').value=c?c.question||'':'';
  $('#dc_normal').value=c?c.normal_phrasing||'':'';
  $('#dc_conditions').value=c?datasetCaseConditionsText(c):'';
  $('#dc_gold_sql').value=c?c.gold_sql||'':'';
}
async function loadDatasetCases(id=currentDatasetId()){
  if(!id){ fillDatasetCaseForm(null); return; }
  const meta=$('#datasetCasesMeta'); if(meta) meta.textContent='загрузка вопросов...';
  try{
    const data=await api('/api/datasets/'+encodeURIComponent(id)+'/cases');
    window._datasetCases=data.cases||[];
    if(data.dataset){
      window._datasets=window._datasets||{};
      window._datasets[data.dataset.id]=data.dataset;
      $('#d_path').value=data.dataset.benchmark_path||$('#d_path').value;
      $('#d_dsn').value=data.dataset.dsn||$('#d_dsn').value;
      setDatasetPathView(data.dataset);
    }
    const sel=$('#dc_case');
    if(sel){
      sel.innerHTML=(window._datasetCases||[]).map(c=>`<option value="${esc(c.case_id)}">${esc(datasetCaseLabel(c))}</option>`).join('');
    }
    fillDatasetCaseForm((window._datasetCases||[])[0]||null);
    if(meta) meta.textContent=`${data.count||0} вопросов`;
  }catch(e){
    window._datasetCases=[];
    fillDatasetCaseForm(null);
    const sel=$('#dc_case'); if(sel) sel.innerHTML='';
    if(meta) meta.textContent='не удалось прочитать вопросы';
    toast(e.message);
  }
}
function selectDatasetCase(){
  const caseId=$('#dc_case')&&$('#dc_case').value;
  const c=(window._datasetCases||[]).find(x=>x.case_id===caseId);
  fillDatasetCaseForm(c||null);
}
function datasetCasePayload(){
  let conditions=$('#dc_conditions').value.trim();
  if(conditions && /^[\[{]/.test(conditions)){
    try{ conditions=JSON.parse(conditions); }catch(e){ throw new Error('Conditions должен быть валидным JSON или обычной строкой'); }
  }
  return {
    benchmark_id: $('#dc_benchmark_id').value.trim(),
    case_id: $('#dc_case_id').value.trim(),
    difficulty: $('#dc_difficulty').value.trim()||'Unknown',
    question: $('#dc_question').value.trim(),
    normal_phrasing: $('#dc_normal').value.trim(),
    conditions,
    gold_sql: $('#dc_gold_sql').value.trim()
  };
}
async function saveDatasetCase(){
  const id=currentDatasetId(); if(!id)return toast('Сначала выберите датасет');
  const oldCaseId=$('#dc_case')&&$('#dc_case').value;
  if(!oldCaseId)return toast('Выберите вопрос');
  let payload;
  try{ payload=datasetCasePayload(); }catch(e){ return toast(e.message); }
  if(!payload.case_id||!payload.question||!payload.gold_sql)return toast('Заполните case_id, вопрос и Gold SQL');
  const saved=await api('/api/datasets/'+encodeURIComponent(id)+'/cases/'+encodeURIComponent(oldCaseId),{method:'PUT',body:JSON.stringify(payload)});
  if(saved.dataset){
    window._datasets=window._datasets||{};
    window._datasets[saved.dataset.id]=saved.dataset;
    $('#d_path').value=saved.dataset.benchmark_path||$('#d_path').value;
    $('#d_dsn').value=saved.dataset.dsn||$('#d_dsn').value;
    setDatasetPathView(saved.dataset);
  }
  toast('Вопрос сохранён');
  await loadDatasetCases(id);
  const sel=$('#dc_case'); if(sel) sel.value=saved.case.case_id;
  fillDatasetCaseForm(saved.case);
  loadDatasets(); loadRunSelectors(); loadChatConnectors();
}
function downloadCurrentDataset(){ const id=currentDatasetId(); if(!id)return toast('Сначала выберите датасет'); downloadDataset(id); }
function normaliseDatasetDbId(value){
  return String(value||'').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'');
}
function inferDatasetDbId(stem){
  const key=normaliseDatasetDbId(stem);
  const compact=key.replace(/_/g,'');
  if(compact.includes('dmmis')) return 'dm_mis';
  if(compact.includes('sportevent')||compact.includes('sportsevent')) return 'sports_events_large';
  if(compact.includes('cybermarket')||compact.includes('cyber')) return 'cybermarket_pattern_large';
  return key;
}
function datasetDbIdGuess(fileName, datasetName){
  const byName=inferDatasetDbId(datasetName||'');
  if(byName) return byName;
  return inferDatasetDbId(String(fileName||'').replace(/\.[^.]+$/,''));
}
function fillDatasetFromFile(){
  const f=$('#d_file')&&$('#d_file').files&&$('#d_file').files[0]; if(!f)return;
  const stem=f.name.replace(/\.[^.]+$/,'');
  if(!$('#d_name').value) $('#d_name').value=stem;
  $('#d_dbid').value=datasetDbIdGuess(f.name, $('#d_name').value);
  setDatasetPathView(null);
}
async function saveDataset(){ const d={id:currentDatasetId()||null,name:$('#d_name').value,benchmark_path:$('#d_path').value,db_id:$('#d_dbid').value,dsn:$('#d_dsn').value,db_type:$('#d_dbtype').value};
  if(!d.name||!d.db_id)return toast('Заполните название и db_id');
  if(!d.id||!d.benchmark_path)return toast('Сначала загрузите JSONL-файл — путь создастся автоматически');
  try{
    const saved=await api('/api/datasets',{method:'POST',body:JSON.stringify(d)});
    $('#d_id').value=saved.id;
    $('#d_path').value=saved.benchmark_path||'';
    $('#d_dsn').value=saved.dsn||'';
    setDatasetEditorMode(saved);
    setDatasetPathView(saved);
    toast('Датасет сохранён');
    loadDatasetCases(saved.id); loadDatasets(); loadRunSelectors(); loadChatConnectors();
  }catch(e){ toast(e.message, 6000); }
}
async function uploadDatasetFile(btn){
  const f=$('#d_file')&&$('#d_file').files&&$('#d_file').files[0];
  if(!f)return toast('Выберите benchmark-файл');
  fillDatasetFromFile();
  const d={id:currentDatasetId()||null,name:$('#d_name').value, file_name:f.name, content:await f.text(),
    db_id:$('#d_dbid').value, db_type:$('#d_dbtype').value||'auto'};
  if(!d.name)return toast('Заполните название');
  const oldText=btn&&btn.textContent;
  if(btn){ btn.disabled=true; btn.textContent='Загружаю…'; }
  try{
    const saved=await api('/api/datasets/upload',{method:'POST',body:JSON.stringify(d)});
    $('#d_id').value=saved.id||'';
    $('#d_path').value=saved.benchmark_path||'';
    $('#d_dsn').value=saved.dsn||'';
    setDatasetEditorMode(saved);
    setDatasetPathView(saved);
    toast(`Датасет загружен: ${saved.cases_count||0} кейсов`);
    loadDatasetCases(saved.id); loadDatasets(); loadRunSelectors(); loadChatConnectors();
  }catch(e){ toast(e.message, 6000); }
  finally{ if(btn){ btn.disabled=false; btn.textContent=oldText||'Загрузить файл и сохранить'; } }
}
async function delDataset(id){ if(!(await askConfirm('Удалить этот датасет?')))return; await api('/api/datasets/'+id,{method:'DELETE'});
  if(currentDatasetId()===id) newDataset();
  loadDatasets(); loadRunSelectors(); loadChatConnectors(); toast('Удалено'); }
async function deleteCurrentDataset(){ const id=currentDatasetId(); if(!id)return toast('Сначала выберите датасет'); await delDataset(id); }

// ---------- connector chat ----------
let chatHistory=[];
async function loadChatConnectors(){
  const [conns, datasets]=await Promise.all([api('/api/connectors'), api('/api/datasets')]);
  window._chatConns=Object.fromEntries(conns.map(c=>[c.id,c]));
  window._chatDs=Object.fromEntries(datasets.map(d=>[d.id,d]));
  const sel=$('#chat_connector'); if(!sel)return;
  sel.innerHTML=conns.map(c=>`<option value="${c.id}">${esc(c.name)}${c.db_id?' · '+esc(c.db_id):''} · ${esc(c.default_dialect||'postgres')}</option>`).join('')||'<option value="">— нет коннекторов —</option>';
  const dsSel=$('#chat_dataset');
  if(dsSel) dsSel.innerHTML=datasets.map(d=>`<option value="${d.id}">${esc(d.name)} · ${esc(d.db_id||'')} · ${esc(d.db_type||'postgres')}</option>`).join('')||'<option value="">— нет датасетов —</option>';
  onChatConnectorChange(false);
}
function pickChatDatasetForConnector(c){
  const dsSel=$('#chat_dataset'); if(!dsSel || !c)return;
  const datasets=Object.values(window._chatDs||{});
  const match=datasets.find(d=>c.db_id && d.db_id===c.db_id) || datasets[0];
  if(match) dsSel.value=match.id;
}
async function onChatConnectorChange(fillQuestion=true){
  const c=(window._chatConns||{})[$('#chat_connector')&&$('#chat_connector').value];
  if(!c)return;
  $('#chat_dialect').value=c.default_dialect||'postgres';
  $('#chat_db').value=c.db_id||'';
  pickChatDatasetForConnector(c);
  if(fillQuestion && c.db_id && !($('#chat_question').value||'').trim()){
    try{ const r=await api('/api/first-question?db_id='+encodeURIComponent(c.db_id));
      if(r && r.question) $('#chat_question').value=r.question;
    }catch(e){}
  }
}
function renderChat(){
  const el=$('#chatLog'); if(!el)return;
  if(!chatHistory.length){ el.innerHTML='<div class="muted">Выберите коннектор и отправьте вопрос.</div>'; return; }
  el.innerHTML=chatHistory.map((item,i)=>{
    if(item.role==='user') return `<div class="chat-msg user"><div class="meta">${esc(item.connector||'')} · ${esc(item.when||'')}</div><div>${esc(item.text)}</div></div>`;
    return `<div class="chat-msg"><div class="meta">${esc(item.connector||'')} · ${item.error?'<span class="badge l1">'+esc(item.error)+'</span>':'<span class="badge l4">ok</span>'}</div>
      <label>Извлечённый SQL</label>${sqlBlock(item.sql,'(нет SQL)')}
      ${item.sql?`<div style="margin:8px 0;display:flex;gap:8px;flex-wrap:wrap"><button class="btn sm ghost" onclick="editChatSqlFromHistory(${i})">В редактор</button><button class="btn sm" onclick="runHistorySql(${i})">Запустить SQL</button></div>`:''}
      ${item.sql_result?`<label>Результат выполнения</label>${resultBlock(item.sql_result)}`:''}
      <details style="margin-top:8px"><summary class="muted" style="cursor:pointer">сырой ответ</summary><div class="codeblk" style="max-height:360px;white-space:pre-wrap">${esc(JSON.stringify(item.response,null,2))}</div></details></div>`;
  }).join('');
  el.scrollTop=el.scrollHeight;
}
async function sendChat(){
  const connector_id=$('#chat_connector').value, question=($('#chat_question').value||'').trim();
  if(!connector_id)return toast('Выберите коннектор');
  if(!question)return toast('Введите вопрос');
  const c=(window._chatConns||{})[connector_id]||{};
  chatHistory.push({role:'user', connector:c.name||connector_id, text:question, when:new Date().toLocaleTimeString('ru-RU')});
  renderChat();
  try{
    const r=await api('/api/connectors/chat',{method:'POST',body:JSON.stringify({connector_id,question,dialect:$('#chat_dialect').value||c.default_dialect||'postgres',database:$('#chat_db').value||c.db_id||'',dataset_id:$('#chat_dataset').value||null})});
    chatHistory.push({role:'assistant', connector:r.connector_name||connector_id, sql:r.sql, error:r.error, response:r.response, sql_result:r.sql_result});
    if(r.sql){ $('#chat_sql_editor').value=r.sql; renderSqlExecResult(r.sql_result); }
  }catch(e){
    chatHistory.push({role:'assistant', connector:c.name||connector_id, sql:null, error:e.message, response:null});
  }
  renderChat();
}
function clearChat(){ chatHistory=[]; renderChat(); $('#chatCurlOut').innerHTML=''; $('#chatSqlOut').innerHTML=''; }
async function curlChatConnector(){
  const connector_id=$('#chat_connector').value, c=(window._chatConns||{})[connector_id];
  if(!c)return toast('Выберите коннектор');
  const q=($('#chat_question').value||'').trim()||SAMPLE_Q;
  try{
    const r=await api('/api/connectors/curl',{method:'POST',body:JSON.stringify({connector:c,question:q,dialect:$('#chat_dialect').value||c.default_dialect||'postgres',database:$('#chat_db').value||c.db_id||''})});
    renderCurl($('#chatCurlOut'), r.curl, 'для выбранного вопроса');
  }catch(e){ toast(e.message); }
}
function renderSqlExecResult(res){
  const el=$('#chatSqlOut'); if(!el)return;
  if(!res){ el.innerHTML='<div class="muted">SQL ещё не запускался или датасет не выбран.</div>'; return; }
  const meta=`${esc(res.dataset_name||res.dataset_id||'dataset')} · ${res.elapsed_s!=null?esc(res.elapsed_s)+'s':''}`;
  el.innerHTML=`<label>Результат SQL <span class="muted">${meta}</span></label>${resultBlock(res)}`;
}
function editChatSqlFromHistory(i){
  const item=chatHistory[i]; if(!item || !item.sql)return;
  $('#chat_sql_editor').value=item.sql;
  renderSqlExecResult(item.sql_result);
}
async function runHistorySql(i){
  const item=chatHistory[i]; if(!item || !item.sql)return;
  $('#chat_sql_editor').value=item.sql;
  await runChatSql();
}
async function runChatSql(){
  const sql=($('#chat_sql_editor').value||'').trim();
  const dataset_id=$('#chat_dataset').value;
  if(!dataset_id)return toast('Выберите датасет для запуска SQL');
  if(!sql)return toast('Введите SQL');
  $('#chatSqlOut').innerHTML='<div class="muted">Выполняю SQL…</div>';
  try{
    const r=await api('/api/sql/execute',{method:'POST',body:JSON.stringify({dataset_id,sql,timeout_ms:30000})});
    renderSqlExecResult(r.result);
  }catch(e){
    $('#chatSqlOut').innerHTML='<div class="badge l1">'+esc(e.message)+'</div>';
  }
}

const normDialect = s => { s=(s||'').toLowerCase().trim(); return ['postgres','postgresql','pg'].includes(s)?'postgres':s; };
async function loadRunSelectors(){
  const [ds,conns]=await Promise.all([api('/api/datasets'),api('/api/connectors')]);
  window._runDs=Object.fromEntries(ds.map(d=>[d.id,d]));
  window._runConns=Object.fromEntries(conns.map(c=>[c.id,c]));
  $('#r_dataset').innerHTML=ds.map(d=>`<option value="${d.id}">${esc(d.name)} · ${esc(d.db_type||'postgres')}</option>`).join('')||'<option value="">— нет датасетов —</option>';
  $('#r_connector').innerHTML=conns.map(c=>`<option value="${c.id}">${esc(c.name)}${c.db_id?' · '+esc(c.db_id):''} · ${esc(c.default_dialect||'postgres')}</option>`).join('')||'<option value="">— нет коннекторов —</option>';
  const rc=$('#runConnList');
  if(rc) rc.innerHTML = conns.length ? conns.map(c=>
    `<div class="list-item" onclick='pickRunConnector(${JSON.stringify(c.id)})'><b>${esc(c.name)}</b>
     <span class="badge l3" style="margin-left:8px">${esc(c.default_dialect||'postgres')}</span>
     ${c.db_id?`<span class="badge l4" style="margin-left:4px">${esc(c.db_id)}</span>`:''}
     <span class="muted mono" style="margin-left:auto;font-size:.72rem">${esc(c.method)} ${esc((c.url||'').slice(0,24))}</span></div>`).join('')
    : '<div class="muted">Нет коннекторов — создайте на вкладке «Коннекторы».</div>';
  checkRunCompat();
}
function pickRunConnector(id){ $('#r_connector').value=id; checkRunCompat(); toast('Коннектор выбран для прогона'); }
function toggleRunJudge(){
  renderRunJudgeSummary();
}
function checkRunCompat(){
  const d=(window._runDs||{})[$('#r_dataset').value], c=(window._runConns||{})[$('#r_connector').value];
  const note=$('#runCompat'), btn=$('#runBtn'); if(!note||!btn)return true;
  if(!d||!c){ note.innerHTML=''; btn.disabled=false; return true; }
  const dt=normDialect(d.db_type||'postgres'), cd=normDialect(c.default_dialect||'postgres');
  if(dt!==cd){
    note.innerHTML=`<span style="color:#dc2626">✗ Несовместимо: коннектор «${esc(c.name)}» — диалект <b>${esc(c.default_dialect||'?')}</b>, БД датасета — <b>${esc(d.db_type||'?')}</b>. Запуск заблокирован.</span>`;
    btn.disabled=true; return false; }
  if(c.db_id && c.db_id!==d.db_id){
    note.innerHTML=`<span style="color:#dc2626">✗ Коннектор «${esc(c.name)}» привязан к БД <b>${esc(c.db_id)}</b>, а датасет — <b>${esc(d.db_id)}</b>. Запуск заблокирован.</span>`;
    btn.disabled=true; return false; }
  note.innerHTML=`<span style="color:var(--green)">✓ Совместимо: ${esc(cd)}${c.db_id?' · '+esc(c.db_id):''}</span>`; btn.disabled=false; return true;
}

// ---------- run trigger + progress ----------
async function triggerRun(){
  const dataset_id=$('#r_dataset').value, connector_id=$('#r_connector').value;
  if(!dataset_id||!connector_id)return toast('Выберите датасет и коннектор');
  if(!checkRunCompat())return toast('Диалект коннектора несовместим с типом БД датасета');
  const requestedConcurrency=Math.max(1, +($('#r_concurrency')&&$('#r_concurrency').value || 1));
  const limit=apiConcurrencyLimit();
  const concurrency=limit ? Math.min(requestedConcurrency, limit) : requestedConcurrency;
  if(limit && requestedConcurrency>limit){
    if($('#r_concurrency')) $('#r_concurrency').value=String(limit);
    toast(`Потоки ограничены env до ${limit}`);
  }
  const body={dataset_id,connector_id,concurrency};
  const ma=$('#r_attempts')&&$('#r_attempts').value; if(ma!=='' && ma!=null) body.max_attempts=Math.max(0,+ma);
  const rd=$('#r_retry_delay')&&$('#r_retry_delay').value; if(rd!=='' && rd!=null) body.retry_delay=Math.max(0,+rd);
  const ct=$('#r_case_timeout')&&$('#r_case_timeout').value; if(ct!=='' && ct!=null) body.case_timeout=Math.max(0,+ct);
  try{ const run=await api('/api/runs',{method:'POST',body:JSON.stringify(body)});
    currentRunId=run.id; runsMap[run.id]=run; progOpen.add(run.id); casesMap[run.id]=[];
    toast('Прогон запущен'); $$('.navi').find(b=>b.dataset.tab==='progress').click(); startProgress(); }
  catch(e){ toast(e.message); }
}
let progOpen=new Set(), progCaseOpen=new Set(), progGroupClosed=new Set(), progBound=false, progInit=false;
let progWS=null, runsMap={}, casesMap={}, rerunning=new Set(), rerunPendingSeen=new Set(), deletingRuns=new Set(), casesLoading=new Set();   // rerunning: "runId::caseId"; deletingRuns: runId
let runLastSeen={};   // runId -> Date последнего WS-сообщения по прогону
const STATUS_BADGE={queued:'l2',running:'l3',judging:'l3',done:'l4',error:'l1',paused:'l2',stopped:'l1'};
const ACTIVE_ST=['queued','running','paused','judging'];
const CONTROLLABLE_ST=['queued','running','paused'];
const CASE_STATUS_META={
  api_waiting:['l3','ждем ответ API',true],
  llm_queued:['l2','в очереди на LLM-оценку',false],
  awaiting_judge:['l2','ожидает LLM-оценку',false],
  sent_to_judge:['l3','отправлен на оценку',true],
  judging:['l3','оценивается LLM',true],
  judged:['l4','оценка готова',false],
  done:['l4','готово',false],
  api_error:['l1','ошибка API',false],
  api_timeout:['l1','тайм-аут API',false],
  no_sql:['l0','нет SQL от API',false],
  gold_error:['l2','ошибка gold SQL',false],
  sql_error:['l1','ошибка SQL модели',false],
  judge_error:['l1','ошибка оценки',false],
};
function inferCaseStatus(c){
  const err=(c.error||'').toLowerCase();
  const goldErr=c.gold_result&&c.gold_result.error;
  const agentErr=c.agent_result&&c.agent_result.error;
  if(c.level!=null) return 'judged';
  if(err.includes('judge') || err.includes('оцен')) return 'judge_error';
  if(goldErr) return 'gold_error';
  if(err.includes('тайм-аут') || err.includes('timeout')) return 'api_timeout';
  if(!c.predicted_sql && (c.error || c.raw_response)) return c.error ? 'api_error' : 'no_sql';
  if(agentErr) return 'sql_error';
  if(c.error) return 'api_error';
  if(c.predicted_sql || c.gold_result || c.agent_result) return 'llm_queued';
  return 'api_waiting';
}
function caseStatusBadge(c){
  const st=c.case_status || inferCaseStatus(c);
  const meta=CASE_STATUS_META[st] || ['l2', c.case_status_label||st, false];
  const spin=meta[2]?'<span class="spin">↻</span> ':'';
  return `<span class="badge ${meta[0]}">${spin}${esc(c.case_status_label||meta[1])}</span>`;
}
function rerunKey(runId, caseId){ return runId+'::'+caseId; }
function rerunStatusBadge(){ return '<span class="badge l3"><span class="spin">↻</span> перезапуск…</span>'; }
function progressCaseStatusBadge(runId, c){
  const key=rerunKey(runId, c&&c.case_id);
  if(rerunning.has(key) && !caseUpdateStillPending(c||{})) return rerunStatusBadge();
  return caseStatusBadge(c);
}
function markRerunningCase(runId, caseId){
  const key=rerunKey(runId, caseId);
  rerunning.add(key);
  rerunPendingSeen.delete(key);
}
function clearRerunningCase(runId, caseId){
  const key=rerunKey(runId, caseId);
  rerunPendingSeen.delete(key);
  return rerunning.delete(key);
}
function clearRerunningForRun(runId){
  let changed=false;
  [...rerunning].forEach(k=>{ if(k.startsWith(runId+'::')){ rerunning.delete(k); rerunPendingSeen.delete(k); changed=true; } });
  return changed;
}
function reloadOpenRunCases(runId){
  if(!progOpen.has(runId)) return;
  delete casesMap[runId];
  loadRunCases(runId);
}
function handleFinishedRun(run){
  if(!run || !run.id || ACTIVE_ST.includes(run.status)) return;
  if(clearRerunningForRun(run.id)) reloadOpenRunCases(run.id);
}
function caseUpdateStillPending(c){
  const st=(c&&c.case_status) || inferCaseStatus(c||{});
  return ['api_waiting','llm_queued','awaiting_judge','sent_to_judge','judging'].includes(st);
}
function mergeProgressCase(runId, c){
  if(!runId || !c) return;
  const list=casesMap[runId] || (casesMap[runId]=[]);
  const key=c.idx!=null ? 'idx' : 'case_id';
  const i=list.findIndex(x=>x[key]===c[key]);
  if(i>=0) list[i]={...list[i], ...c}; else list.push(c);
}
function handleProgressCaseUpdate(runId, c){
  mergeProgressCase(runId, c);
  if(!runId || !c || !c.case_id) return;
  const key=rerunKey(runId, c.case_id);
  if(!rerunning.has(key)) return;
  if(caseUpdateStillPending(c)) rerunPendingSeen.add(key);
  else if(rerunPendingSeen.has(key)) clearRerunningCase(runId, c.case_id);
}
function handleProgressMessage(m){
  if(!m || m.type==='ping') return;
  if(m.type==='snapshot'){
    runsMap={};
    (m.runs||[]).forEach(r=>{
      runsMap[r.id]=r;
      runLastSeen[r.id]=new Date();
      handleFinishedRun(r);
    });
    (m.cases||[]).forEach(item=>handleProgressCaseUpdate(item.run_id, item.case||{}));
  }
  else if(m.type==='run'){
    runsMap[m.run.id]=m.run;
    runLastSeen[m.run.id]=new Date();
    handleFinishedRun(m.run);
  }
  else if(m.type==='case'){
    handleProgressCaseUpdate(m.run_id, m.case||{});
    runLastSeen[m.run_id]=new Date();
  }
  renderProgress();
  renderCaseStatusModal();
}
let caseStatusState=null;
function caseStatusVal(v, fallback='—'){
  return (v===null || v===undefined || v==='') ? fallback : esc(v);
}
function findCaseInRun(runId, caseId){
  return (casesMap[runId]||[]).find(c=>c.case_id===caseId);
}
function statusItem(label, value, cls=''){
  return `<div class="status-item"><div class="lab">${esc(label)}</div><div class="val ${cls}">${value}</div></div>`;
}
function renderCaseStatusModal(){
  if(!caseStatusState || !$('#caseStatusBody')) return;
  const {runId, caseId}=caseStatusState;
  const run=runsMap[runId]||{};
  const c=findCaseInRun(runId, caseId)||{};
  const s=run.summary||{};
  const a=c.assessment||{};
  const agentErr=c.agent_result&&c.agent_result.error;
  const goldErr=c.gold_result&&c.gold_result.error;
  const rawJudge=a.raw_response ? `<details style="margin-top:10px"><summary class="muted" style="cursor:pointer">сырой ответ LLM-оценки</summary><div class="codeblk" style="max-height:260px;white-space:pre-wrap">${esc(a.raw_response)}</div></details>` : '';
  $('#caseStatusBody').innerHTML=`
    <div class="kv"><b>Run</b> <span class="mono">${esc(runId)}</span> · <span class="badge ${STATUS_BADGE[run.status]||'l2'}">${esc(run.status||'—')}</span></div>
    <div class="kv"><b>Вопрос</b> <span class="mono">${esc(caseId)}</span> ${c.idx?`#${esc(c.idx)}`:''}</div>
    <div class="status-grid">
      ${statusItem('Статус кейса', progressCaseStatusBadge(runId, c))}
      ${statusItem('Прогресс run', `${caseStatusVal(run.done_cases, '0')}/${caseStatusVal(run.total_cases, '0')} · judged ${caseStatusVal(s.judged)} · queued ${caseStatusVal(s.llm_queued)} · in LLM ${caseStatusVal(s.llm_in_progress)}`)}
      ${statusItem('API attempts', caseStatusVal(c.attempts))}
      ${statusItem('API/SQL error', caseStatusVal(c.error || agentErr || goldErr))}
      ${statusItem('LLM attempts', caseStatusVal(a.attempts))}
      ${statusItem('LLM category', caseStatusVal(a.error_category))}
      ${statusItem('LLM confidence', caseStatusVal(a.confidence))}
      ${statusItem('LLM repair', a.repair_attempted===true?'да':(a.repair_attempted===false?'нет':'—'))}
      ${statusItem('Raw level', caseStatusVal(a.raw_level))}
      ${statusItem('Validation error', caseStatusVal(a.validation_error))}
    </div>
    ${c.reason?`<div class="kv"><b>Причина LLM</b> <span>${esc(c.reason)}</span></div>`:''}
    ${a.reason && a.reason!==c.reason?`<div class="kv"><b>Assessment reason</b> <span>${esc(a.reason)}</span></div>`:''}
    ${rawJudge}
  `;
}
function showCaseStatus(runId, caseId){
  caseStatusState={runId, caseId};
  $('#caseStatusModal').classList.remove('hide');
  renderCaseStatusModal();
}
function closeCaseStatus(){
  caseStatusState=null;
  $('#caseStatusModal').classList.add('hide');
}
async function startProgress(){
  try{ const runs=await api('/api/runs'); const m={}; runs.forEach(r=>m[r.id]=r); runsMap=m; }   // reconcile: drop ghosts of deleted/orphaned runs
  catch(e){}
  renderProgress(); connectProgressWS();
}
let wsLast=null;
function updateWsStatus(connected){
  const el=$('#wsStatus'); if(!el)return;
  el.classList.toggle('on',connected); el.classList.toggle('off',!connected);
  const t=wsLast?wsLast.toLocaleString('ru-RU'):'—';
  $('#wsLabel').textContent = connected ? `WebSocket подключён · обновлено ${t}` : `WebSocket отключён · посл. ${t}`;
}
function connectProgressWS(){
  if(progWS && (progWS.readyState===WebSocket.CONNECTING || progWS.readyState===WebSocket.OPEN)) return;
  const proto = location.protocol==='https:'?'wss':'ws';
  progWS = new WebSocket(proto+'://'+location.host+'/ws/progress');
  progWS.onopen = ()=>updateWsStatus(true);
  progWS.onmessage = ev=>{ let m; try{ m=JSON.parse(ev.data); }catch(e){ return; }
    wsLast=new Date(); updateWsStatus(true);
    handleProgressMessage(m);
  };
  progWS.onclose = ()=>{ progWS=null; updateWsStatus(false); setTimeout(connectProgressWS, 3000); };  // auto-reconnect (still push, not poll)
  progWS.onerror = ()=>{ try{ progWS.close(); }catch(e){} };
}
function bindProgress(){ if(progBound)return; progBound=true;
  $('#progList').addEventListener('click', e=>{
    const pr=e.target.closest('[data-pause-run]'), rsm=e.target.closest('[data-resume-run]'), sp=e.target.closest('[data-stop-run]');
    if(pr){ e.stopPropagation(); pauseRun(pr.dataset.pauseRun); return; }
    if(rsm){ e.stopPropagation(); resumeRun(rsm.dataset.resumeRun); return; }
    if(sp){ e.stopPropagation(); stopRun(sp.dataset.stopRun); return; }
    const dl=e.target.closest('[data-del-run]');
    if(dl){ e.stopPropagation(); delRun(dl.dataset.delRun); return; }
    const rp=e.target.closest('[data-repeat-run]');
    if(rp){ e.stopPropagation(); repeatRun(rp.dataset.repeatRun); return; }
    const log=e.target.closest('[data-log-run]');
    if(log){ e.stopPropagation(); window.open('/api/runs/'+encodeURIComponent(log.dataset.logRun)+'/logs/download','_blank'); return; }
    const cont=e.target.closest('[data-continue-run]');
    if(cont){ e.stopPropagation(); continueRun(cont.dataset.continueRun); return; }
    const rf=e.target.closest('[data-rerun-run]'); const rc=e.target.closest('[data-rc-case]');
    const rj=e.target.closest('[data-rejudge-case]');
    const cs=e.target.closest('[data-case-status]');
    if(rf){ e.stopPropagation(); rerunFailed(rf.dataset.rerunRun); return; }
    if(rc){ e.stopPropagation(); rerunCase(rc.dataset.rcRun, rc.dataset.rcCase); return; }
    if(rj){ e.stopPropagation(); rejudgeCase(rj.dataset.rejudgeRun, rj.dataset.rejudgeCase); return; }
    if(cs){ e.stopPropagation(); showCaseStatus(cs.dataset.caseStatusRun, cs.dataset.caseStatus); return; }
    const gt=e.target.closest('[data-group]'); const rt=e.target.closest('[data-run]'); const ct=e.target.closest('[data-case]');
    if(gt){ const k=gt.dataset.group; progGroupClosed.has(k)?progGroupClosed.delete(k):progGroupClosed.add(k); renderProgress(); return; }
    if(ct){ e.stopPropagation(); const k=ct.dataset.case; progCaseOpen.has(k)?progCaseOpen.delete(k):progCaseOpen.add(k); renderProgress(); return; }
    if(rt){ const id=rt.dataset.run; if(progOpen.has(id))progOpen.delete(id); else { progOpen.add(id); loadRunCases(id); } renderProgress(); }
  });
}
function ensureRunCases(id){
  if(casesMap[id]!==undefined || casesLoading.has(id)) return;
  casesLoading.add(id);
  loadRunCases(id);
}
async function loadRunCases(id){ try{ const d=await api('/api/runs/'+id); const cur=casesMap[id]||[];
    const byIdx=new Map(cur.map(c=>[c.idx,c])); (d.cases||[]).forEach(c=>byIdx.set(c.idx, {...(byIdx.get(c.idx)||{}), ...c}));
    casesMap[id]=[...byIdx.values()].sort((a,b)=>(a.idx||0)-(b.idx||0)); renderProgress(); }catch(e){} finally{ casesLoading.delete(id); } }
async function pauseRun(id){ if(!(await askConfirm('Поставить прогон на паузу?')))return;
  try{ await api('/api/runs/'+id+'/pause',{method:'POST'}); toast('Ставлю на паузу…'); }catch(e){ toast(e.message); } }
async function resumeRun(id){ try{ await api('/api/runs/'+id+'/resume',{method:'POST'}); toast('Продолжаю'); }catch(e){ toast(e.message); } }
async function stopRun(id){ if(!(await askConfirm('Остановить прогон? Уже выполненные кейсы сохранятся.')))return;
  try{ const r=await api('/api/runs/'+id+'/stop',{method:'POST'});
    if(r.run) runsMap[id]=r.run; renderProgress(); toast('Прогон остановлен'); }
  catch(e){ toast(e.message); } }
async function continueRun(id){ try{ const r=await api('/api/runs/'+id+'/rerun',{method:'POST'});
  progOpen.add(id); delete casesMap[id]; loadRunCases(id); renderProgress();
  toast(`Продолжаю — дозапуск ${r.targets||0} кейсов`); }
  catch(e){ toast(e.message); } }
async function repeatRun(id){ try{ const run=await api('/api/runs/'+id+'/repeat',{method:'POST'});
  runsMap[run.id]=run; casesMap[run.id]=[]; progOpen.add(run.id); renderProgress(); toast('Повтор запущен'); }
  catch(e){ toast(e.message); } }
async function delRun(id){ if(!(await askConfirm('Удалить этот прогон? Его результаты и JSON будут удалены.')))return;
  deletingRuns.add(id); renderProgress();                         // показать «удаление в процессе…»
  try{ await api('/api/runs/'+id,{method:'DELETE'}); delete runsMap[id]; delete casesMap[id]; toast('Прогон удалён'); }
  catch(e){ toast(e.message); }
  finally{ deletingRuns.delete(id); renderProgress(); } }
async function rerunFailed(runId){
  try{ const r=await api('/api/runs/'+runId+'/rerun',{method:'POST'});
    const targets=Number(r.targets||0);
    if(r.status==='queued' || r.status==='rerunning'){
      runsMap[runId]={...(runsMap[runId]||{}), id:runId, status:r.status==='queued'?'queued':'running', error:null, finished_at:null};
      runLastSeen[runId]=new Date();
    }
    clearRerunningForRun(runId);
    if(targets>0 && r.status!=='no_targets'){
      (casesMap[runId]||[]).forEach(c=>{ if(caseNeedsRerun(c)) markRerunningCase(runId, c.case_id); });
    } else {
      reloadOpenRunCases(runId);
    }
    progOpen.add(runId); renderProgress();
    if(targets>0) toast(r.status==='queued' ? `Перепрогон поставлен в очередь: ${targets}` : `Перепрогон неуспешных: ${targets}`);
    else toast('Нет неуспешных вопросов для перепрогона'); }
  catch(e){ toast(e.message); }
}
async function rerunCase(runId, caseId){
  try{ const r=await api('/api/runs/'+runId+'/rerun-case',{method:'POST',body:JSON.stringify({case_id:caseId})});
    if(r.status==='queued' || r.status==='rerunning_api'){
      runsMap[runId]={...(runsMap[runId]||{}), id:runId, status:r.status==='queued'?'queued':'running', error:null, finished_at:null};
      runLastSeen[runId]=new Date();
    }
    markRerunningCase(runId, caseId); progOpen.add(runId); renderProgress();
    toast((r.status==='queued'?'API-запрос поставлен в очередь: ':'API-запрос перезапущен: ')+caseId); }
  catch(e){ toast(e.message); }
}
async function rejudgeCase(runId, caseId){
  try{ await api('/api/runs/'+runId+'/judge-case',{method:'POST',body:JSON.stringify({case_id:caseId})});
    progOpen.add(runId); renderProgress();
    toast('LLM-оценка перезапущена: '+caseId); }
  catch(e){ toast(e.message); }
}
function renderProgress(){
  bindProgress();
  const runs=Object.values(runsMap).sort((a,b)=>(b.created_at||0)-(a.created_at||0));
  if(!runs.length){ $('#progList').innerHTML='<div class="muted">Прогонов ещё нет. Запустите на вкладке «Запуск».</div>'; return; }
  // auto-open the active (or newest) run ONCE, on first paint — never re-open afterwards
  if(!progInit){ progInit=true; const act=runs.find(r=>ACTIVE_ST.includes(r.status))||runs[0]; if(act){ progOpen.add(act.id); loadRunCases(act.id); } }
  const active=runs.filter(r=>ACTIVE_ST.includes(r.status));
  const finished=runs.filter(r=>!ACTIVE_ST.includes(r.status));
  const group=(key,title,arr)=>{ if(!arr.length) return '';
    const closed=progGroupClosed.has(key);
    return `<div class="prog-group-title" data-group="${key}" style="cursor:pointer">`
      +`<span class="muted" style="display:inline-block;width:14px">${closed?'▸':'▾'}</span>${title} <span class="muted">· ${arr.length}</span></div>`
      +(closed?'':arr.map(progCard).join('')); };
  $('#progList').innerHTML=(group('active','⏳ Ещё выполняется', active)+group('finished','✅ Завершено', finished))
    || '<div class="muted">Прогонов ещё нет.</div>';
}
function progCard(r){
  const open=progOpen.has(r.id), s=r.summary||{};
  const total=r.total_cases||0;
  const rerunN=[...rerunning].filter(k=>k.startsWith(r.id+'::')).length;   // вопросы в статусе «перезапуск»
  const done=Math.max(0,(r.done_cases||0)-rerunN), pct=total?Math.round(done/total*100):0;  // их не считаем пройденными
  const dt=r.created_at?new Date(r.created_at*1000).toLocaleString('ru-RU'):'';
  let body='';
  if(open){
    ensureRunCases(r.id);
    const cases=casesMap[r.id]||[];
    const failed=cases.filter(caseNeedsRerun).length;
    const busy=ACTIVE_ST.includes(r.status);
    body=`<div class="prog-body">
      <div class="kv" style="margin-bottom:6px"><b>Прогресс</b> ${done}/${total} (${pct}%) · <span class="mono muted">${r.id}</span></div>
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div style="margin:10px 0">${['L4','L3','L2','L1','L0'].map(l=>`<span class="pill">${l}: <b>${s[l]||0}</b></span>`).join('')}
        <span class="pill">accuracy: <b>${s.accuracy!=null?s.accuracy+'%':'—'}</b></span>
        ${busy?'':`<button class="btn sm" data-rerun-run="${esc(r.id)}" style="margin-left:8px" title="перепрогнать вопросы без ответа, с ошибкой или уровнем ниже L4">↻ перепрогнать неуспешные${failed?` (${failed})`:''}</button>`}</div>
      ${r.error?`<div class="badge l1">${esc(r.error)}</div>`:''}
      <table><thead><tr><th>#</th><th>Кейс</th><th>Сложн.</th><th>Статус</th><th>Ур.</th><th>Время</th><th></th></tr></thead><tbody>
      ${cases.map(c=>progCaseRow(r.id,c,busy)).join('')||`<tr><td colspan="7" class="muted">${casesLoading.has(r.id)?'Загружаю кейсы…':'Кейсов пока нет…'}</td></tr>`}
      </tbody></table></div>`;
  }
  const deleting = deletingRuns.has(r.id);
  const ctrls = !CONTROLLABLE_ST.includes(r.status) ? '' :
    (r.status==='paused'
       ? `<button class="btn sm ghost" data-resume-run="${esc(r.id)}" title="продолжить">▶ Продолжить</button>`
       : `<button class="btn sm ghost" data-pause-run="${esc(r.id)}" title="пауза">⏸ Пауза</button>`)
     + `<button class="btn sm ghost" data-stop-run="${esc(r.id)}" style="color:#dc2626" title="остановить">⏹ Остановить</button>`;
  const delBtn = `<button class="btn sm ghost" data-del-run="${esc(r.id)}" style="color:#dc2626" title="удалить прогон">🗑 Удалить</button>`;
  const logBtn = `<button class="btn sm ghost" data-log-run="${esc(r.id)}" title="скачать JSONL-лог этого запуска">Лог</button>`;
  const repeatBtn = (!ACTIVE_ST.includes(r.status)) ? `<button class="btn sm ghost" data-repeat-run="${esc(r.id)}" title="новый прогон с теми же параметрами">↺ Повторить</button>` : '';
  const contBtn = (r.status==='stopped'||r.status==='error') ? `<button class="btn sm" data-continue-run="${esc(r.id)}" title="дозапустить недоделанные кейсы (готовые сохранятся)">▶ Продолжить</button>` : '';
  const seen=runLastSeen[r.id];
  const upd=(ACTIVE_ST.includes(r.status)&&seen)?` · обновлено ${seen.toLocaleTimeString('ru-RU')}`:'';
  const right = deleting
    ? `<span class="badge l1"><span class="spin">↻</span> удаление в процессе…</span>`
    : `${ctrls}${contBtn}${repeatBtn}${logBtn}${delBtn}<span class="muted">${done}/${total} · acc ${s.accuracy!=null?s.accuracy+'%':'—'} · старт ${dt}${upd}</span>`;
  return `<div class="card prog-card"${deleting?' style="opacity:.55"':''}>
    <div class="prog-head" data-run="${esc(r.id)}" style="cursor:pointer;display:flex;align-items:center;gap:10px">
      <span class="muted" style="width:14px">${open?'▾':'▸'}</span>
      <span class="badge ${STATUS_BADGE[r.status]||'l2'}">${r.status}</span>
      <b>${esc(r.dataset_name)}</b> <span class="muted">← ${esc(r.connector_name)}</span>
      <span style="margin-left:auto;display:flex;align-items:center;gap:6px">${right}</span>
    </div>${body}</div>`;
}
function caseNeedsRerun(c){
  if(!c) return false;
  if(c.error || !c.predicted_sql) return true;
  const level=c.level;
  if(level===null || level===undefined || level==='') return true;
  const n=Number(level);
  if(!Number.isFinite(n)) return true;
  return n!==4;
}
function progCaseRow(runId, c, busy){
  const k=runId+'::'+c.idx, open=progCaseOpen.has(k);
  const isRerun = rerunning.has(rerunKey(runId, c.case_id));
  const rerunBtn = (busy && !isRerun) ? '' : `<button class="btn sm ghost" data-rc-run="${esc(runId)}" data-rc-case="${esc(c.case_id)}" title="перепрогнать этот вопрос" ${isRerun?'disabled':''}>↻</button>`;
  const lvlCell = isRerun ? rerunStatusBadge() : gradeBadge(c);
  const main=`<tr data-case="${esc(k)}" style="cursor:pointer${isRerun?';opacity:.7':''}"><td>${c.idx}</td>
    <td class="mono">${open?'▾ ':''}${esc(c.case_id)}</td><td>${esc(c.difficulty||'')}</td>
    <td>${progressCaseStatusBadge(runId, c)}</td><td>${lvlCell}</td><td class="muted">${c.elapsed_s!=null?c.elapsed_s+'s':''}</td><td>${rerunBtn}</td></tr>`;
  if(!open) return main;
  const detail=`<tr class="detail-row"><td colspan="7">
    <div style="margin-bottom:8px"><b>Вопрос:</b> ${esc(c.question||'')}</div>
    <div class="kv" style="margin-bottom:6px"><b>Статус:</b> ${progressCaseStatusBadge(runId, c)}</div>
    <div class="case-actions">
      <button class="btn sm case-action api" data-rc-run="${esc(runId)}" data-rc-case="${esc(c.case_id)}" title="Заново отправить вопрос в API коннектора">↻ Повторить API-запрос</button>
      <button class="btn sm case-action judge" data-rejudge-run="${esc(runId)}" data-rejudge-case="${esc(c.case_id)}" title="Заново отправить ответ на LLM-оценку">⚖ Повторить LLM-оценку</button>
    </div>
    ${c.reason?`<div class="kv" style="margin-bottom:6px"><b>Авто-оценка (ЛЛМ):</b> <span class="muted">${esc(c.reason)}</span></div>`:''}
    ${gradeControl(runId, c.case_id, c)}
    ${c.error?`<div class="badge l1" style="margin-bottom:8px">${esc(c.error)}</div>`:''}
    <div class="grid2">
      <div><label>SQL модели — как отправлен в БД</label>${sqlBlock(c.predicted_sql,'(нет SQL)')}${resultBlock(c.agent_result)}</div>
      <div><label>✅ Gold SQL — как отправлен в БД</label>${sqlBlock(c.gold_sql,'')}${resultBlock(c.gold_result)}</div>
    </div>
    ${c.raw_response?`<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">🛈 сырой ответ API</summary><div class="codeblk" style="max-height:420px;white-space:pre-wrap;overflow:auto">${esc(c.raw_response)}</div></details>`:''}
    </td></tr>`;
  return main+detail;
}

// ---------- results ----------
async function loadResultDatasets(){ const ds=await api('/api/datasets');
  $('#res_dataset').innerHTML=ds.map(d=>`<option value="${d.id}">${esc(d.name)}</option>`).join('');
  if(ds.length) loadResults(); else $('#resOut').innerHTML='<div class="muted">Нет датасетов.</div>'; }
async function loadResults(runId){
  const ds=$('#res_dataset').value; if(!ds)return;
  let data; try{ data=await api('/api/results?dataset_id='+ds+(runId?'&run_id='+runId:'')); }
  catch(e){ $('#resOut').innerHTML='<div class="muted">Нет прогонов для этого датасета.</div>'; $('#res_revision').innerHTML=''; return; }
  // revisions dropdown
  $('#res_revision').innerHTML=data.revisions.map(r=>{
    const dt=new Date(r.created_at*1000).toLocaleString('ru-RU');
    return `<option value="${r.id}" ${r.id===data.run.id?'selected':''}>${dt} · ${esc(r.connector_name)} · ${r.accuracy!=null?r.accuracy+'%':r.status}</option>`;
  }).join('');
  const run=data.run, s=run.summary||{}, cases=data.cases||[];
  const dt=new Date(run.created_at*1000).toLocaleString('ru-RU');
  $('#resOut').innerHTML=`
    <div class="kv"><b>Прогон</b> <span class="mono">${run.id}</span> · ${dt} · <span class="badge ${ {done:'l4',error:'l1',running:'l3'}[run.status]||'l2'}">${run.status}</span></div>
    <div class="kv"><b>Модель</b> ${esc(run.connector_name)}</div>
    <div style="margin:10px 0">
      <span class="pill">accuracy <b>${s.accuracy!=null?s.accuracy+'%':'—'}</b></span>
      <span class="pill">passed <b>${s.passed||0}/${s.total||0}</b></span>
      ${['L4','L3','L2','L1','L0'].map(l=>`<span class="pill">${l}: <b>${s[l]||0}</b></span>`).join('')}
      <button class="btn sm" style="margin-left:8px" onclick="window.open('/api/runs/${run.id}/download')">⬇ JSON</button>
    </div>
    <table><thead><tr><th>#</th><th>Кейс</th><th>Сложн.</th><th>Уровень</th><th>Вопрос</th><th>Время</th></tr></thead>
    <tbody>${cases.map(c=>`<tr onclick="this.nextElementSibling.classList.toggle('hide')">
      <td>${c.idx}</td><td class="mono">${esc(c.case_id)}</td><td>${esc(c.difficulty)}</td><td>${gradeBadge(c)}</td>
      <td>${esc((c.question||'').slice(0,70))}</td><td class="muted">${c.elapsed_s}s</td></tr>
      <tr class="hide"><td colspan="6">
        ${gradeControl(run.id, c.case_id, c)}
        <div class="grid2">
        <div><label>SQL модели — как отправлен в БД</label>${sqlBlock(c.predicted_sql,'(нет)')}${resultBlock(c.agent_result)}</div>
        <div><label>Gold SQL — как отправлен в БД</label>${sqlBlock(c.gold_sql,'')}${resultBlock(c.gold_result)}</div></div>
        ${c.error?`<div class="badge l1" style="margin-top:8px">${esc(c.error)}</div>`:''}
        ${c.raw_response?`<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">🛈 сырой ответ API</summary><div class="codeblk" style="max-height:420px;white-space:pre-wrap;overflow:auto">${esc(c.raw_response)}</div></details>`:''}</td></tr>`).join('')}</tbody></table>`;
}

applyTheme(document.documentElement.getAttribute('data-theme')||'light');  // sync button label + chart colors
if($('#caseStatusClose')) $('#caseStatusClose').onclick=closeCaseStatus;
if($('#caseStatusModal')) $('#caseStatusModal').onclick=e=>{ if(e.target.id==='caseStatusModal') closeCaseStatus(); };
connectProgressWS();   // live status badge works on every tab
(function initTab(){
  const routeTab = location.pathname.replace(/^\/+|\/+$/g, '');
  if(routeTab === 'chat'){
    const chatBtn = $$('.navi').find(b=>b.dataset.tab==='chat');
    if(chatBtn){ chatBtn.click(); loadSettings(); return; }
  }
  let saved; try{ saved=localStorage.getItem('benchTab'); }catch(e){}
  const btn = saved && $$('.navi').find(b=>b.dataset.tab===saved);
  if(btn) btn.click(); else loadLeaderboard();
  loadSettings();
})();
