/* 订单流页面（/flow.html）
 *
 * 四个区块各自取数、各自失败、各自刷新：
 *   A 状态卡      GET /api/flow/summary   60s
 *   B 深度热力图  GET /api/flow/depth     30s
 *   C 清算热力图  GET /api/flow/liqmap    5min（估算，模型推算，不是交易所数据）
 *   D CVD 对比    GET /api/flow/cvd       60s
 * 页面只读这些公共接口；标签页隐藏时停止刷新，回到前台时补拉。
 * 热力图渲染器 HeatChart 只实现一次，B、C 共用。
 */
(function () {
'use strict';

// ====================== 小工具 ======================
const $ = id => document.getElementById(id);
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const isNum = x => typeof x === 'number' && isFinite(x);
const pad2 = n => (n < 10 ? '0' : '') + n;
const lastOf = a => (Array.isArray(a) && a.length ? a[a.length - 1] : undefined);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function fmtPrice(p, digits) {
  if (!isNum(p)) return '–';
  const d = digits != null ? digits : (Math.abs(p) >= 1000 ? 0 : 2);
  return p.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
// BTC 数量：尽量短（12.3 / 420 / 2.26K / 86.2K）
function fmtBtc(x, signed) {
  if (!isNum(x)) return '–';
  const a = Math.abs(x);
  let s;
  if (a >= 1e6) s = (a / 1e6).toFixed(2) + 'M';
  else if (a >= 1e4) s = (a / 1e3).toFixed(1) + 'K';
  else if (a >= 1e3) s = (a / 1e3).toFixed(2) + 'K';
  else if (a >= 100) s = a.toFixed(0);
  else s = a.toFixed(1);
  return (x < 0 ? '-' : (signed && x > 0 ? '+' : '')) + s;
}
function fmtPct(x, d) {
  if (!isNum(x)) return '–';
  return (x > 0 ? '+' : '') + x.toFixed(d == null ? 1 : d) + '%';
}
const trimNum = x => String(Math.round(x * 100) / 100);
function fmtClock(ms) {
  const t = new Date(ms);
  return pad2(t.getHours()) + ':' + pad2(t.getMinutes()) + ':' + pad2(t.getSeconds());
}
function fmtStamp(ms) {
  const t = new Date(ms);
  return pad2(t.getMonth() + 1) + '-' + pad2(t.getDate()) + ' ' + pad2(t.getHours()) + ':' + pad2(t.getMinutes());
}
function fmtDur(min) {
  if (!isNum(min) || min <= 0) return '0 分钟';
  if (min < 90) return Math.round(min) + ' 分钟';
  if (min < 48 * 60) return (min / 60).toFixed(1) + ' 小时';
  return (min / 1440).toFixed(1) + ' 天';
}
function fmtAgo(ms) {
  if (ms < 0) ms = 0;
  if (ms < 90e3) return Math.round(ms / 1e3) + ' 秒前';
  if (ms < 90 * 60e3) return Math.round(ms / 60e3) + ' 分钟前';
  return (ms / 3600e3).toFixed(1) + ' 小时前';
}
const signCls = x => (x > 0 ? 'up' : x < 0 ? 'down' : 'flat');

// 本地偏好（记住上次选的窗口/数据源），隐私模式下 localStorage 可能抛错，全部兜住
const store = {
  get(k, def) {
    try { const v = localStorage.getItem('flow.' + k); return v == null ? def : JSON.parse(v); }
    catch (e) { return def; }
  },
  set(k, v) { try { localStorage.setItem('flow.' + k, JSON.stringify(v)); } catch (e) { /* 忽略 */ } },
};

// 画布用的颜色取自 flow.css 的变量（带兜底），改主题只需改 CSS
const rootCss = getComputedStyle(document.documentElement);
const cssVar = (name, def) => (rootCss.getPropertyValue(name) || '').trim() || def;
const C = {
  border: cssVar('--fl-border', '#2a3344'), text: cssVar('--fl-text', '#d8dee9'), muted: cssVar('--fl-muted', '#7a869c'),
  green: cssVar('--fl-green', '#2ecc71'), red: cssVar('--fl-red', '#e74c3c'), accent: cssVar('--fl-accent', '#4f8ef7'),
};
const FONT = '"Segoe UI", system-ui, -apple-system, sans-serif';

// ====================== 取数调度 ======================
// 每个区块一个 loader：独立请求、独立报错、失败退避重试、隐藏时暂停。
const loaders = [];
function touchClock() { $('hl-time').textContent = fmtClock(Date.now()); }

function createLoader(cfg) {
  const root = cfg.root || null;
  const errEl = root ? root.querySelector('[data-err]') : null;
  const st = {timer: 0, ctrl: null, seq: 0, lastOk: 0, fails: 0, busy: false};

  function setErr(msg) {
    if (errEl) { errEl.textContent = msg; errEl.title = msg; }
  }
  function schedule(ms) {
    clearTimeout(st.timer);
    st.timer = 0;
    if (!document.hidden) st.timer = setTimeout(load, ms);
  }
  async function load() {
    clearTimeout(st.timer);
    st.timer = 0;
    if (document.hidden) return;
    if (st.ctrl) st.ctrl.abort();              // 参数变了：作废上一个还没回来的请求
    const ctrl = st.ctrl = new AbortController();
    const seq = ++st.seq;
    let timedOut = false;
    const killer = setTimeout(() => { timedOut = true; ctrl.abort(); }, cfg.timeout || 25000);
    st.busy = true;
    if (root) root.classList.add('is-busy');
    let why = '';
    try {
      const r = await fetch(cfg.url(), {signal: ctrl.signal, cache: 'no-store', credentials: 'same-origin'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      if (seq !== st.seq) return;
      try { cfg.render(data); }
      catch (e) { console.error('[flow] render ' + cfg.name, e); why = '数据异常'; }
    } catch (e) {
      if (seq !== st.seq) return;              // 被新请求取代，不算失败
      why = timedOut ? '超时' : /^HTTP/.test(e && e.message) ? e.message
        : e instanceof SyntaxError ? '数据异常' : '连接失败';
    } finally {
      clearTimeout(killer);
    }
    if (seq !== st.seq) return;
    st.busy = false;
    if (root) root.classList.remove('is-busy');
    if (!why) {
      st.lastOk = Date.now();
      st.fails = 0;
      setErr('');
      if (!cfg.silent) touchClock();
      schedule(cfg.interval);
    } else {
      st.fails++;
      const delay = Math.min(cfg.interval, 60000, 5000 * Math.pow(2, st.fails - 1));
      setErr(why + ' · ' + Math.round(delay / 1000) + 's 后重试');
      if (cfg.onFail) cfg.onFail(why, st.lastOk > 0);    // 第二个参数：之前是否成功过（成功过就保留旧图）
      schedule(delay);
    }
  }
  const api = {
    load,
    pause() { clearTimeout(st.timer); st.timer = 0; },
    resume() {
      if (st.timer || st.busy) return;
      const due = st.lastOk + cfg.interval - Date.now();
      if (due <= 0) load(); else schedule(due);
    },
  };
  loaders.push(api);
  return api;
}

document.addEventListener('visibilitychange', () => {
  for (const l of loaders) { if (document.hidden) l.pause(); else l.resume(); }
});
window.addEventListener('pageshow', () => { for (const l of loaders) l.resume(); });

// 分段按钮（单选）
function bindSeg(box, value, onChange) {
  const set = v => box.querySelectorAll('button').forEach(b => {
    const on = b.dataset.v === String(v);
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  set(value);
  box.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b || b.classList.contains('on')) return;
    set(b.dataset.v);
    onChange(b.dataset.v);
  });
}

// ====================== 热力图渲染器（B、C 共用） ======================
// 色带：暗 → 蓝 → 青 → 黄 → 白。位置是"色带位置"，数值经 GAMMA 压缩后映射上去，
// 让中等大小的挂单也看得见，而不是只有最大的几堵墙发亮。
const RAMP = [
  [0.00, [12, 20, 48]],
  [0.18, [20, 46, 120]],
  [0.40, [34, 104, 224]],
  [0.60, [28, 200, 236]],
  [0.80, [246, 226, 72]],
  [1.00, [255, 255, 255]],
];
const GAMMA = 0.6;
const LUT = new Uint8Array(256 * 3);          // 色带位置 0..255 → RGB
(function () {
  for (let i = 0; i < 256; i++) {
    const p = i / 255;
    let k = 0;
    while (k < RAMP.length - 2 && p > RAMP[k + 1][0]) k++;
    const a = RAMP[k], b = RAMP[k + 1];
    const f = clamp((p - a[0]) / (b[0] - a[0]), 0, 1);
    for (let c = 0; c < 3; c++) LUT[i * 3 + c] = Math.round(a[1][c] + (b[1][c] - a[1][c]) * f);
  }
})();
const QN = 1024;                                // 数值量化档数（免得每格都算一次 pow）
const QLUT = new Uint8Array(QN);
for (let q = 0; q < QN; q++) QLUT[q] = Math.round(Math.pow(q / (QN - 1), GAMMA) * 255);

// 图例色条按"数值线性"画：色带位置 p 对应数值比例 p^(1/GAMMA)
const RAMP_CSS = 'linear-gradient(90deg,' + RAMP.map(s =>
  'rgb(' + s[1].join(',') + ') ' + (Math.pow(s[0], 1 / GAMMA) * 100).toFixed(1) + '%').join(',') + ')';
function setLegend(box, zmax) {
  if (!box.firstChild) {
    const ramp = el('span', 'fl-ramp');
    ramp.style.background = RAMP_CSS;
    box.append(el('span', '', '0'), ramp, el('span', 'fl-legend-max'));
  }
  box.lastChild.textContent = zmax > 0 ? '≥' + fmtBtc(zmax) + ' BTC' : '–';
}

// 把时间列切成连续段：相邻间隔超过 2.5 倍中位间隔视为断档（录制中断），断档处留空不拉伸
function segmentsOf(times) {
  const T = times.length;
  if (T === 0) return {segs: [], step: 0};
  if (T === 1) return {segs: [[0, 1]], step: 30000};
  const diffs = [];
  for (let i = 0; i < T - 1; i++) diffs.push(times[i + 1] - times[i]);
  const sorted = diffs.slice().sort((a, b) => a - b);
  const step = sorted[sorted.length >> 1] || 1;
  const segs = [];
  let s = 0;
  for (let i = 0; i < T - 1; i++) {
    if (diffs[i] > step * 2.5) { segs.push([s, i + 1]); s = i + 1; }
  }
  segs.push([s, T]);
  return {segs, step};
}

function niceStep(range, maxTicks) {
  const raw = range / Math.max(1, maxTicks);
  if (!(raw > 0)) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= raw) return m * pow;
  return 10 * pow;
}
const T_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 240, 360, 720, 1440, 2880, 4320, 10080, 20160].map(m => m * 60000);
function timeTicks(t0, t1, maxTicks) {
  let step = T_STEPS[T_STEPS.length - 1];
  for (const s of T_STEPS) { if ((t1 - t0) / s <= maxTicks) { step = s; break; } }
  const off = new Date(t0).getTimezoneOffset() * 60000;    // 刻度对齐本地整点/零点
  const out = [];
  for (let t = Math.ceil((t0 - off) / step) * step + off; t <= t1; t += step) out.push(t);
  return {ticks: out, step};
}
function fmtTick(ms, step) {
  const t = new Date(ms);
  const md = pad2(t.getMonth() + 1) + '-' + pad2(t.getDate());
  if (step >= 86400000 || (t.getHours() === 0 && t.getMinutes() === 0)) return md;
  return pad2(t.getHours()) + ':' + pad2(t.getMinutes());
}

class HeatChart {
  /* opt:
   *   zooms     纵轴范围档位，0=全幅，其余为相对当前价的 ±%
   *   zoomKey   记住档位用的本地键
   *   sizeLabel 提示里"格子数值"的名字；aLabel/bLabel 右侧轮廓两种颜色的名字
   */
  constructor(root, opt) {
    this.root = root;
    this.opt = Object.assign({zooms: [0], zoomKey: '', sizeLabel: '挂单', aLabel: '买', bLabel: '卖'}, opt);
    this.base = el('canvas', 'fl-heat-base');
    this.over = el('canvas', 'fl-heat-over');
    this.msg = el('div', 'fl-heat-msg');
    this.msg.hidden = true;
    this.tip = el('div', 'fl-tip');
    this.tip.hidden = true;
    this.zoomBtn = el('button', 'fl-zoom');
    this.zoomBtn.type = 'button';
    this.zoomBtn.title = '切换纵轴范围';
    root.append(this.base, this.over, this.msg, this.tip, this.zoomBtn);

    this.off = document.createElement('canvas');   // 矩阵分辨率的离屏位图，一格一像素
    this.d = null;
    this.segs = [];
    this.segX = [];
    this.step = 0;
    this.L = null;
    this.view = null;
    this.cross = null;
    this.down = false;
    this.raf = 0;
    this.hideTimer = 0;
    this.dpr = 1;

    const z = this.opt.zoomKey ? store.get(this.opt.zoomKey, 0) : 0;
    this.zoom = this.opt.zooms.includes(z) ? z : this.opt.zooms[0];
    this.syncZoom();
    this.zoomBtn.addEventListener('click', () => {
      const zs = this.opt.zooms;
      this.zoom = zs[(zs.indexOf(this.zoom) + 1) % zs.length];
      if (this.opt.zoomKey) store.set(this.opt.zoomKey, this.zoom);
      this.syncZoom();
      this.queue();
    });

    const ov = this.over;
    // 鼠标：悬停即显示。触屏：点一下或横向拖动才显示；竖向滑动交还给页面滚动
    // (touch-action: pan-y，浏览器接管滚动时会发 pointercancel，此时收起十字线)。
    ov.addEventListener('pointerdown', e => {
      this.down = true;
      if (e.pointerType === 'mouse') this.setCross(e);
    });
    ov.addEventListener('pointermove', e => { if (e.pointerType === 'mouse' || this.down) this.setCross(e); });
    ov.addEventListener('pointerup', e => {
      if (e.pointerType !== 'mouse') {
        if (this.down) this.setCross(e);
        this.hideLater();
      }
      this.down = false;
    });
    ov.addEventListener('pointercancel', e => {
      this.down = false;
      if (e.pointerType !== 'mouse') this.clearCross();
    });
    ov.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') this.clearCross(); });

    if (window.ResizeObserver) new ResizeObserver(() => this.queue()).observe(root);
    window.addEventListener('resize', () => this.queue());      // 旋转屏幕 / 缩放导致 DPR 变化
    this.queue();
  }

  syncZoom() {
    this.zoomBtn.textContent = this.zoom > 0 ? '纵轴 ±' + this.zoom + '%' : '纵轴 全幅';
  }

  setMessage(text) {
    this.msg.textContent = text || '';
    this.msg.hidden = !text;
  }

  /* data: {times[], line[], z[T][rows], rows, p0, bucket, zmax, ref,
   *        profile: {a[], b[], prices[]?} | null, markers: [{price, color}]} */
  setData(data) {
    const T = Array.isArray(data.times) ? data.times.length : 0;
    const ok = T > 0 && data.rows > 0 && data.bucket > 0 && isNum(data.p0) && Array.isArray(data.z);
    this.d = ok ? data : null;
    if (ok) {
      const sg = segmentsOf(data.times);
      this.segs = sg.segs;
      this.step = sg.step;
      this.buildImage();
    }
    this.queue();
  }

  // 把矩阵画进离屏位图：x=时间列，y=价格行（第 0 行在最下）。0 值保持透明，露出底色。
  buildImage() {
    const d = this.d;
    const T = d.times.length, R = d.rows;
    this.off.width = T;
    this.off.height = R;
    const ctx = this.off.getContext('2d');
    const img = ctx.createImageData(T, R);
    const px = img.data;
    let zmax = d.zmax;
    if (!(zmax > 0)) {                               // 服务端没给上限时自己取最大值兜底
      zmax = 0;
      for (let t = 0; t < T; t++) { const col = d.z[t] || []; for (let i = 0; i < R; i++) if (col[i] > zmax) zmax = col[i]; }
      if (!(zmax > 0)) zmax = 1;
    }
    const k = (QN - 1) / zmax;
    for (let t = 0; t < T; t++) {
      const col = d.z[t];
      if (!col) continue;
      for (let i = 0; i < R; i++) {
        const v = col[i];
        if (!(v > 0)) continue;
        let q = v * k;
        q = q >= QN - 1 ? QN - 1 : q | 0;
        const li = QLUT[q] * 3, o = ((R - 1 - i) * T + t) * 4;
        px[o] = LUT[li]; px[o + 1] = LUT[li + 1]; px[o + 2] = LUT[li + 2]; px[o + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

  queue() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => { this.raf = 0; this.render(); });
  }

  render() {
    const W = this.root.clientWidth, H = this.root.clientHeight;
    if (W < 40 || H < 40) return;                    // 容器不可见
    const dpr = this.dpr = clamp(window.devicePixelRatio || 1, 1, 3);
    const pw = Math.round(W * dpr), ph = Math.round(H * dpr);
    for (const c of [this.base, this.over]) {
      if (c.width !== pw || c.height !== ph) { c.width = pw; c.height = ph; }
    }
    const narrow = W < 520;
    const axisW = narrow ? 46 : 60;
    const profW = narrow ? clamp(Math.round(W * 0.17), 46, 64) : clamp(Math.round(W * 0.15), 90, 170);
    const L = this.L = {W, H, y0: 6, pw: W - axisW - profW, ph: H - 6 - 18,
      axisX: W - axisW - profW, axisW, profX: W - profW, profW};
    this.msg.style.width = L.pw + 'px';

    const ctx = this.base.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = C.border;
    ctx.fillRect(L.axisX, L.y0, 1, L.ph);

    const d = this.d;
    this.segX = [];
    if (!d) { this.view = null; this.drawOverlay(); return; }

    // ---- 纵轴范围 ----
    const gLo = d.p0, gHi = d.p0 + d.rows * d.bucket;
    let vLo = gLo, vHi = gHi;
    if (this.zoom > 0 && isNum(d.ref)) {
      const half = d.ref * this.zoom / 100;
      vLo = d.ref - half; vHi = d.ref + half;
    }
    this.view = {lo: vLo, hi: vHi};
    const Y = p => L.y0 + (vHi - p) / (vHi - vLo) * L.ph;

    // ---- 时间映射：全局线性，段内列等宽 ----
    const T = d.times.length;
    const t0 = d.times[0], t1 = d.times[T - 1] + this.step, span = (t1 - t0) || 1;
    const X = t => (t - t0) / span * L.pw;
    if (T >= 2) {
      for (const sg of this.segs) {
        this.segX.push({s: sg[0], e: sg[1], x0: X(d.times[sg[0]]), x1: X(d.times[sg[1] - 1] + this.step)});
      }
    }

    // ---- 热力格 + 价格线 ----
    if (T >= 2) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, L.y0, L.pw, L.ph);
      ctx.clip();
      const a = Math.max(vLo, gLo), b = Math.min(vHi, gHi);
      if (b > a) {
        const sy = (gHi - b) / d.bucket, sh = (b - a) / d.bucket;
        const dy = Y(b), dh = Y(a) - Y(b);
        ctx.imageSmoothingEnabled = T > L.pw * dpr * 1.25;     // 只有列数多于像素时才平滑，否则保持格子锐利
        for (const g of this.segX) ctx.drawImage(this.off, g.s, sy, g.e - g.s, sh, g.x0, dy, g.x1 - g.x0, dh);
      }
      const stroke = (color, width) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        ctx.lineJoin = 'round';
        ctx.beginPath();
        for (const g of this.segX) {
          const cw = (g.x1 - g.x0) / (g.e - g.s);
          let pen = false;
          for (let t = g.s; t < g.e; t++) {
            const v = d.line ? d.line[t] : null;
            if (!isNum(v)) { pen = false; continue; }
            const x = g.x0 + (t - g.s + 0.5) * cw, y = Y(v);
            if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); pen = true; }
          }
        }
        ctx.stroke();
      };
      stroke('rgba(0,0,0,.65)', 3.4);
      stroke('#ffffff', 1.3);
      ctx.restore();
    }

    // ---- 价格刻度 ----
    const fs = narrow ? 10 : 11;
    ctx.font = fs + 'px ' + FONT;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    const pStep = niceStep(vHi - vLo, Math.max(2, Math.floor(L.ph / 40)));
    const pDigits = pStep < 1 ? 2 : 0;
    for (let p = Math.ceil(vLo / pStep) * pStep; p <= vHi; p += pStep) {
      const y = Y(p);
      if (y < L.y0 + 5 || y > L.y0 + L.ph - 3) continue;
      ctx.fillStyle = 'rgba(255,255,255,.055)';
      ctx.fillRect(0, Math.round(y), L.pw, 1);
      ctx.fillStyle = C.muted;
      ctx.fillText(fmtPrice(p, pDigits), L.axisX + 6, y);
    }

    // ---- 时间刻度 ----
    if (T >= 2) {
      const tk = timeTicks(t0, t1, Math.max(2, Math.floor(L.pw / 56)));
      ctx.textAlign = 'center';
      ctx.textBaseline = 'alphabetic';
      ctx.fillStyle = C.muted;
      for (const t of tk.ticks) {
        const x = X(t);
        if (x < 16 || x > L.pw - 16) continue;
        ctx.fillText(fmtTick(t, tk.step), x, H - 5);
      }
    }

    // ---- 右侧当前轮廓（横向条）----
    const pr = d.profile;
    if (pr && Array.isArray(pr.a) && Array.isArray(pr.b)) {
      const n = Math.min(d.rows, Math.max(pr.a.length, pr.b.length));
      const lowOf = i => (pr.prices && isNum(pr.prices[i]) ? pr.prices[i] : d.p0 + i * d.bucket);
      let mx = 0;
      for (let i = 0; i < n; i++) {
        const lo = lowOf(i);
        if (lo + d.bucket <= vLo || lo >= vHi) continue;
        const s = (pr.a[i] > 0 ? pr.a[i] : 0) + (pr.b[i] > 0 ? pr.b[i] : 0);
        if (s > mx) mx = s;
      }
      if (mx > 0) {
        const maxLen = L.profW - 7;
        const rowH = d.bucket / (vHi - vLo) * L.ph;
        const bh = Math.max(1, rowH - (rowH >= 4 ? 1 : 0));
        ctx.save();
        ctx.beginPath();
        ctx.rect(L.profX, L.y0, L.profW, L.ph);
        ctx.clip();
        for (let i = 0; i < n; i++) {
          const av = pr.a[i] > 0 ? pr.a[i] : 0, bv = pr.b[i] > 0 ? pr.b[i] : 0;
          if (!av && !bv) continue;
          const lo = lowOf(i);
          if (lo + d.bucket <= vLo || lo >= vHi) continue;
          const y = Y(lo + d.bucket);
          const la = av / mx * maxLen, lb = bv / mx * maxLen;
          if (av) { ctx.fillStyle = C.green; ctx.fillRect(L.profX + 3, y, Math.max(1, la), bh); }
          if (bv) { ctx.fillStyle = C.red; ctx.fillRect(L.profX + 3 + (av ? la : 0), y, Math.max(1, lb), bh); }
        }
        ctx.restore();
        ctx.textAlign = 'right';
        ctx.textBaseline = 'alphabetic';
        ctx.fillStyle = C.muted;
        ctx.fillText('峰 ' + fmtBtc(mx), W - 4, H - 5);
      }
    }

    // ---- 重点价位标记（清算簇）----
    if (Array.isArray(d.markers)) {
      for (const m of d.markers) {
        if (!isNum(m.price)) continue;
        const y = Y(m.price);
        if (y < L.y0 || y > L.y0 + L.ph) continue;
        ctx.fillStyle = m.color;
        ctx.beginPath();
        ctx.moveTo(L.axisX + 1, y);
        ctx.lineTo(L.axisX + 6, y - 4);
        ctx.lineTo(L.axisX + 6, y + 4);
        ctx.closePath();
        ctx.fill();
      }
    }

    // ---- 当前价标签 ----
    if (isNum(d.ref)) {
      const y = Y(d.ref);
      if (y >= L.y0 && y <= L.y0 + L.ph) {
        ctx.strokeStyle = 'rgba(255,255,255,.4)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(L.profX, Math.round(y) + 0.5);
        ctx.lineTo(W, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        this.tag(ctx, y, fmtPrice(d.ref, pDigits), '#e8edf5', '#0b0f16', fs, true);
      }
    }
    this.drawOverlay();
  }

  // 坐标轴条上的价格标签
  tag(ctx, y, text, bg, fg, fs, bold) {
    const L = this.L;
    const yy = clamp(y, L.y0 + 8, L.y0 + L.ph - 8);
    ctx.fillStyle = bg;
    ctx.fillRect(L.axisX + 1, yy - 8, L.axisW - 2, 16);
    ctx.fillStyle = fg;
    ctx.font = (bold ? '600 ' : '') + fs + 'px ' + FONT;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, L.axisX + 6, yy + 0.5);
    ctx.font = fs + 'px ' + FONT;
  }

  colAt(x) {
    for (const g of this.segX) {
      if (x >= g.x0 && x < g.x1) {
        const n = g.e - g.s;
        return g.s + Math.min(n - 1, Math.floor((x - g.x0) / (g.x1 - g.x0) * n));
      }
    }
    return -1;
  }

  setCross(e) {
    clearTimeout(this.hideTimer);
    const r = this.root.getBoundingClientRect();
    this.cross = {x: e.clientX - r.left, y: e.clientY - r.top};
    this.drawOverlay();
  }
  clearCross() {
    clearTimeout(this.hideTimer);
    this.cross = null;
    this.drawOverlay();
  }
  hideLater() {                                      // 触屏：手指离开后十字线再留 8 秒，够读完提示
    clearTimeout(this.hideTimer);
    this.hideTimer = setTimeout(() => this.clearCross(), 8000);
  }

  drawOverlay() {
    const L = this.L;
    if (!L) return;
    const ctx = this.over.getContext('2d');
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, L.W, L.H);
    const d = this.d, c = this.cross, v = this.view;
    if (!d || !c || !v || c.y < L.y0 || c.y > L.y0 + L.ph || c.x < 0 || c.x > L.W) {
      this.tip.hidden = true;
      return;
    }
    const price = v.hi - (c.y - L.y0) / L.ph * (v.hi - v.lo);
    const row = Math.floor((price - d.p0) / d.bucket);
    const inGrid = row >= 0 && row < d.rows;
    const lo = d.p0 + row * d.bucket;
    const inPlot = c.x < L.pw;
    const col = inPlot ? this.colAt(c.x) : -1;

    ctx.strokeStyle = 'rgba(255,255,255,.5)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    const yy = Math.round(c.y) + 0.5;
    ctx.moveTo(0, yy);
    ctx.lineTo(L.W, yy);
    if (inPlot) {
      const xx = Math.round(c.x) + 0.5;
      ctx.moveTo(xx, L.y0);
      ctx.lineTo(xx, L.y0 + L.ph);
    }
    ctx.stroke();
    ctx.setLineDash([]);
    const digits = d.bucket < 1 ? 2 : 0;
    this.tag(ctx, c.y, fmtPrice(price, digits), C.accent, '#ffffff', L.W < 520 ? 10 : 11, false);

    // 提示框：数值 / 价格带 / 时间
    const tip = this.tip;
    tip.textContent = '';
    const band = inGrid ? fmtPrice(lo, digits) + ' – ' + fmtPrice(lo + d.bucket, digits) : fmtPrice(price, digits);
    if (c.x >= L.axisX) {
      const pr = d.profile;
      const av = pr && inGrid && pr.a ? pr.a[row] || 0 : 0, bv = pr && inGrid && pr.b ? pr.b[row] || 0 : 0;
      tip.append(el('b', '', band),
        el('span', '', this.opt.aLabel + ' ' + fmtBtc(av) + ' · ' + this.opt.bLabel + ' ' + fmtBtc(bv) + ' BTC'));
    } else if (col >= 0) {
      const z = inGrid && d.z[col] ? d.z[col][row] || 0 : 0;
      tip.append(el('b', '', this.opt.sizeLabel + ' ' + fmtBtc(z) + ' BTC'), el('span', '', band),
        el('span', '', fmtStamp(d.times[col]) + (this.step < 60000 ? ':' + pad2(new Date(d.times[col]).getSeconds()) : '')));
    } else {
      tip.append(el('b', '', band), el('span', '', '此处无数据'));
    }
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = c.x + 12;
    if (left + tw > L.W - 4) left = c.x - 12 - tw;
    let top = c.y - th - 10;
    if (top < 4) top = c.y + 14;
    tip.style.left = clamp(left, 4, Math.max(4, L.W - tw - 4)) + 'px';
    tip.style.top = clamp(top, 4, Math.max(4, L.H - th - 4)) + 'px';
  }
}

