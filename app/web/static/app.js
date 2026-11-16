// 缠论交易系统前端
const $ = id => document.getElementById(id);
const fmtT = ts => new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
const fmtP = p => p == null ? '–' : (p >= 100 ? p.toLocaleString('en', {maximumFractionDigits: 2}) : Number(p.toPrecision(6)));

// 用户录入的文本(备注/标题)进 innerHTML 前先转义
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
// 放进 onclick="fn('…')" 这种 JS 字符串里的币种名：只留安全字符
const symSafe = s => String(s ?? '').replace(/[^\p{L}\p{N}_.-]/gu, '');

function toast(msg) {
  const d = document.createElement('div');
  d.textContent = msg;
  $('toast').appendChild(d);
  setTimeout(() => d.remove(), 6000);
}

// ---------- 登录态 ----------
// 页面免登录可看；控制类功能(实盘持仓/自动下单/参数/关注增删/大盘观点保存/预演/回测)只在登录后出现。
// 匿名时根本不去请求需要登录的接口；任何接口 401 只让对应面板降级，绝不跳登录页。
let ME = {logged_in: false, user: null};
class ApiError extends Error {
  constructor(status, body) { super('HTTP ' + status); this.status = status; this.body = body; }
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  const isAction = !!(opts && opts.method && opts.method !== 'GET');
  let body = null;
  try { body = await r.json(); } catch (e) {}
  if (r.status === 401) { onSessionLost(isAction); throw new ApiError(401, body); }
  if (r.status === 403 || r.status === 429 || r.status >= 500 || body === null) {
    if (isAction) toast('❌ ' + ((body && body.error) || '请求失败 ' + r.status));
    throw new ApiError(r.status, body);
  }
  return body;      // 其余(含 400/409 的 {error})交给调用方自己提示
}
function onSessionLost(isAction) {
  if (ME.logged_in) {                       // 会话中途失效：收起控制区，留在当前页
    ME = {logged_in: false, user: null};
    applyAuthUI();
    run(loadWatch);                         // 重画关注列表(去掉 ✕)
    toast('🔒 登录已失效，请重新登录');
  } else if (isAction) {
    toast('🔒 请先登录');
  }
}
function needLogin() {
  if (ME.logged_in) return true;
  toast('🔒 请先登录');
  return false;
}
function applyAuthUI() {
  document.body.classList.remove('auth-pending');
  document.body.classList.toggle('authed', !!ME.logged_in);
  const link = $('login-link');
  if (link) link.href = '/login.html?next=' + encodeURIComponent(location.pathname + location.search);
}
// 面板级容错：某个接口失败(401/429/网络)只影响它自己那块；被限流(429)稍后自动重试一次
function run(fn, retried) {
  return Promise.resolve().then(fn).catch(e => {
    if (e instanceof ApiError) {
      if (e.status === 429 && !retried) setTimeout(() => run(fn, true), 3000 + Math.random() * 2000);
    } else console.warn(fn.name, e);
  });
}

async function logout() {
  try { await fetch('/api/logout', {method: 'POST'}); } catch (e) {}
  location.reload();                        // 退出后留在本页，变回只读视图
}

// ---------- 状态与统计 ----------
const TRACK_NAMES = {
  buy1: '🧪模拟·一买',
  buy2: '🧪模拟·二买',
  second_buy: '🟢 二买',
  second_sell: '🔴 二卖',
};
async function loadStatus() {
  const s = await api('/api/status');
  if (s.error) { $('stat-cards').innerHTML = '<div class="card"><h3>引擎</h3><div class="big red">未运行</div></div>'; return; }
  $('st-mode').textContent = s.mode === 'paper' ? '🧪 PAPER' : '🔥 LIVE';
  $('st-symbols').textContent = `${s.symbols} 币种`;
  $('st-ws').textContent = s.ws_conns > 0 ? `WS ✓ (${s.ws_last_msg_age_s ?? '?'}s前)` : 'WS ✗';
  const h = Math.floor(s.uptime_s / 3600), m = Math.floor(s.uptime_s % 3600 / 60);
  $('st-uptime').textContent = `运行 ${h}h${m}m`;
  let cards = `<div class="card"><h3>引擎</h3>
    <div class="big ${s.ws_conns > 0 ? '' : 'red'}">${s.ws_conns > 0 ? '正常' : '异常'}</div>
    <div class="sub">评估 ${s.last_eval_ms}ms · 累计破位 ${(s.funnel || {}).breakdown || 0} 次</div></div>`;
  for (const [key, t] of Object.entries(s.tracks || {})) {
    const cls = t.total_pnl > 0 ? 'green' : t.total_pnl < 0 ? 'red' : '';
    cards += `<div class="card"><h3>${TRACK_NAMES[key] || key}</h3>
      <div class="big ${cls}">${t.total_pnl >= 0 ? '+' : ''}${t.total_pnl} U</div>
      <div class="sub">${t.closed}平 / ${t.open}持 · 胜率${t.win_rate}% · 期望${t.expectancy_r}R</div></div>`;
  }
  $('stat-cards').innerHTML = cards;
  // 逼空候选
  const sq = s.squeeze || [];
  $('squeeze-chips').innerHTML = sq.length ? sq.map(c => `
    <span class="pill ${c.strong ? 'gold' : ''}" style="cursor:pointer" onclick="openChart('${c.symbol}','15m')"
      title="OI ${c.oi_change_pct}% · 费率${((c.funding||0)*100).toFixed(3)}% · 价格位${Math.round(c.pos*100)}%">
      ${c.strong ? '🔥' : '⚠'} ${c.symbol} OI${c.oi_change_pct >= 0 ? '+' : ''}${c.oi_change_pct}%
    </span>`).join('') : '<span class="muted">暂无（行情平静时正常）</span>';
}

