// 缠论交易系统前端
const $ = id => document.getElementById(id);
const fmtT = ts => new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
const fmtP = p => p == null ? '–' : (p >= 100 ? p.toLocaleString('en', {maximumFractionDigits: 2}) : Number(p.toPrecision(6)));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = '/login.html'; throw new Error('401'); }
  return r.json();
}

function toast(msg) {
  const d = document.createElement('div');
  d.textContent = msg;
  $('toast').appendChild(d);
  setTimeout(() => d.remove(), 6000);
}

async function logout() { await fetch('/api/logout', {method: 'POST'}); location.href = '/login.html'; }

// ---------- 状态与统计 ----------
const TRACK_NAMES = {buy1: '✅ 一买', buy2: '🔁 二买'};
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
const TYPE_TAG = {buy1: '✅一买', buy2: '🔁二买', chan: '分型'};
// 生命周期(方案A): try=试买/试卖, ok=一买/一卖(确认), fail=一买✗(失败)
function typeLabel(type, dir, state) {
  if (state === 'try') return dir === 'short' ? '试卖' : '试买';
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
  if (ex.path === '威科夫') {
    const base = dir === 'short' ? '威科夫卖' : '威科夫买';
    if (state === 'try') return (dir === 'short' ? '试·威科夫卖' : '试·威科夫买') + dual;
    return (state === 'fail' ? base + '✗' : base) + dual;
  }
  return typeLabel(type, dir, state) + dual;
}
let signalCache = {};
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
      <td><b>${s.rr}</b></td><td>${s.vol_ratio}x</td><td>${s.status}</td>
    </tr>`).join('');
}
function openChartFromSignal(id) {
  const s = signalCache[id];
  if (s) openChart(s.symbol, s.tf, {entry: s.entry, sl: s.sl, tp: s.tp, extra: s.extra});
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
  loadEquity();
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
    <span class="pill" title="${w.note || ''}" style="cursor:pointer" onclick="openChart('${w.symbol}','15m')">
      ${w.symbol}${w.note ? ' · ' + w.note : ''}
      <span style="color:var(--red);margin-left:6px" onclick="event.stopPropagation();removeWatch('${w.symbol}')">✕</span>
    </span>`).join('') || '<span class="muted">还没关注的币，加一个（会强制纳入监控，即使成交额不达标）</span>';
}
async function addWatch() {
  const sym = $('watch-symbol').value, note = $('watch-note').value;
  if (!sym) { toast('❌ 请填币种'); return; }
  toast('⏳ 加入并回填K线中…');
  const r = await api('/api/watchlist', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({symbol: sym, note})});
  if (r.ok) { toast('✅ 已关注 ' + r.symbol); $('watch-symbol').value = ''; $('watch-note').value = ''; loadWatch(); }
  else toast('❌ ' + (r.error || '失败'));
}
async function removeWatch(sym) {
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
      <td>${PB_STATUS[p.status] || p.status}</td><td class="muted">${p.title || ''}</td>
      <td onclick="event.stopPropagation()">${p.status === 'active'
        ? `<button class="btn" style="padding:2px 8px" onclick="pbAction(${p.id},'cancelled')">取消</button>`
        : `<button class="btn" style="padding:2px 8px;background:var(--panel2)" onclick="pbAction(${p.id},'done')">归档</button>`}</td>
    </tr>`).join('') || '<tr><td colspan="12" class="muted">还没有预演，点「+ 新建预演」记录一个剧本</td></tr>';
}

// ---------- 策略回测 ----------
let btTimer = null;
async function runBacktest() {
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
    const et = snap(s.created_at);
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
  'mode': '运行模式',
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
const BOOL_SETTINGS = ['chan.require_divergence', 'chan.strong_reversal_15m', 'spring.btc_filter'];
async function loadSettings() {
  const s = await api('/api/settings');
  $('settings').innerHTML = Object.entries(s).map(([k, v]) => {
    const label = SETTING_LABELS[k] || k;
    if (k === 'mode') return `<label>${label}<select data-k="${k}">
      <option value="paper" ${v === 'paper' ? 'selected' : ''}>paper 模拟</option>
      <option value="live" ${v === 'live' ? 'selected' : ''}>live 实盘</option></select></label>`;
    if (BOOL_SETTINGS.includes(k)) return `<label>${label}<select data-k="${k}">
      <option value="true" ${v ? 'selected' : ''}>开</option>
      <option value="false" ${!v ? 'selected' : ''}>关</option></select></label>`;
    return `<label>${label}<input data-k="${k}" value="${v}"></label>`;
  }).join('');
}
async function saveSettings() {
  const body = {};
  document.querySelectorAll('#settings [data-k]').forEach(el => body[el.dataset.k] = el.value);
  const r = await api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  toast(r.ok ? '✅ 已保存并热生效' : '❌ ' + (r.error || '保存失败'));
}

// ---------- 实时推送 ----------
function connectWS() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/ws');
  ws.onmessage = e => {
    const {kind, data} = JSON.parse(e.data);
    if (kind === 'signal') {
      toast(`📡 新信号 #${data.id} ${data.symbol} ${data.tf} ${data.direction === 'long' ? '做多' : '做空'} RR=${data.rr}`);
      loadSignals(); loadStatus();
    } else if (kind === 'trade_close') {
      toast(`${data.result === 'tp' ? '🎯' : '🛑'} ${data.symbol} ${data.result.toUpperCase()} ${data.pnl?.toFixed(2)}U`);
      loadTrades(); loadStatus();
    }
  };
  ws.onopen = () => setInterval(() => ws.readyState === 1 && ws.send('ping'), 30000);
  ws.onclose = () => setTimeout(connectWS, 5000);
}

// ---------- 实盘持仓 ----------
async function loadPositions() {
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
loadMacro(); loadWatch(); loadStatus(); loadSignals(); loadTrades(); loadTfStats(); loadPlaybooks(); loadSettings(); loadPositions(); loadBtcChart(); connectWS();
setInterval(loadStatus, 15000);
setInterval(loadPositions, 15000);
setInterval(loadBtcChart, 60000);
setInterval(loadTfStats, 60000);
setInterval(loadPlaybooks, 30000);