// ====================== A. 状态卡 ======================
const DIV_STATE = {
  bear_div: {label: '顶背离', glyph: '顶', cls: 'warn'},
  bull_div: {label: '底背离', glyph: '底', cls: 'warn'},
  confirm_up: {label: '同向上涨', glyph: '↑', cls: 'up'},
  confirm_down: {label: '同向下跌', glyph: '↓', cls: 'down'},
  flat: {label: '横盘', glyph: '–', cls: 'flat'},
  insufficient: {label: '数据不足', glyph: '…', cls: 'flat'},
  // 合约 CVD 对 现货 CVD（/api/flow/cvd 的 fut_vs_spot）：两边反向才算背离
  both_up: {label: '同步买入', glyph: '↑', cls: 'up'},
  both_down: {label: '同步卖出', glyph: '↓', cls: 'down'},
  fut_up_spot_down: {label: '合约买 现货卖', glyph: '!', cls: 'warn'},
  fut_down_spot_up: {label: '合约卖 现货买', glyph: '!', cls: 'warn'},
  fut_only_up: {label: '仅合约买', glyph: '↑', cls: 'up'},
  fut_only_down: {label: '仅合约卖', glyph: '↓', cls: 'down'},
  spot_only_up: {label: '仅现货买', glyph: '↑', cls: 'up'},
  spot_only_down: {label: '仅现货卖', glyph: '↓', cls: 'down'},
};
// 后端给了没见过的 state 时：从 state/text 猜颜色，标签取 text 的第一小段
function divInfo(d) {
  if (!d || typeof d !== 'object') return {label: '–', glyph: '–', cls: 'flat'};
  if (DIV_STATE[d.state]) return DIV_STATE[d.state];
  const s = String(d.state || ''), text = String(d.text || '');
  let cls = 'flat';
  if (/div|背离|lead|mismatch|oppos/i.test(s) || /背离/.test(text)) cls = 'warn';
  else if (/up|bull|buy/i.test(s)) cls = 'up';
  else if (/down|bear|sell/i.test(s)) cls = 'down';
  const short = text.replace(/[^一-龥A-Za-z0-9+\-.%、，：:—\s]/g, '').trim().split(/[：:，,—(（]/)[0].trim();
  return {label: (short || s || '–').slice(0, 8), glyph: '?', cls};
}
const QUAD = {
  new_longs: {label: '新多进场', cls: 'up'},
  short_cover: {label: '空头回补', cls: 'warn'},
  new_shorts: {label: '新空进场', cls: 'down'},
  long_squeeze: {label: '多头平仓', cls: 'warn'},
  flat: {label: '横盘', cls: 'flat'},
};

function setChg(box, v) {
  const b = box.querySelector('b');
  b.textContent = fmtPct(v, 2);
  b.className = signCls(v);
}

function renderSummary(p) {
  $('s-price').textContent = fmtPrice(p.price, 0);
  setChg($('s-chg24'), p.chg_24h);
  setChg($('s-chg7'), p.chg_7d);

  // CVD × 价格：主值看 24h，那排小格是 6h/24h/72h
  const divs = Array.isArray(p.divs) ? p.divs : [];
  const d24 = divs.find(x => x && x.bars === 24) || divs[1] || divs[0];
  const i24 = divInfo(d24);
  const main = $('s-div');
  main.textContent = i24.label;
  main.className = 'fl-stat-main ' + i24.cls;
  $('st-cvd').classList.toggle('is-warn', i24.cls === 'warn');
  $('st-cvd').title = d24 && d24.text ? d24.text : '';
  const pills = $('s-divs');
  pills.textContent = '';
  for (const d of divs.slice(0, 3)) {
    const info = divInfo(d);
    const s = el('span', info.cls);
    s.append(el('b', '', info.glyph), document.createTextNode((d && d.bars ? d.bars : '?') + 'h'));
    if (d && d.text) s.title = d.text;
    pills.append(s);
  }
  $('s-cvd').textContent = '24h净 ' + fmtBtc(p.cvd_24h, true);

  // 资金费：近若干期均值 + 按天小柱
  const f = p.funding || {};
  const fm = $('s-fund');
  fm.textContent = isNum(f.avg) ? (f.avg > 0 ? '+' : '') + (f.avg * 100).toFixed(4) + '%' : '–';
  const high = /_high$/.test(String(f.state || ''));
  fm.className = 'fl-stat-main ' + (high ? 'warn' : '');
  $('st-fund').classList.toggle('is-warn', high);
  const label = String(f.label || f.text || '').replace(/[(（].*?[)）]/g, '').split('/')[0].trim();
  $('s-fund-label').textContent = label || ' ';
  $('st-fund').title = (f.label || '') + (isNum(f.neg) && isNum(f.look) ? ' · 近' + f.look + '期中 ' + f.neg + ' 期为负' : '');
  drawFundBars($('s-fund-bars'), Array.isArray(p.funding_days) ? p.funding_days : []);

  // OI 四象限：点的横坐标=OI 24h 变化，纵坐标=价格 24h 变化（各按 ±5% 封顶）
  const q = QUAD[p.quadrant] || {
    label: String(p.quadrant_text || '–').split('→').pop().split(/[，,(（]/)[0].trim().slice(0, 8), cls: 'flat'};
  const qm = $('s-quad');
  qm.textContent = q.label;
  qm.className = 'fl-stat-main ' + q.cls;
  $('st-oi').title = p.quadrant_text || '';
  document.querySelectorAll('#st-oi .q').forEach(r => r.classList.toggle('on', r.dataset.q === p.quadrant));
  const dot = $('s-quad-dot');
  dot.setAttribute('cx', (38 + clamp((p.oi_24h || 0) / 5, -1, 1) * 33).toFixed(1));
  dot.setAttribute('cy', (22 - clamp((p.chg_24h || 0) / 5, -1, 1) * 18).toFixed(1));
  $('s-oi').textContent = 'OI 24h ' + fmtPct(p.oi_24h, 1);

  $('s-summary').textContent = p.summary || '';
  $('s-summary').title = p.summary || '';
}

$('s-summary').addEventListener('click', e => e.currentTarget.classList.toggle('open'));

function drawFundBars(box, days) {
  const NS = 'http://www.w3.org/2000/svg';
  box.textContent = '';
  const rows = days.filter(r => Array.isArray(r) && isNum(r[1])).slice(-7);
  if (!rows.length) return;
  const svg = document.createElementNS(NS, 'svg');
  const W = 100, H = 30, mid = H / 2;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('preserveAspectRatio', 'none');
  const mx = Math.max.apply(null, rows.map(r => Math.abs(r[1]))) || 1;
  const bw = W / rows.length;
  const base = document.createElementNS(NS, 'rect');
  base.setAttribute('x', 0); base.setAttribute('y', mid - 0.25);
  base.setAttribute('width', W); base.setAttribute('height', 0.5);
  base.setAttribute('fill', C.border);
  svg.append(base);
  rows.forEach((r, i) => {
    const h = Math.max(1, Math.abs(r[1]) / mx * (mid - 1));
    const rect = document.createElementNS(NS, 'rect');
    rect.setAttribute('x', (i * bw + bw * 0.15).toFixed(2));
    rect.setAttribute('width', (bw * 0.7).toFixed(2));
    rect.setAttribute('y', (r[1] >= 0 ? mid - h : mid).toFixed(2));
    rect.setAttribute('height', h.toFixed(2));
    rect.setAttribute('rx', 1);
    rect.setAttribute('fill', r[1] >= 0 ? C.green : C.red);
    const t = document.createElementNS(NS, 'title');
    t.textContent = String(r[0]).slice(5) + '  ' + (r[1] * 100).toFixed(4) + '%';
    rect.append(t);
    svg.append(rect);
  });
  box.append(svg);
}

// ====================== B. 深度热力图 ======================
const SRC_ORDER = ['binance', 'bybit', 'okx', 'binance_spot'];
const depth = {hours: 6, src: 'all'};
(function () {
  const h = store.get('d.hours', 6);
  if ([1, 6, 24, 72, 168].includes(h)) depth.hours = h;
  const s = store.get('d.src', 'all');
  if (Array.isArray(s)) {
    const ok = SRC_ORDER.filter(x => s.includes(x));
    if (ok.length) depth.src = ok;
  }
})();
const depthChart = new HeatChart($('d-chart'), {
  zooms: [0, 2, 1, 0.5, 0.25], zoomKey: 'd.zoom', sizeLabel: '挂单', aLabel: '买', bLabel: '卖'});
depthChart.setMessage('加载中…');
// 后端 flow.enabled=false 时（由 /api/flow/health 得知）：两张热力图直说"未启用"，别写"随录制逐步填充"
const OFF_TEXT = '订单流未启用';
let flowOff = false;

function depthUrl() {
  const src = depth.src === 'all' ? 'all' : depth.src.join(',');
  const cols = window.innerWidth < 700 ? 240 : 360;      // 手机图窄，少要点列，省流量
  return '/api/flow/depth?hours=' + depth.hours + '&src=' + src + '&cols=' + cols;
}
function syncSrcButtons(included) {
  $('d-srcs').querySelectorAll('button').forEach(b => {
    const s = b.dataset.src;
    const on = s === 'all' ? depth.src === 'all' : depth.src !== 'all' && depth.src.includes(s);
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.classList.toggle('inc', !on && s !== 'all' && depth.src === 'all' && !!included && included.includes(s));
  });
}
let depthIncluded = null;
syncSrcButtons(null);

function renderDepth(d) {
  const now = Date.now();
  const T = Array.isArray(d.times) ? d.times.length : 0;
  const cov = d.coverage || {};
  const hl = d.health || {};
  $('d-srcs').querySelectorAll('button').forEach(b => {
    const s = b.dataset.src;
    if (s === 'all') {                             // "全部"到底合并了哪几个源由后端决定，这里只报个数
      b.querySelector('i').textContent = Array.isArray(d.sources) && d.sources.length && depth.src === 'all'
        ? d.sources.length + ' 个源' : '合并';
      return;
    }
    b.querySelector('i').textContent = cov[s] > 0 ? '±' + trimNum(cov[s]) + '%' : '–';
    const ok = hl.last_ok ? hl.last_ok[s] : null, err = hl.last_error ? hl.last_error[s] : null;
    if (isNum(ok) && now - ok < 180000) b.dataset.h = 'ok';
    else if (err || isNum(ok)) b.dataset.h = 'bad';
    else delete b.dataset.h;
    b.title = [cov[s] > 0 ? '盘口覆盖中间价 ±' + trimNum(cov[s]) + '%' : '暂无记录',
      isNum(ok) ? '上次成功 ' + fmtAgo(now - ok) : '', err ? '最近错误: ' + err : ''].filter(Boolean).join(' · ');
  });
  depthIncluded = Array.isArray(d.sources) ? d.sources : null;
  syncSrcButtons(depthIncluded);

  const latest = d.latest && typeof d.latest === 'object' ? d.latest : null;
  depthChart.setData({
    times: d.times || [], line: d.mid || [], z: d.z || [], rows: d.rows | 0, p0: d.p0, bucket: d.bucket,
    zmax: d.zmax, ref: latest && isNum(latest.mid) ? latest.mid : lastOf(d.mid),
    profile: latest ? {prices: latest.prices, a: latest.bids || [], b: latest.asks || []} : null,
  });
  const rm = isNum(d.recorded_minutes) ? d.recorded_minutes : 0;
  const filling = '已录 ' + fmtDur(rm) + ' · 随录制逐步填充';
  // 已经在录、但所选的源一格都没有（例如该交易所从服务器连不上）→ 别说"逐步填充"，直说没有
  const srcEmpty = T === 0 && depth.src !== 'all' && (rm > 0 || (hl.snapshots | 0) > 0);
  depthChart.setMessage(T >= 2 ? '' : flowOff ? OFF_TEXT : srcEmpty ? '所选数据源暂无记录' : filling);
  const note = $('d-note');
  note.hidden = !(T >= 2 && rm < 30);
  note.textContent = filling;
  setLegend($('d-legend'), T >= 2 ? d.zmax : 0);
  $('d-meta').textContent = T >= 2 ? (note.hidden ? '已录 ' + fmtDur(rm) + ' · ' : '') + '$' + trimNum(d.bucket) + '/格' : '';
}

const FAIL_TEXT = '暂时取不到数据 · 自动重试中';
const depthLoader = createLoader({name: 'depth', root: $('blk-depth'), interval: 30000, url: depthUrl,
  render: renderDepth, onFail: (why, had) => { if (!had) depthChart.setMessage(FAIL_TEXT); }});
bindSeg($('d-hours'), depth.hours, v => {
  depth.hours = +v;
  store.set('d.hours', depth.hours);
  depthLoader.load();
});
let srcTimer = 0;
$('d-srcs').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  const s = b.dataset.src;
  if (s === 'all') {
    if (depth.src === 'all') return;
    depth.src = 'all';
  } else if (depth.src === 'all') {
    depth.src = [s];
  } else {
    const set = depth.src.includes(s) ? depth.src.filter(x => x !== s) : depth.src.concat(s);
    depth.src = set.length ? SRC_ORDER.filter(x => set.includes(x)) : 'all';   // 全取消 = 回到全部
  }
  store.set('d.src', depth.src);
  syncSrcButtons(depthIncluded);
  clearTimeout(srcTimer);                       // 连点几个数据源时只发最后一次请求
  srcTimer = setTimeout(() => depthLoader.load(), 350);
});