// ---------- 信号 ----------
const TYPE_TAG = {buy1: '✅一买', buy2: '🔁二买', second_buy: '🟢二买', second_sell: '🔴二卖', chan: '分型'};
// 生命周期(方案A): try=试买/试卖, ok=一买/一卖(确认), fail=一买✗(失败)
function typeLabel(type, dir, state) {
  if (state === 'try') return dir === 'short' ? '试卖' : '试买';
  if (type === 'second_sell') return state === 'fail' ? '二卖✗' : '二卖';
  if (type === 'second_buy') return state === 'fail' ? '二买✗' : '二买';
  const base = dir === 'short'
    ? (type === 'buy1' ? '一卖' : type === 'buy2' ? '二卖' : (TYPE_TAG[type] || type))
    : (type === 'buy1' ? '一买' : type === 'buy2' ? '二买' : (TYPE_TAG[type] || type));
  return state === 'fail' ? base + '✗' : base;
}
// 试=灰(未定) 成=金(成立) 败=红(失败), 给买卖点tag上色做区别标记
function stateClass(state) {
  return state === 'fail' ? 'short' : state === 'ok' ? 'primary' : 'secondary';
}
// 综合显示名: 威科夫买卖点单独命名 + 双信号重叠⭐徽章
function dispType(type, dir, state, extraStr) {
  let ex = {}; try { ex = JSON.parse(extraStr || '{}'); } catch (e) {}
  const dual = ex.dual ? ' ⭐双' : '';
  if (ex.path === '趋势反转') {
    return `🔄趋势反转(${state === 'fail' ? '败' : state === 'ok' ? '成' : '试'})`;
  }
  if (ex.path === '头肩顶') {
    return `🚩头肩顶(${state === 'fail' ? '败' : '试'})`;
  }
  if (ex.path === '威科夫') {
    const base = dir === 'short' ? '威科夫卖' : '威科夫买';
    if (state === 'try') return (dir === 'short' ? '试·威科夫卖' : '试·威科夫买') + dual;
    return (state === 'fail' ? base + '✗' : base) + dual;
  }
  return typeLabel(type, dir, state) + dual;
}
let signalCache = {};
let deepLinkedSignalOpened = false;
function sigType(s) {
  try { return JSON.parse(s.extra || '{}').type || ''; } catch (e) { return ''; }
}
function sigScore(s) {
  try { return JSON.parse(s.extra || '{}').score ?? ''; } catch (e) { return ''; }
}
async function loadSignals() {
  const level = $('f-level').value, dir = $('f-dir').value;
  let rows = await api('/api/signals?limit=200');
  if (level) rows = rows.filter(s => s.tf === level);
  if (dir) rows = rows.filter(s => s.direction === dir);
  rows = rows.slice(0, 100);
  signalCache = {};
  rows.forEach(s => signalCache[s.id] = s);
  $('t-signals').innerHTML = rows.map(s => `
    <tr class="clickable" onclick="openChartFromSignal(${s.id})">
      <td>${s.id}</td><td>${fmtT(s.created_at)}</td>
      <td><b>${s.symbol}</b></td><td>${s.tf}</td>
      <td><span class="tag ${s.direction}">${s.direction === 'long' ? '多' : '空'}</span></td>
      <td><span class="tag ${stateClass(s.state)}">${dispType(sigType(s), s.direction, s.state, s.extra)} ${sigScore(s)}</span></td>
      <td>${fmtP(s.entry)}</td><td class="red">${fmtP(s.sl)}</td><td class="green">${fmtP(s.tp)}</td>
      <td><b>${s.rr}</b></td><td>${s.vol_ratio}x</td><td>${s.status || '–'}</td>
    </tr>`).join('');
  maybeOpenSignalFromUrl();
}
function openChartFromSignal(id) {
  const s = signalCache[id];
  if (s) openChart(s.symbol, s.tf, {entry: s.entry, sl: s.sl, tp: s.tp, extra: s.extra});
}
function maybeOpenSignalFromUrl() {
  if (deepLinkedSignalOpened) return;
  const id = Number(new URLSearchParams(location.search).get('signal') || 0);
  if (!id || !signalCache[id]) return;
  deepLinkedSignalOpened = true;
  openChartFromSignal(id);
}

// ---------- 交易 ----------
let tradeCache = {};
async function loadTrades() {
  const q = `?track=${$('f-track').value}&result=${$('f-result').value}&tf=${$('f-tf-trade').value}&state=${$('f-state-trade').value}`;
  const rows = await api('/api/trades' + q);
  tradeCache = {};
  rows.forEach(t => tradeCache[t.id] = t);
  $('t-trades').innerHTML = rows.map(t => `
    <tr class="clickable" onclick="openChartFromTrade(${t.id})" title="点击查看当时K线形态与买卖点">
      <td>${t.id}</td><td><b>${t.symbol}</b></td><td>${t.tf}</td>
      <td><span class="tag ${stateClass(t.sig_state)}">${dispType(t.track, t.direction, t.sig_state, t.sig_extra)}</span></td>
      <td><span class="tag ${t.direction}">${t.direction === 'long' ? '多' : '空'}</span></td>
      <td>${fmtP(t.entry)}</td><td class="red">${fmtP(t.sl)}</td><td class="green">${fmtP(t.tp)}</td>
      <td>${t.result === 'open' ? '⏳持仓' : t.result === 'tp' ? '🎯止盈' : t.result === 'rev' ? '🔄反向平仓' : '🛑止损'}</td>
      <td class="${(t.pnl ?? 0) > 0 ? 'green' : (t.pnl ?? 0) < 0 ? 'red' : ''}">${t.pnl == null ? '–' : t.pnl.toFixed(2)}</td>
      <td>${t.pnl_r == null ? '–' : t.pnl_r.toFixed(2)}</td>
      <td>${fmtT(t.opened_at)}</td>
    </tr>`).join('') || '<tr><td colspan="12" class="muted">暂无记录</td></tr>';
}
function openChartFromTrade(id) {
  const t = tradeCache[id];
  if (!t) return;
  const sig = t.signal_id && signalCache[t.signal_id];
  openChart(t.symbol, t.tf, {entry: t.entry, sl: t.sl, tp: t.tp,
    extra: sig ? sig.extra : null, opened_at: t.opened_at, exit_price: t.exit_price});
}

// ---------- 关注列表 ----------
async function loadWatch() {
  const rows = await api('/api/watchlist');
  $('watch-chips').innerHTML = rows.map(w => `
    <span class="pill" title="${esc(w.note)}" style="cursor:pointer" onclick="openChart('${symSafe(w.symbol)}','15m')">
      ${esc(w.symbol)}${w.note ? ' · ' + esc(w.note) : ''}
      ${ME.logged_in ? `<span style="color:var(--red);margin-left:6px" onclick="event.stopPropagation();removeWatch('${symSafe(w.symbol)}')">✕</span>` : ''}
    </span>`).join('') || `<span class="muted">${ME.logged_in ? '还没关注的币，加一个（会强制纳入监控，即使成交额不达标）' : '暂无关注'}</span>`;
}
async function addWatch() {
  if (!needLogin()) return;
  const sym = $('watch-symbol').value, note = $('watch-note').value;
  if (!sym) { toast('❌ 请填币种'); return; }
  toast('⏳ 加入并回填K线中…');
  const r = await api('/api/watchlist', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({symbol: sym, note})});
  if (r.ok) { toast('✅ 已关注 ' + r.symbol); $('watch-symbol').value = ''; $('watch-note').value = ''; loadWatch(); }
  else toast('❌ ' + (r.error || '失败'));
}
async function removeWatch(sym) {
  if (!needLogin()) return;
  await api('/api/watchlist/remove', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({symbol: sym})});
  loadWatch();
}

