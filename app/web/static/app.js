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
async function loadStatus() {
  const s = await api('/api/status');
  if (s.error) { $('c-engine').textContent = '未运行'; return; }
  $('st-mode').textContent = s.mode === 'paper' ? '🧪 PAPER' : '🔥 LIVE';
  $('st-symbols').textContent = `${s.symbols} 币种`;
  $('st-ws').textContent = s.ws_conns > 0 ? `WS ✓ (${s.ws_last_msg_age_s ?? '?'}s前)` : 'WS ✗';
  const h = Math.floor(s.uptime_s / 3600), m = Math.floor(s.uptime_s % 3600 / 60);
  $('st-uptime').textContent = `运行 ${h}h${m}m`;
  $('c-engine').textContent = s.ws_conns > 0 ? '正常' : '异常';
  $('c-engine-sub').textContent = `评估耗时 ${s.last_eval_ms}ms`;
  renderTrack('c-rr5', s.stats_rr5);
  renderTrack('c-rr25', s.stats_rr25);
  $('c-open').textContent = (s.stats_rr5.open + s.stats_rr25.open);
}
function renderTrack(id, t) {
  const el = $(id);
  el.textContent = `${t.total_pnl >= 0 ? '+' : ''}${t.total_pnl} U`;
  el.className = 'big ' + (t.total_pnl > 0 ? 'green' : t.total_pnl < 0 ? 'red' : '');
  $(id + '-sub').textContent = `${t.closed}单 · 胜率${t.win_rate}% · 期望${t.expectancy_r}R`;
}

// ---------- 信号 ----------
async function loadSignals() {
  const kind = $('f-kind').value;
  const rows = await api('/api/signals?limit=100' + (kind ? '&kind=' + kind : ''));
  $('t-signals').innerHTML = rows.map(s => `
    <tr class="clickable" onclick="openChart('${s.symbol}','${s.tf}')">
      <td>${s.id}</td><td>${fmtT(s.created_at)}</td>
      <td><b>${s.symbol}</b></td><td>${s.tf}</td>
      <td><span class="tag ${s.direction}">${s.direction === 'long' ? '多' : '空'}</span></td>
      <td><span class="tag ${s.kind}">${s.kind === 'primary' ? 'RR5' : 'RR2.5'}</span></td>
      <td>${fmtP(s.entry)}</td><td class="red">${fmtP(s.sl)}</td><td class="green">${fmtP(s.tp)}</td>
      <td><b>${s.rr}</b></td><td>${s.vol_ratio}x</td><td>${s.status}</td>
    </tr>`).join('');
}

// ---------- 交易 ----------
async function loadTrades() {
  const rows = await api('/api/trades?track=' + $('f-track').value);
  $('t-trades').innerHTML = rows.map(t => `
    <tr>
      <td>${t.id}</td><td><b>${t.symbol}</b></td><td>${t.tf}</td>
      <td><span class="tag ${t.direction}">${t.direction === 'long' ? '多' : '空'}</span></td>
      <td>${fmtP(t.entry)}</td><td>${fmtP(t.sl)}</td><td>${fmtP(t.tp)}</td>
      <td>${t.result === 'open' ? '⏳持仓' : t.result === 'tp' ? '🎯止盈' : '🛑止损'}</td>
      <td class="${(t.pnl ?? 0) > 0 ? 'green' : (t.pnl ?? 0) < 0 ? 'red' : ''}">${t.pnl == null ? '–' : t.pnl.toFixed(2)}</td>
      <td>${t.pnl_r == null ? '–' : t.pnl_r.toFixed(2)}</td>
      <td>${fmtT(t.opened_at)}</td>
    </tr>`).join('');
  loadEquity();
}

// ---------- 权益曲线 ----------
let eqChart, eqSeries;
async function loadEquity() {
  const track = $('f-track').value;
  const data = await api('/api/equity?track=' + track);
  $('eq-note').textContent = track + ' 轨 · ' + data.length + ' 个结算点';
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

// ---------- K线弹窗 ----------
let klChart;
async function openChart(symbol, tf) {
  $('modal').classList.add('show');
  $('m-title').textContent = `${symbol} · ${tf}`;
  const d = await api(`/api/klines?symbol=${symbol}&tf=${tf}&limit=300`);
  $('chart').innerHTML = '';
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
  cs.setMarkers(d.signals.filter(s => s.status !== 'error').map(s => ({
    time: s.created_at, position: s.direction === 'long' ? 'belowBar' : 'aboveBar',
    color: s.direction === 'long' ? '#2ecc71' : '#e74c3c',
    shape: s.direction === 'long' ? 'arrowUp' : 'arrowDown',
    text: `#${s.id} RR${s.rr}`,
  })).sort((a, b) => a.time - b.time));
  klChart.timeScale().fitContent();
}
function closeModal() { $('modal').classList.remove('show'); if (klChart) { klChart.remove(); klChart = null; } }

// ---------- 设置 ----------
const SETTING_LABELS = {
  'mode': '运行模式', 'signal.vol_multiplier': '量能放大倍数', 'signal.vol_strong': '强信号倍数',
  'signal.min_rr_primary': '主信号RR门槛', 'signal.min_rr_secondary': '次级RR门槛',
  'signal.sl_buffer_pct': '止损缓冲%', 'signal.cooldown_bars': '冷却K线数',
  'signal.trend_filter': '1h趋势过滤', 'universe.min_quote_volume_24h': '最低24h成交额',
  'risk.account_equity': '账户本金U', 'risk.risk_pct': '单笔风险%',
  'risk.max_positions': '最大持仓数', 'risk.leverage': '杠杆',
};
async function loadSettings() {
  const s = await api('/api/settings');
  $('settings').innerHTML = Object.entries(s).map(([k, v]) => {
    const label = SETTING_LABELS[k] || k;
    if (k === 'mode') return `<label>${label}<select data-k="${k}">
      <option value="paper" ${v === 'paper' ? 'selected' : ''}>paper 模拟</option>
      <option value="live" ${v === 'live' ? 'selected' : ''}>live 实盘</option></select></label>`;
    if (k === 'signal.trend_filter') return `<label>${label}<select data-k="${k}">
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

// ---------- 启动 ----------
loadStatus(); loadSignals(); loadTrades(); loadSettings(); connectWS();
setInterval(loadStatus, 15000);