// ====================== C. 清算热力图（估算） ======================
const liq = {days: 7};
(function () { const v = store.get('l.days', 7); if ([3, 7, 14, 30].includes(v)) liq.days = v; })();
const liqChart = new HeatChart($('l-chart'), {
  zooms: [0, 10, 5, 3], zoomKey: 'l.zoom', sizeLabel: '估算清算', aLabel: '多单', bLabel: '空单'});
liqChart.setMessage('加载中…');

function renderClusters(box, list, ready) {
  box.textContent = '';
  const rows = (Array.isArray(list) ? list : []).filter(c => c && isNum(c.price)).slice(0, 3);
  if (!rows.length) { box.append(el('div', 'fl-cl-none', ready ? '±8% 内无明显簇' : '–')); return; }
  const mx = Math.max.apply(null, rows.map(c => c.size || 0)) || 1;
  for (const c of rows) {
    const row = el('div', 'fl-cl-row');
    row.style.setProperty('--w', clamp((c.size || 0) / mx * 100, 4, 100).toFixed(0) + '%');
    row.append(el('b', '', fmtPrice(c.price, 0)), el('span', '', fmtBtc(c.size)), el('em', '', fmtPct(c.dist_pct, 1)));
    box.append(row);
  }
}
function renderAssume(a) {
  const box = $('l-assume');
  box.textContent = '';
  if (!a || typeof a !== 'object') { box.textContent = '估算值：由持仓量变化与假设杠杆推算，不是交易所清算数据。'; return; }
  const tiers = (Array.isArray(a.tiers) ? a.tiers : []).filter(t => Array.isArray(t) && isNum(t[0]))
    .map(t => t[0] + 'x ' + (isNum(t[1]) ? Math.round(t[1] <= 1 ? t[1] * 100 : t[1]) + '%' : '')).join(' · ');
  if (tiers) box.append(el('b', '', '杠杆档 '), document.createTextNode(tiers));
  if (isNum(a.mmr)) box.append(el('b', '', (tiers ? ' · ' : '') + '维持保证金 '), document.createTextNode(trimNum(a.mmr * 100) + '%'));
  if (a.note) {
    if (box.firstChild) box.append(document.createElement('br'));
    box.append(document.createTextNode(String(a.note)));
  }
}
function renderLiq(d) {
  const T = Array.isArray(d.times) ? d.times.length : 0;
  const cl = d.clusters || {};
  const markers = [];
  for (const c of (cl.above || [])) if (c) markers.push({price: c.price, color: C.red});
  for (const c of (cl.below || [])) if (c) markers.push({price: c.price, color: C.green});
  const pf = d.profile && typeof d.profile === 'object' ? d.profile : null;
  liqChart.setData({
    times: d.times || [], line: d.close || [], z: d.z || [], rows: d.rows | 0, p0: d.p0, bucket: d.bucket,
    zmax: d.zmax, ref: isNum(d.price) ? d.price : lastOf(d.close),
    profile: pf ? {a: pf.long || [], b: pf.short || []} : null, markers,
  });
  liqChart.setMessage(T < 2 ? (flowOff ? OFF_TEXT : '估算数据还在准备') : '');
  setLegend($('l-legend'), T >= 2 ? d.zmax : 0);
  renderClusters($('l-above'), cl.above, T >= 2);
  renderClusters($('l-below'), cl.below, T >= 2);
  renderAssume(d.assumptions);
}
const liqLoader = createLoader({name: 'liqmap', root: $('blk-liq'), interval: 300000, timeout: 45000,
  url: () => '/api/flow/liqmap?days=' + liq.days, render: renderLiq,
  onFail: (why, had) => { if (!had) liqChart.setMessage(FAIL_TEXT); }});