// ---------- 大盘观点 ----------
const MACRO = {long: {t: '看多 🟢', c: 'green'}, short: {t: '看空 🔴', c: 'red'}, neutral: {t: '中性 ⚪', c: 'muted'}};
async function loadMacro() {
  const m = await api('/api/macro');
  const meta = MACRO[m.direction] || MACRO.neutral;
  const badge = $('macro-badge');
  badge.textContent = meta.t;
  badge.className = 'pill ' + meta.c;
  $('macro-note').textContent = m.note ? `「${m.note}」 ${m.at ? fmtT(m.at) : ''}` : '（未设置，可手动录入或等每日例程自动更新）';
  $('macro-dir').value = m.direction;
}
async function saveMacro() {
  if (!needLogin()) return;
  const r = await api('/api/macro', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({direction: $('macro-dir').value, note: $('macro-input').value, source: 'manual'})});
  if (r.ok) { toast('✅ 大盘观点已更新'); $('macro-input').value = ''; loadMacro(); }
  else toast('❌ ' + (r.error || '失败'));
}

// ---------- 预演 Playbook ----------
const PB_STATUS = {active: '⏳监控中', triggered: '🎬已触发', done: '✅完成', cancelled: '已取消'};
const PB_TRIG = {price_reach: '到价', sweep_reclaim: '假突破回收'};
function togglePbForm() { const f = $('pb-form'); f.style.display = f.style.display === 'none' ? 'flex' : 'none'; }
async function savePlaybook() {
  if (!needLogin()) return;
  const body = {
    symbol: $('pb-symbol').value, tf: $('pb-tf').value, direction: $('pb-dir').value,
    trigger_type: $('pb-trig').value, trigger_price: $('pb-trigprice').value,
    entry: $('pb-entry').value, tp: $('pb-tp').value, sl: $('pb-sl').value, title: $('pb-title').value,
  };
  if (!body.symbol) { toast('❌ 请填币种'); return; }
  const r = await api('/api/playbooks', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r.ok) { toast('✅ 预演已建 #' + r.id); togglePbForm(); ['pb-symbol','pb-trigprice','pb-entry','pb-tp','pb-sl','pb-title'].forEach(i => $(i).value = ''); loadPlaybooks(); }
  else toast('❌ ' + (r.error || '失败'));
}
async function pbAction(id, status) {
  if (!needLogin()) return;
  await api('/api/playbooks/' + id, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status})});
  loadPlaybooks();
}
async function loadPlaybooks() {
  const rows = await api('/api/playbooks');
  $('t-playbooks').innerHTML = rows.map(p => `
    <tr class="clickable" onclick="openChart('${p.symbol}','${p.tf || '15m'}',{entry:${p.entry},sl:${p.sl},tp:${p.tp}})">
      <td>${p.id}</td><td><b>${p.symbol}</b></td><td>${p.tf || '不限'}</td>
      <td><span class="tag ${p.direction}">${p.direction === 'long' ? '多' : p.direction === 'short' ? '空' : '观'}</span></td>
      <td>${PB_TRIG[p.trigger_type] || p.trigger_type}</td>
      <td class="gold">${fmtP(p.trigger_price)}</td><td>${fmtP(p.entry)}</td>
      <td class="green">${fmtP(p.tp)}</td><td class="red">${fmtP(p.sl)}</td>
      <td>${PB_STATUS[p.status] || p.status}</td><td class="muted">${esc(p.title)}</td>
      <td onclick="event.stopPropagation()">${!ME.logged_in ? '' : p.status === 'active'
        ? `<button class="btn" style="padding:2px 8px" onclick="pbAction(${p.id},'cancelled')">取消</button>`
        : `<button class="btn" style="padding:2px 8px;background:var(--panel2)" onclick="pbAction(${p.id},'done')">归档</button>`}</td>
    </tr>`).join('') || '<tr><td colspan="12" class="muted">还没有预演，点「+ 新建预演」记录一个剧本</td></tr>';
}