bindSeg($('l-days'), liq.days, v => {
  liq.days = +v;
  store.set('l.days', liq.days);
  liqLoader.load();
});
$('l-info').addEventListener('click', () => {
  const box = $('l-assume');
  if (!box.firstChild) renderAssume(null);
  box.hidden = !box.hidden;
  $('l-info').setAttribute('aria-expanded', box.hidden ? 'false' : 'true');
});

// ====================== D. CVD 对比 ======================
const CVD_LINES = [
  {key: 'binance_fut', name: '合约', color: '#4f8ef7'},
  {key: 'binance_spot', name: '现货', color: '#f1c40f'},
  {key: 'okx_swap', name: 'OKX', color: '#b07cff'},
];
const CVD_DIVS = [['fut_vs_price', '合约 × 价'], ['spot_vs_price', '现货 × 价'], ['fut_vs_spot', '合约 × 现货']];
const PRICE_COLOR = '#c9d1e0';
const TZ_SHIFT = -new Date().getTimezoneOffset() * 60;      // 图表库按 UTC 显示，平移成本地时间
const cvd = {tf: '1h', shared: !!store.get('c.shared', false), chart: null, price: null, lines: {}, last: {},
  fitNext: true, divSel: '', divData: null};
(function () { const v = store.get('c.tf', '1h'); if (['5m', '15m', '1h', '4h'].includes(v)) cvd.tf = v; })();

function cvdMessage(text) {
  const box = $('c-chart');
  let m = box.querySelector('.fl-cvd-msg');
  if (!text) { if (m) m.remove(); return; }
  if (!m) { m = el('div', 'fl-cvd-msg'); box.append(m); }
  m.textContent = text;
}
cvdMessage('加载中…');

function ensureCvdChart() {
  if (cvd.chart) return true;
  if (typeof LightweightCharts === 'undefined') return false;
  const LC = LightweightCharts;
  cvd.chart = LC.createChart($('c-chart'), {
    autoSize: true,
    layout: {background: {color: 'transparent'}, textColor: C.muted, fontSize: 11},
    grid: {vertLines: {color: '#1d2433'}, horzLines: {color: '#1d2433'}},
    rightPriceScale: {borderColor: C.border, scaleMargins: {top: 0.08, bottom: 0.08}},
    leftPriceScale: {visible: cvd.shared, borderColor: C.border, scaleMargins: {top: 0.08, bottom: 0.08}},
    timeScale: {timeVisible: true, secondsVisible: false, borderColor: C.border, rightOffset: 2},
    handleScroll: {vertTouchDrag: false},          // 手机上竖向拖动留给页面滚动
  });
  cvd.price = cvd.chart.addLineSeries({color: PRICE_COLOR, lineWidth: 1, priceScaleId: 'right',
    priceLineVisible: false, crosshairMarkerRadius: 3,
    priceFormat: {type: 'price', precision: 0, minMove: 1}});
  cvd.chart.subscribeCrosshairMove(param => {
    if (!param || param.time == null || !param.seriesData) { paintCvdLegend(cvd.last); return; }
    const cur = {};
    const pv = param.seriesData.get(cvd.price);
    if (pv) cur.price = pv.value;
    for (const k of Object.keys(cvd.lines)) {
      const sv = param.seriesData.get(cvd.lines[k]);
      if (sv) cur[k] = sv.value;
    }
    paintCvdLegend(cur);
  });
  return true;
}
function cvdScaleId(key) { return cvd.shared ? 'left' : 'cvd_' + key; }
function applyCvdScales() {
  if (!cvd.chart) return;
  cvd.chart.applyOptions({leftPriceScale: {visible: cvd.shared}});
  for (const k of Object.keys(cvd.lines)) {
    cvd.lines[k].applyOptions({priceScaleId: cvdScaleId(k), lastValueVisible: cvd.shared});
    if (!cvd.shared) cvd.chart.priceScale(cvdScaleId(k)).applyOptions({scaleMargins: {top: 0.1, bottom: 0.1}});
  }
}
function paintCvdLegend(vals) {
  const box = $('c-legend');
  box.textContent = '';
  const add = (name, color, text) => {
    const s = el('span', '', name);
    s.style.setProperty('--c', color);
    s.append(el('b', '', text));
    box.append(s);
  };
  if (isNum(vals.price)) add('价格', PRICE_COLOR, fmtPrice(vals.price, 0));
  for (const ln of CVD_LINES) if (cvd.lines[ln.key]) add(ln.name, ln.color, isNum(vals[ln.key]) ? fmtBtc(vals[ln.key], true) : '–');
}
function paintCvdDivs() {
  const box = $('c-divs');
  box.textContent = '';
  const data = cvd.divData || {};
  for (const [key, name] of CVD_DIVS) {
    const d = data[key];
    const info = divInfo(d);
    const chip = el('button', 'fl-chip ' + (info.cls === 'warn' ? 'warn' : '') + (cvd.divSel === key ? ' sel' : ''));
    chip.type = 'button';
    chip.dataset.k = key;
    const label = !d ? '–' : (key === 'fut_vs_spot' && d.state === 'flat') ? '无明显方向' : info.label;
    chip.append(el('i', '', name), el('b', info.cls, label));
    box.append(chip);
  }
  const note = $('c-div-text');
  const sel = data[cvd.divSel];
  note.hidden = !(sel && sel.text);
  note.textContent = sel && sel.text ? sel.text : '';
}
$('c-divs').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  cvd.divSel = cvd.divSel === b.dataset.k ? '' : b.dataset.k;     // 点一下看原文，再点收起
  paintCvdDivs();
});