// ---------- 策略回测 ----------
let btTimer = null;
async function runBacktest() {
  if (!needLogin()) return;
  const days = $('bt-days').value;
  const r = await api('/api/backtest', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({days: Number(days)})});
  if (r.error) { toast('❌ ' + r.error); return; }
  $('bt-run').disabled = true;
  $('bt-status').textContent = '回测中…';
  if (btTimer) clearInterval(btTimer);
  btTimer = setInterval(pollBacktest, 2000);
}
async function pollBacktest() {
  const d = await api('/api/backtest');
  if (d.running) { $('bt-status').textContent = '回测中 ' + (d.progress || ''); return; }
  clearInterval(btTimer); btTimer = null;
  $('bt-run').disabled = false;
  $('bt-status').textContent = '';
  if (d.result) renderBacktest(d.result);
}
function btTable(title, obj) {
  const rows = Object.entries(obj).map(([k, b]) => `
    <tr><td><b>${TYPE_TAG[k] || k}</b></td><td>${b.signals}</td><td>${b.closed}</td><td>${b.open}</td>
    <td>${b.win_rate}%</td>
    <td class="${b.total_r > 0 ? 'green' : b.total_r < 0 ? 'red' : ''}">${b.total_r}R</td>
    <td>${b.avg_r}R</td></tr>`).join('');
  return `<h3 style="margin:10px 0 6px">${title}</h3>
    <table><thead><tr><th></th><th>信号</th><th>已平</th><th>未平</th><th>胜率</th><th>总盈亏</th><th>均值</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}
function renderBacktest(r) {
  if (r.error) { $('bt-result').innerHTML = `<span class="red">回测失败: ${r.error}</span>`; return; }
  const t = r.total;
  let html = `<p>近 <b>${r.period_days}</b> 天 · ${r.symbols} 币 · ${r.tfs.join('/')} · 耗时 ${r.elapsed_s}s ·
    共 <b>${t.signals}</b> 信号 · 胜率 <b>${t.win_rate}%</b> ·
    总盈亏 <b class="${t.total_r > 0 ? 'green' : 'red'}">${t.total_r}R</b> · 期望 <b>${t.avg_r}R</b>/笔</p>`;
  html += btTable('按级别', r.by_tf) + btTable('按信号类型', r.by_type) + btTable('按方向', r.by_direction);
  const sigRows = (r.signals || []).slice(-60).reverse().map(s => `
    <tr class="clickable" onclick="openChart('${s.symbol}','${s.tf}',{entry:${s.entry},sl:${s.sl},tp:${s.tp}})">
      <td>${fmtT(s.time)}</td><td><b>${s.symbol}</b></td><td>${s.tf}</td>
      <td><span class="tag ${s.direction}">${s.direction === 'long' ? '多' : '空'}</span></td>
      <td>${TYPE_TAG[s.type] || s.type} ${s.score ?? ''}</td>
      <td>${fmtP(s.entry)}</td><td>${s.result === 'open' ? '⏳' : s.result === 'tp' ? '🎯' : '🛑'}</td>
      <td class="${(s.pnl_r ?? 0) > 0 ? 'green' : (s.pnl_r ?? 0) < 0 ? 'red' : ''}">${s.pnl_r == null ? '–' : s.pnl_r.toFixed(2) + 'R'}</td>
    </tr>`).join('');
  html += `<h3 style="margin:10px 0 6px">最近信号明细（点击看图）</h3>
    <table><thead><tr><th>时间</th><th>币种</th><th>级别</th><th>方向</th><th>类型</th><th>入场</th><th>结果</th><th>盈亏</th></tr></thead>
    <tbody>${sigRows}</tbody></table>`;
  $('bt-result').innerHTML = html;
}

// ---------- 级别准确率 ----------
async function loadTfStats() {
  const d = await api('/api/stats_by_tf');
  $('t-tfstats').innerHTML = (d.by_tf || []).map(r => {
    const closed = r.n - (r.open_cnt || 0);
    const wr = closed > 0 ? (100 * r.wins / closed).toFixed(1) : '–';
    return `<tr><td><b>${r.tf}</b></td><td>${r.n}</td><td>${r.open_cnt || 0}</td>
      <td>${closed}</td><td>${wr}${closed > 0 ? '%' : ''}</td>
      <td class="${(r.pnl ?? 0) > 0 ? 'green' : (r.pnl ?? 0) < 0 ? 'red' : ''}">${r.pnl ?? 0}</td>
      <td>${r.avg_r ?? '–'}</td></tr>`;
  }).join('') || '<tr><td colspan="7" class="muted">暂无成交数据</td></tr>';
}

// ---------- 权益曲线 ----------
let eqChart, eqSeries;
async function loadEquity() {
  const track = $('f-track').value || 'buy1';
  const data = await api('/api/equity?track=' + track);
  $('eq-note').textContent = TRACK_NAMES[track] + ' · ' + data.length + ' 个结算点';
  if (!eqChart) {
    eqChart = LightweightCharts.createChart($('equity-chart'), {
      layout: {background: {color: 'transparent'}, textColor: '#7a869c'},
      grid: {vertLines: {color: '#1d2433'}, horzLines: {color: '#1d2433'}},
      timeScale: {timeVisible: true}, height: 220, autoSize: true,
    });
    eqSeries = eqChart.addLineSeries({color: '#4f8ef7', lineWidth: 2});
  }
  eqSeries.setData(data.map(d => ({time: d.ts, value: d.equity})));
  eqChart.timeScale().fitContent();
}

// ---------- K线弹窗（标注：买入/止盈/止损线 + 信号类型 + 触发K，可切换级别）----------
let klChart;
let _chartCtx = null;                 // {symbol, ref, tf}
const CHART_TFS = ['5m', '15m', '30m', '1h'];
async function openChart(symbol, tf, ref) {
  $('modal').classList.add('show');
  _chartCtx = {symbol, ref, tf};
  $('tf-switch').innerHTML = CHART_TFS.map(t =>
    `<button class="tfbtn${t === tf ? ' on' : ''}" data-tf="${t}" onclick="switchTf('${t}')">${t}</button>`).join('');
  await renderChart(symbol, tf, ref);
}
function switchTf(tf) {
  if (!_chartCtx) return;
  _chartCtx.tf = tf;
  document.querySelectorAll('#tf-switch .tfbtn').forEach(b => b.classList.toggle('on', b.dataset.tf === tf));
  renderChart(_chartCtx.symbol, tf, _chartCtx.ref);
}
async function renderChart(symbol, tf, ref) {
  $('m-title').textContent = `${symbol} · ${tf}` + (ref ? ' · 🟡黄箭头=入场 · 🟠橙圈/橙线=缠论底/顶分型(威科夫=爆量扫破位) · 蓝=买入 绿=止盈 红=止损' : '');
  const d = await api(`/api/klines?symbol=${symbol}&tf=${tf}&limit=300`);
  $('chart').innerHTML = '';
  if (klChart) { klChart.remove(); klChart = null; }
  await new Promise(r => setTimeout(r, 60));  // 等弹窗布局完成，避免首开图表零尺寸空白
  klChart = LightweightCharts.createChart($('chart'), {
    layout: {background: {color: 'transparent'}, textColor: '#7a869c'},
    grid: {vertLines: {color: '#1d2433'}, horzLines: {color: '#1d2433'}},
    timeScale: {timeVisible: true}, autoSize: true,
  });
  const cs = klChart.addCandlestickSeries({
    upColor: '#2ecc71', downColor: '#e74c3c', borderVisible: false,
    wickUpColor: '#2ecc71', wickDownColor: '#e74c3c',
  });
  cs.setData(d.klines.map(k => ({time: k.open_time / 1000, open: k.open, high: k.high, low: k.low, close: k.close})));
  const vs = klChart.addHistogramSeries({priceFormat: {type: 'volume'}, priceScaleId: 'vol'});
  klChart.priceScale('vol').applyOptions({scaleMargins: {top: 0.8, bottom: 0}});
  vs.setData(d.klines.map(k => ({time: k.open_time / 1000, value: k.volume, color: k.close >= k.open ? '#2ecc7144' : '#e74c3c44'})));

  // 标记时间对齐到当前级别的K(切级别后仍能落在对应那根K上); 落在范围外则跳过
  const barTimes = d.klines.map(k => Math.floor(k.open_time / 1000));
  const snap = t => {
    let lo = 0, hi = barTimes.length - 1, res = null;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (barTimes[m] <= t) { res = barTimes[m]; lo = m + 1; } else hi = m - 1; }
    return res;
  };
  // 信号标记：买点箭头(=停顿K入场) + 顶/底分型(真正参与一↔二比较的极值) + 破位K + 主力K(可选)
  const markers = [];
  for (const s of d.signals.filter(x => x.status !== 'error')) {
    let ex = {};
    try { ex = JSON.parse(s.extra || '{}'); } catch (e) {}
    const entryTime = ex.structure && ex.structure.entry_time ? Math.floor(Number(ex.structure.entry_time) / 1000) : s.created_at;
    const et = snap(entryTime);
    if (et != null) markers.push({
      time: et, position: s.direction === 'long' ? 'belowBar' : 'aboveBar',
      color: '#ffd700',                                   // 买入点统一黄色, 醒目区分入场位置
      shape: s.direction === 'long' ? 'arrowUp' : 'arrowDown',
      text: `${dispType(ex.type, s.direction, s.state, s.extra)} #${s.id}`,
    });
    // 橙点/橙线：缠论=底/顶分型那根K; 威科夫=爆量K扫破极值(止损位)。两者标签区分,避免混淆
    if (ex.fractal_price != null) {
      const isWy = ex.path === '威科夫';
      let tag;
      if (isWy) {
        const vx = s.vol_ratio != null ? ` ${s.vol_ratio}x` : '';
        tag = `💥爆量K${vx}`;          // 威科夫: 橙圈/橙线落在那根爆量扫破K上
      } else {
        tag = s.direction === 'long' ? '底分型' : '顶分型';
      }
      const ft = ex.fractal_time ? snap(Math.floor(ex.fractal_time / 1000)) : null;
      if (ft != null) {
        markers.push({
          time: ft,
          position: s.direction === 'long' ? 'belowBar' : 'aboveBar',
          color: '#f39c12', shape: 'circle', text: `${tag}#${s.id}`,
        });
      }
      cs.createPriceLine({
        price: ex.fractal_price, color: '#f39c12', lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true,
        title: `${tag} #${s.id}`,
      });
    }
    if (ex.breakdown && ex.breakdown.time) {
      const bt = snap(Math.floor(ex.breakdown.time / 1000));
      if (bt != null) markers.push({time: bt, position: 'aboveBar', color: '#f1c40f', shape: 'circle', text: '破位K'});
    }
    if (Array.isArray(ex.markers)) {
      for (const m of ex.markers) {
        const rawTime = Number(m.time || 0);
        const mt = rawTime ? snap(Math.floor(rawTime / 1000)) : null;
        if (mt == null) continue;
        const isSweep = m.key === 'volume_sweep';
        const text = isSweep && m.vol_ratio != null ? `${m.label} ${m.vol_ratio}x #${s.id}` : `${m.label} #${s.id}`;
        const position = m.position || (s.direction === 'long' ? 'belowBar' : 'aboveBar');
        const color = isSweep ? '#ff7043' : '#4f8ef7';
        markers.push({
          time: mt,
          position,
          color,
          shape: isSweep ? 'square' : 'circle',
          text,
        });
        if (m.price != null) {
          cs.createPriceLine({
            price: Number(m.price),
            color,
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dotted,
            axisLabelVisible: true,
            title: text,
          });
        }
      }
    }
    if (ex.main_k && ex.main_k.time) {
      const mt = snap(Math.floor(ex.main_k.time / 1000));
      if (mt != null) markers.push({time: mt, position: 'aboveBar', color: '#9b59b6', shape: 'square', text: '🚩主力K'});
    }
  }
  // 同一根K上多个标记按时间稳定排序; 去掉重复时间冲突由库自行堆叠
  cs.setMarkers(markers.sort((a, b) => a.time - b.time));

  // 买入/止盈/止损 价格线
  if (ref && ref.entry != null) {
    cs.createPriceLine({price: ref.entry, color: '#4f8ef7', lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: '买入'});
    if (ref.tp != null) cs.createPriceLine({price: ref.tp, color: '#2ecc71', lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: '止盈'});
    if (ref.sl != null) cs.createPriceLine({price: ref.sl, color: '#e74c3c', lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: '止损'});
  }
  klChart.timeScale().fitContent();
}
function closeModal() { $('modal').classList.remove('show'); _chartCtx = null; if (klChart) { klChart.remove(); klChart = null; } }

// ---------- 设置 ----------
const SETTING_LABELS = {
  'mode': '运行模式', 'trade_direction': '交易方向',
  'live.auto_trade': '自动下单(免确认)', 'live.fixed_margin_pct': '单笔保证金%(×杠杆)', 'live.fixed_margin_u': '单笔保证金U(×杠杆)', 'live.fixed_notional_u': '单笔名义额U',
  'live.max_positions': '实盘最大持仓', 'live.max_loss_pct': '亏损熔断%',
  'chan.bi_min_bars': '一笔最少合并K', 'chan.stall_max_gap': '停顿窗口(根)',
  'chan.fractal_vol_mult': '底分型放量倍数', 'chan.fractal_vol_ma': '放量均量回看(根)',
  'chan.require_divergence': '一买必须背驰', 'chan.mtf_tol_pct': '多级别价位容差%',
  'chan.strong_reversal_15m': '15m只认强反转', 'chan.reversal_body_ratio': '强反转右K实体占比',
  'spring.min_rr': '最低盈亏比门槛', 'spring.tp_lookback': '止盈回看根数',
  'signal.sl_buffer_pct': '止损缓冲%', 'spring.btc_filter': '回测BTC过滤',
  'risk.account_equity': '账户本金U', 'risk.risk_pct': '单笔风险%',
  'risk.max_positions': '最大持仓数', 'risk.leverage': '杠杆',
  'universe.min_quote_volume_24h': '最低24h成交额',
};
const BOOL_SETTINGS = ['chan.require_divergence', 'chan.strong_reversal_15m', 'spring.btc_filter', 'live.auto_trade'];
// 顶部一键自动交易开关
async function loadAutoSwitch() {
  if (!ME.logged_in) return;
  const s = await api('/api/settings'), btn = $('auto-switch');
  if (!btn) return;
  const on = !!s['live.auto_trade'], live = s.mode === 'live';
  btn.textContent = '自动交易: ' + (on ? '🟢开' : '🔴关') + (live ? '' : '(非live)');
  btn.style.background = on ? 'var(--green)' : 'var(--panel2)';
  btn.style.color = on ? '#fff' : 'var(--text)';
  btn.dataset.on = on ? '1' : '0';
}
async function toggleAuto() {
  if (!needLogin()) return;
  const btn = $('auto-switch'), turnOn = btn.dataset.on !== '1';
  if (turnOn && !confirm('确认开启【自动下单】? 信号将免Telegram确认直接真实下单(受熔断/持仓/单笔名义额限制)。')) return;
  const r = await api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({'live.auto_trade': turnOn ? 'true' : 'false'})});
  toast(r.ok ? ('✅ 自动交易已' + (turnOn ? '开启' : '关闭')) : '❌ ' + (r.error || '失败'));
  loadAutoSwitch();
}
// 🔴 线上配置项: 直接影响实盘下单金额/杠杆/风控的参数, 单独分组醒目展示
const LIVE_KEYS = ['mode', 'trade_direction', 'live.auto_trade', 'risk.account_equity', 'risk.leverage',
  'live.fixed_margin_pct', 'live.fixed_margin_u', 'live.fixed_notional_u', 'live.max_positions', 'live.max_loss_pct'];