function renderCvd(d) {
  cvd.divData = d.div && typeof d.div === 'object' ? d.div : {};
  paintCvdDivs();
  const times = Array.isArray(d.times) ? d.times : [];
  if (!ensureCvdChart()) { cvdMessage('图表库未加载'); return; }
  if (!times.length) {                               // 后端缓存还没算出来：清掉旧线，别让旧周期的图留在原地
    cvd.price.setData([]);
    for (const k of Object.keys(cvd.lines)) cvd.lines[k].setData([]);
    cvd.last = {};
    paintCvdLegend({});
    cvd.fitNext = true;
    cvdMessage('数据还在准备');
    return;
  }
  cvdMessage('');
  // 时间必须严格递增，否则图表库会抛错；顺手丢掉空值
  const toSeries = arr => {
    const out = [];
    let prev = -Infinity;
    for (let i = 0; i < times.length; i++) {
      const t = Math.floor(times[i] / 1000) + TZ_SHIFT, v = arr[i];
      if (!isNum(v) || !(t > prev)) continue;
      out.push({time: t, value: v});
      prev = t;
    }
    return out;
  };
  const last = {};
  cvd.price.setData(toSeries(Array.isArray(d.price) ? d.price : []));
  last.price = lastOf(d.price);
  const series = d.series && typeof d.series === 'object' ? d.series : {};
  for (const ln of CVD_LINES) {
    const arr = series[ln.key];
    const has = Array.isArray(arr) && arr.length === times.length;
    if (has && !cvd.lines[ln.key]) {
      cvd.lines[ln.key] = cvd.chart.addLineSeries({color: ln.color, lineWidth: 2, priceScaleId: cvdScaleId(ln.key),
        priceLineVisible: false, lastValueVisible: cvd.shared, crosshairMarkerRadius: 3,
        priceFormat: {type: 'custom', minMove: 0.1, formatter: v => fmtBtc(v, true)}});
      if (!cvd.shared) cvd.chart.priceScale(cvdScaleId(ln.key)).applyOptions({scaleMargins: {top: 0.1, bottom: 0.1}});
    } else if (!has && cvd.lines[ln.key]) {
      cvd.chart.removeSeries(cvd.lines[ln.key]);
      delete cvd.lines[ln.key];
    }
    if (has) { cvd.lines[ln.key].setData(toSeries(arr)); last[ln.key] = lastOf(arr); }
  }
  cvd.last = last;
  paintCvdLegend(last);
  if (cvd.fitNext) { cvd.chart.timeScale().fitContent(); cvd.fitNext = false; }
}
const cvdLoader = createLoader({name: 'cvd', root: $('blk-cvd'), interval: 60000,
  url: () => '/api/flow/cvd?tf=' + cvd.tf, render: renderCvd,
  onFail: (why, had) => { if (!had) cvdMessage(FAIL_TEXT); }});