function renderField(k, v) {
  const label = SETTING_LABELS[k] || k;
  if (k === 'mode') return `<label>${label}<select data-k="${k}">
    <option value="paper" ${v === 'paper' ? 'selected' : ''}>paper 模拟</option>
    <option value="live" ${v === 'live' ? 'selected' : ''}>live 实盘</option></select></label>`;
  if (k === 'trade_direction') return `<label>${label}<select data-k="${k}">
    <option value="both" ${v === 'both' ? 'selected' : ''}>全部(多空)</option>
    <option value="long" ${v === 'long' ? 'selected' : ''}>只做多</option>
    <option value="short" ${v === 'short' ? 'selected' : ''}>只做空</option></select></label>`;
  if (BOOL_SETTINGS.includes(k)) return `<label>${label}<select data-k="${k}">
    <option value="true" ${v ? 'selected' : ''}>开</option>
    <option value="false" ${!v ? 'selected' : ''}>关</option></select></label>`;
  return `<label>${label}<input data-k="${k}" value="${v}"></label>`;
}
async function loadSettings() {
  if (!ME.logged_in) return;
  const s = await api('/api/settings');
  const title = (t, c) => `<div style="flex-basis:100%;width:100%;font-weight:600;color:${c};margin:8px 0 2px">${t}</div>`;
  const live = LIVE_KEYS.filter(k => k in s).map(k => renderField(k, s[k])).join('');
  const other = Object.entries(s).filter(([k]) => !LIVE_KEYS.includes(k)).map(([k, v]) => renderField(k, v)).join('');
  $('settings').innerHTML =
    title('🔴 线上配置项 · 改完点「保存并热生效」立即生效(影响实盘下单)', '#f0883e') + live +
    title('🧪 策略 / 回测参数', '#8b949e') + other;
}
async function saveSettings() {
  if (!needLogin()) return;
  const body = {};
  document.querySelectorAll('#settings [data-k]').forEach(el => body[el.dataset.k] = el.value);
  const r = await api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  toast(r.ok ? '✅ 已保存并热生效' : '❌ ' + (r.error || '保存失败'));
}

// ---------- 实时推送 ----------
// 匿名也能连(只收推送)；服务端对匿名连接数有上限，连不上就退避重试，期间靠定时轮询兜底
let wsUp = false, wsFails = 0;
function connectWS() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/ws');
  let ping = null;
  ws.onmessage = e => {
    const {kind, data} = JSON.parse(e.data);
    if (kind === 'signal') {
      toast(`📡 新信号 #${data.id} ${data.symbol} ${data.tf} ${data.direction === 'long' ? '做多' : '做空'} RR=${data.rr}`);
      run(loadSignals); run(loadStatus);
    } else if (kind === 'trade_close') {
      toast(`${data.result === 'tp' ? '🎯' : '🛑'} ${data.symbol} ${data.result.toUpperCase()} ${data.pnl?.toFixed(2)}U`);
      run(loadTrades); run(loadStatus);
    }
  };
  ws.onopen = () => {
    wsUp = true; wsFails = 0;
    ping = setInterval(() => ws.readyState === 1 && ws.send('ping'), 30000);
  };
  ws.onclose = () => {
    wsUp = false; wsFails++;
    if (ping) clearInterval(ping);
    setTimeout(connectWS, Math.min(60000, 5000 * wsFails));
  };
}

// ---------- 实盘持仓 ----------
async function loadPositions() {
  if (!ME.logged_in) return;
  const d = await api('/api/positions');
  const note = $('pos-note'), tb = $('t-positions');
  if (!d.live) { note.textContent = 'paper 模式(未实盘)'; tb.innerHTML = '<tr><td colspan="8" class="muted">切到 live 模式后显示真实持仓</td></tr>'; return; }
  if (d.error) { note.textContent = '读取失败: ' + d.error; tb.innerHTML = ''; return; }
  const ps = d.positions || [];
  note.textContent = `${ps.length} 个持仓`;
  tb.innerHTML = ps.map(p => `<tr>
    <td><b>${p.symbol}</b></td>
    <td><span class="tag ${p.direction}">${p.direction === 'long' ? '多' : '空'}</span></td>
    <td>${p.amt}</td><td>${fmtP(p.entry)}</td><td>${fmtP(p.mark)}</td>
    <td class="${p.pnl > 0 ? 'green' : p.pnl < 0 ? 'red' : ''}">${p.pnl >= 0 ? '+' : ''}${p.pnl}</td>
    <td>${p.leverage}x</td><td>${p.strategy || '?'}${p.tf ? ' ' + p.tf : ''}</td>
  </tr>`).join('') || '<tr><td colspan="8" class="muted">当前无持仓</td></tr>';
}

// ---------- 实盘数据(真实盈亏/胜率/手续费, 与模拟盘区分) ----------
async function loadLiveStats() {
  const el = $('live-stats');
  if (!el || !ME.logged_in) return;
  const d = await api('/api/live_stats?days=30');
  if (!d.live) { el.textContent = '💰 实盘数据: 切到 live 模式后显示真实盈亏/手续费'; return; }
  if (d.error) { el.textContent = '💰 实盘数据读取失败: ' + d.error; return; }
  if (d.note || !d.trades) { el.innerHTML = `💰 <b>实盘</b>: ${d.note || '本系统尚无实盘成交'}(只统计本系统下的单)`; return; }
  const cls = v => v > 0 ? 'green' : v < 0 ? 'red' : '';
  el.innerHTML = `💰 <b>实盘</b>(近${d.days}天): 净盈亏 <span class="${cls(d.net)}">${d.net >= 0 ? '+' : ''}${d.net}U</span>`
    + ` · ${d.trades}单 胜率${d.win_rate}% 均${d.avg >= 0 ? '+' : ''}${d.avg}U`
    + ` · 已实现${d.realized} · <span class="red">手续费${d.commission}</span> · 资金费${d.funding}`;
}

// ---------- BTC K线(可切级别) ----------
let btcChart, btcSeries, btcVol, _btcTf = '1h';
function switchBtcTf(tf) {
  _btcTf = tf;
  document.querySelectorAll('#btc-tf-switch .tfbtn').forEach(b => b.classList.toggle('on', b.dataset.tf === tf));
  loadBtcChart();
}
async function loadBtcChart() {
  if (!$('btc-tf-switch').innerHTML) {
    $('btc-tf-switch').innerHTML = CHART_TFS.map(t =>
      `<button class="tfbtn${t === _btcTf ? ' on' : ''}" data-tf="${t}" onclick="switchBtcTf('${t}')">${t}</button>`).join('');
  }
  const d = await api(`/api/klines?symbol=BTCUSDT&tf=${_btcTf}&limit=200`);
  if (!d.klines || !d.klines.length) { $('btc-note').textContent = '无数据'; return; }
  if (!btcChart) {
    btcChart = LightweightCharts.createChart($('btc-chart'), {
      layout: {background: {color: 'transparent'}, textColor: '#7a869c'},
      grid: {vertLines: {color: '#1d2433'}, horzLines: {color: '#1d2433'}},
      timeScale: {timeVisible: true}, autoSize: true,
    });
    btcSeries = btcChart.addCandlestickSeries({upColor: '#2ecc71', downColor: '#e74c3c',
      borderVisible: false, wickUpColor: '#2ecc71', wickDownColor: '#e74c3c'});
    btcVol = btcChart.addHistogramSeries({priceFormat: {type: 'volume'}, priceScaleId: 'vol'});
    btcChart.priceScale('vol').applyOptions({scaleMargins: {top: 0.8, bottom: 0}});
  }
  btcSeries.setData(d.klines.map(k => ({time: k.open_time / 1000, open: k.open, high: k.high, low: k.low, close: k.close})));
  btcVol.setData(d.klines.map(k => ({time: k.open_time / 1000, value: k.volume, color: k.close >= k.open ? '#2ecc7144' : '#e74c3c44'})));
  $('btc-note').textContent = '最新 ' + fmtP(d.klines[d.klines.length - 1].close);
  btcChart.timeScale().fitContent();
}

// ---------- 启动 ----------
function removeLegacySummaryPanels() {
  ['t-playbooks', 'bt-result', 't-tfstats', 'equity-chart'].forEach(id => {
    const el = $(id);
    const panel = el ? el.closest('.panel') : null;
    if (panel) panel.remove();
  });
}

async function loadStrategyStatus() {
  const s = await api('/api/status');
  if (s.error) {
    $('stat-cards').innerHTML = '<div class="card"><h3>引擎</h3><div class="big red">未运行</div></div>';
    return;
  }
  $('st-mode').textContent = s.mode === 'paper' ? 'PAPER' : 'LIVE';
  $('st-symbols').textContent = `${s.symbols} 币种`;
  $('st-ws').textContent = s.ws_conns > 0 ? `WS OK (${s.ws_last_msg_age_s ?? '?'}s)` : 'WS OFF';
  const h = Math.floor(s.uptime_s / 3600), m = Math.floor(s.uptime_s % 3600 / 60);
  $('st-uptime').textContent = `运行 ${h}h${m}m`;

  let cards = `<div class="card"><h3>引擎</h3>
    <div class="big ${s.ws_conns > 0 ? '' : 'red'}">${s.ws_conns > 0 ? '正常' : '异常'}</div>
    <div class="sub">评估 ${s.last_eval_ms}ms · 累计预位 ${(s.funnel || {}).breakdown || 0} 次</div></div>`;

  const strategyStats = await api('/api/strategy_stats');
  if (!strategyStats.length) {
    cards += `<div class="card"><h3>策略模拟盘</h3>
      <div class="big">+0 U</div>
      <div class="sub">0平 / 0持 · 胜率0% · 期望0R</div></div>`;
  }
  for (const t of strategyStats) {
    const pnl = Number(t.total_pnl || 0);
    const cls = pnl > 0 ? 'green' : pnl < 0 ? 'red' : '';
    cards += `<div class="card"><h3>${t.strategy}</h3>
      <div class="big ${cls}">${pnl >= 0 ? '+' : ''}${pnl} U</div>
      <div class="sub">${t.closed}平 / ${t.open}持 · 胜率${t.win_rate}% · 期望${t.expectancy_r}R</div></div>`;
  }
  $('stat-cards').innerHTML = cards;

  const sq = s.squeeze || [];
  $('squeeze-chips').innerHTML = sq.length ? sq.map(c => `
    <span class="pill ${c.strong ? 'gold' : ''}" style="cursor:pointer" onclick="openChart('${c.symbol}','15m')"
      title="OI ${c.oi_change_pct}% · 费率${((c.funding||0)*100).toFixed(3)}% · 价格位${Math.round(c.pos*100)}%">
      ${c.strong ? 'HOT' : 'WARN'} ${c.symbol} OI${c.oi_change_pct >= 0 ? '+' : ''}${c.oi_change_pct}%
    </span>`).join('') : '<span class="muted">暂无（行情平静时正常）</span>';
}
loadStatus = loadStrategyStatus;