bindSeg($('c-tf'), cvd.tf, v => {
  cvd.tf = v;
  cvd.fitNext = true;
  store.set('c.tf', v);
  cvdLoader.load();
});
$('c-shared').setAttribute('aria-pressed', cvd.shared ? 'true' : 'false');
$('c-shared').addEventListener('click', () => {
  cvd.shared = !cvd.shared;
  store.set('c.shared', cvd.shared);
  $('c-shared').setAttribute('aria-pressed', cvd.shared ? 'true' : 'false');
  applyCvdScales();
});

// ====================== 0. 顶栏：状态点 / 登录态 ======================
const CACHE_NAMES = {summary: '状态卡', liqmap: '清算图', cvd: 'CVD'};
const CACHE_STALE = {summary: 300000, liqmap: 1200000, cvd: 300000};
function setDot(level, rows) {
  $('hl-dot').className = 'fl-dot' + (level ? ' ' + level : '');
  const pop = $('hl-pop');
  pop.textContent = '';
  for (const r of rows) {
    const line = document.createElement('div');
    line.append(el('span', '', r[0]), el('span', '', r[1]));
    pop.append(line);
  }
}
function renderHealth(h) {
  const now = Date.now();
  const rows = [];
  let level = 'ok';
  const warn = () => { if (level === 'ok') level = 'warn'; };
  if (!h.enabled) { level = ''; rows.push(['订单流', '未启用']); }
  if (flowOff !== !h.enabled) {
    flowOff = !h.enabled;
    if (flowOff) { depthChart.setMessage(OFF_TEXT); liqChart.setMessage(OFF_TEXT); }
  }
  const rec = h.recorder && typeof h.recorder === 'object' ? h.recorder : {};
  let newest = 0;
  if (rec.last_ok && typeof rec.last_ok === 'object') {
    for (const v of Object.values(rec.last_ok)) if (isNum(v) && v > newest) newest = v;
  } else if (isNum(rec.last_ok)) newest = rec.last_ok;
  if (isNum(rec.last_snapshot) && rec.last_snapshot > newest) newest = rec.last_snapshot;
  if (rec.running === false && h.enabled) {
    rows.push(['盘口录制', '已停止']);
    warn();
  } else if (newest > 0) {
    rows.push(['盘口录制', fmtAgo(now - newest)]);
    if (now - newest > 180000) warn();
  } else if (h.enabled) {
    rows.push(['盘口录制', '暂无快照']);
    warn();
  }
  if (isNum(rec.snapshots)) rows.push(['快照数', rec.snapshots.toLocaleString('en-US')]);
  const caches = h.caches && typeof h.caches === 'object' ? h.caches : {};
  for (const k of Object.keys(CACHE_NAMES)) {
    const v = caches[k];
    if (isNum(v) && v > 0) {
      const age = v > 1e11 ? now - v : v;          // 兼容"时间戳"与"已过毫秒数"两种给法
      rows.push([CACHE_NAMES[k], fmtAgo(age)]);
      if (age > CACHE_STALE[k]) warn();
    } else if (h.enabled) {
      rows.push([CACHE_NAMES[k], '未就绪']);
      warn();
    }
  }
  setDot(level, rows);
}
createLoader({name: 'health', interval: 30000, silent: true, url: () => '/api/flow/health',
  render: renderHealth, onFail: why => setDot('bad', [['状态接口', why]])});

$('hl-btn').addEventListener('click', e => {
  e.stopPropagation();
  const pop = $('hl-pop');
  pop.hidden = !pop.hidden;
  $('hl-btn').setAttribute('aria-expanded', pop.hidden ? 'false' : 'true');
});
document.addEventListener('click', e => {
  const pop = $('hl-pop');
  if (!pop.hidden && !pop.contains(e.target)) {
    pop.hidden = true;
    $('hl-btn').setAttribute('aria-expanded', 'false');
  }
});

// ====================== 启动 ======================
createLoader({name: 'summary', root: $('blk-summary'), interval: 60000, url: () => '/api/flow/summary', render: renderSummary});
for (const l of loaders) l.load();
})();