// ---------- 价格提醒(仅登录后)：手动提醒 设/查/删 + 自动提醒与联动规则的当前状态 ----------
// 币种、备注、status 的键和值都可能是任意字符串：这一段只用 createElement + textContent，不拼 innerHTML。
const AL_OP = {below: '跌破', above: '涨破'};
// 曼谷时间(UTC+7)，形如 "11-16 13:05"；秒或毫秒时间戳都认
function fmtBkk(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  const d = new Date((n > 1e11 ? n : n * 1000) + 7 * 3600 * 1000);
  const p = x => String(x).padStart(2, '0');
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}
function alEl(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const alOpen = {};      // 明细展开状态：定时刷新重画时保持
// 一行状态：后端给了摘要就显示摘要(「键: 值」明细收进可展开的 <details>)；没有摘要就把明细拼成一行。
// 摘要和明细都是后端拼好的纯文本(status 的键名由各模块自己定，有什么显示什么)。
function alStatusLine(box, key, title, summary, lines, failed, off) {
  lines = Array.isArray(lines) ? lines.map(String) : [];
  const text = off ? '未启用' : failed ? '状态读取失败' : (summary || lines.join(' · ') || '无');
  box.appendChild(alEl('div', '', `${title} · ${text}`));
  if (off || failed || !summary || !lines.length) return;
  const det = alEl('details', 'al-more');
  det.dataset.key = key;
  det.open = !!alOpen[key];
  det.addEventListener('toggle', () => { alOpen[key] = det.open; });
  det.appendChild(alEl('summary', '', '明细'));
  for (const ln of lines) det.appendChild(alEl('div', '', ln));
  box.appendChild(det);
}
const alPx = v => (typeof v === 'number' && Number.isFinite(v)) ? String(fmtP(v)) : '–';
function alDist(level, last) {
  if (typeof level !== 'number' || typeof last !== 'number' || !(last > 0) || !Number.isFinite(level)) return '';
  const pct = (level - last) / last * 100;
  return ` (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)`;
}
function renderAlerts(d) {
  const list = $('al-list'), info = $('al-info');
  if (!list || !info) return;
  const errs = d.errors || [];
  list.replaceChildren();
  info.replaceChildren();
  $('al-form').style.display = d.enabled ? '' : 'none';
  list.style.display = d.enabled ? '' : 'none';
  if (!d.enabled) {
    $('al-note').textContent = '未启用（引擎未运行或已关闭）';
  } else {
    const rows = (d.levels || []).filter(a => (a.status || 'active') === 'active');
    $('al-note').textContent = errs.includes('levels') ? '列表读取失败' : rows.length ? `${rows.length} 条生效中` : '';
    for (const a of rows) {
      const row = alEl('div', 'al-row');
      const made = fmtBkk(a.created_at);
      if (made) row.title = `创建于 ${made} (UTC+7)`;
      const main = alEl('div', 'al-main');           // 左边内容可换行，右边删除键固定
      main.appendChild(alEl('span', 'muted', '#' + a.id));
      main.appendChild(alEl('b', '', String(a.symbol ?? '?')));
      main.appendChild(alEl('span', 'tag ' + (a.op === 'above' ? 'long' : 'short'), AL_OP[a.op] || String(a.op || '?')));
      main.appendChild(alEl('span', 'al-px', alPx(a.price)));
      if (a.last_price != null) main.appendChild(alEl('span', 'muted', `现价 ${alPx(a.last_price)}${alDist(a.price, a.last_price)}`));
      if (a.note) main.appendChild(alEl('span', 'al-note muted', String(a.note)));
      row.appendChild(main);
      const del = alEl('button', 'al-del', '✕');
      del.type = 'button';
      del.title = `删除提醒 #${a.id}`;
      del.setAttribute('aria-label', del.title);
      del.addEventListener('click', () => removeAlert(a.id));
      row.appendChild(del);
      list.appendChild(row);
    }
    if (!rows.length && !errs.includes('levels')) list.appendChild(alEl('div', 'muted', '暂无手动提醒（到价后 Telegram 推送一次）'));
    // 最近已触发的(最多 5 条)：价位触发后就从上面的列表消失，Telegram 那条万一没送达，这里还看得到
    const fired = (d.levels || []).filter(a => a.status === 'fired').slice(0, 5);
    for (const a of fired) {
      const row = alEl('div', 'al-row muted');
      const main = alEl('div', 'al-main');
      main.appendChild(alEl('span', '', '#' + a.id));
      main.appendChild(alEl('b', '', String(a.symbol ?? '?')));
      main.appendChild(alEl('span', '', `${AL_OP[a.op] || String(a.op || '?')} ${alPx(a.price)}`));
      main.appendChild(alEl('span', '', `已触发 ${fmtBkk(a.fired_at)}${a.fired_price != null ? ' @ ' + alPx(a.fired_price) : ''}`));
      if (a.note) main.appendChild(alEl('span', 'al-note', String(a.note)));
      row.appendChild(main);
      const del = alEl('button', 'al-del', '✕');
      del.type = 'button';
      del.title = `从列表移除 #${a.id}`;
      del.setAttribute('aria-label', del.title);
      del.addEventListener('click', () => removeAlert(a.id));
      row.appendChild(del);
      list.appendChild(row);
    }
    alStatusLine(info, 'auto', '⚙️ 自动提醒', d.status_summary, d.status_lines, errs.includes('status'), false);
  }
  alStatusLine(info, 'linked', '🔗 联动规则', d.linked_summary, d.linked_lines, errs.includes('linked'), !d.linked_enabled);
  // 通知通路出过问题(发送失败/积压丢弃/内部异常)才显示这一行
  if (d.health_summary) info.appendChild(alEl('div', '', `⚠️ 通知通路 · ${d.health_summary}`));
}
async function loadAlerts() {
  if (!ME.logged_in) return;
  renderAlerts(await api('/api/alerts?fired=1'));
}
async function addAlert(ev) {
  if (ev) ev.preventDefault();
  if (!needLogin()) return;
  const symbol = $('al-symbol').value.trim(), price = $('al-price').value.trim();
  if (!symbol) { toast('❌ 请填币种'); return; }
  if (!price) { toast('❌ 请填价格'); return; }
  const btn = $('al-add');
  btn.disabled = true;
  try {
    // 价格按字符串原样发给后端解析(63k / 63,000 都认)，与 Telegram 指令同一套规则
    const r = await api('/api/alerts', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol, price, op: $('al-op').value, note: $('al-note-in').value.trim()})});
    if (r.ok) {
      const a = r.alert || {};
      toast(`🔔 已设提醒 #${a.id ?? '?'} ${a.symbol || ''} ${AL_OP[a.op] || ''} ${alPx(a.price)}`);
      $('al-price').value = ''; $('al-note-in').value = ''; $('al-op').value = '';
      run(loadAlerts);
    } else toast('❌ ' + (r.error || '添加失败'));
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;      // ApiError 已由 api() 提示过
  } finally { btn.disabled = false; }
}
async function removeAlert(id) {
  if (!needLogin()) return;
  try {
    const r = await api('/api/alerts/remove', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    toast(r.ok ? `🗑 已删除提醒 #${id}` : '❌ ' + (r.error || '删除失败'));
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
  }
  run(loadAlerts);
}

// ---------- 账号(仅登录后)：初始密码提醒 + 修改密码 ----------
async function loadAccount() {
  if (!ME.logged_in) return;
  const a = await api('/api/account');
  const w = $('pwd-warn');
  if (w) w.style.display = a.default_password ? '' : 'none';
}
function togglePwdForm(show) {
  const f = $('pwd-form');
  if (!f) return;
  f.style.display = (show === true || f.style.display === 'none') ? 'flex' : 'none';
  if (f.style.display !== 'none') f.scrollIntoView({block: 'center'});
}
async function changePassword() {
  if (!needLogin()) return;
  const oldp = $('pwd-old').value, newp = $('pwd-new').value;
  if (newp.length < 8) { toast('❌ 新密码至少8位'); return; }
  if (newp !== $('pwd-new2').value) { toast('❌ 两次输入的新密码不一致'); return; }
  const r = await api('/api/password', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old: oldp, new: newp})});
  if (r.ok) {
    toast('✅ 密码已修改，其它设备上的登录已全部失效');
    ['pwd-old', 'pwd-new', 'pwd-new2'].forEach(i => $(i).value = '');
    $('pwd-form').style.display = 'none';
    run(loadAccount);
  } else toast('❌ ' + (r.error || '修改失败'));
}

async function boot() {
  removeLegacySummaryPanels();
  try {
    const m = await api('/api/me');
    ME = {logged_in: !!m.logged_in, user: m.user || null};
  } catch (e) { ME = {logged_in: false, user: null}; }
  applyAuthUI();
  [loadMacro, loadWatch, loadStatus, loadSignals, loadTrades, loadBtcChart].forEach(f => run(f));
  if (ME.logged_in) [loadSettings, loadPositions, loadAutoSwitch, loadAccount, loadAlerts].forEach(f => run(f));
  connectWS();
  setInterval(() => run(loadStatus), 15000);
  setInterval(() => ME.logged_in && run(loadAutoSwitch), 20000);
  setInterval(() => ME.logged_in && run(loadPositions), 15000);
  setInterval(() => ME.logged_in && !document.hidden && run(loadAlerts), 20000);   // 刷新现价与状态；页面不在前台就不刷
  setInterval(() => run(loadBtcChart), 60000);
  setInterval(() => { if (!wsUp) { run(loadSignals); run(loadTrades); } }, 60000);   // 推送不通时的兜底刷新
}
boot();
