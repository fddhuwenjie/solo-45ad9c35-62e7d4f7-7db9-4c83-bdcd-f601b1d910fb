(() => {
  'use strict';

  const EPS = 1e-6;
  const SNAP = 0.05;
  const ORIENTATIONS = {
    LWH: [0, 1, 2], WLH: [1, 0, 2], LHW: [0, 2, 1],
    WHL: [1, 2, 0], HLW: [2, 0, 1], HWL: [2, 1, 0],
  };
  const COLORS = ['#5087e8', '#f59e0b', '#10b981', '#a855f7', '#ef4444', '#06b6d4'];

  let plans = [];
  let planId = null;
  let state = null;
  let report = null;
  let selectedId = null;
  let analyzeTimer = null;
  let drag = null;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];
  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
  const snap = (v) => Math.round(v / SNAP) * SNAP;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function toast(message, type = '') {
    const el = $('#toast');
    el.textContent = message;
    el.className = `toast ${type}`;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.add('hidden'), 3200);
  }

  async function api(url, options = {}) {
    const res = await fetch(url, {
      headers: options.body ? {'Content-Type': 'application/json'} : {},
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
    return data;
  }

  function dims(c, orientation = 'LWH') {
    const order = ORIENTATIONS[orientation] || ORIENTATIONS.LWH;
    return order.map(i => Number(c.dims[i] || 0));
  }
  function caseById(id) { return state.cases.find(c => c.id === id); }
  function stopById(id) { return state.stops.find(s => s.id === id); }
  function placement(caseId) { return state.placements.find(p => p.case_id === caseId); }
  function box(c) {
    const p = placement(c.id);
    if (!p) return null;
    const [dx, dy, dz] = dims(c, p.orientation);
    return {...p, id: c.id, label: c.label, dx, dy, dz, weight: c.weight_kg, stop_id: c.stop_id, case: c};
  }
  function allBoxes() { return state.cases.map(box).filter(Boolean); }
  function stopRank(id) { const i = state.stops.findIndex(s => s.id === id); return i < 0 ? 999 : i; }
  function stopColor(i) { return COLORS[i % COLORS.length]; }

  function setPlanData(plan) {
    planId = plan.id;
    state = plan.state;
    $('#planName').value = state.name || plan.name || '';
    $('#statusPill').textContent = plan.status === 'confirmed' ? '已确认' : '草稿';
    $('#statusPill').className = `pill ${plan.status}`;
    $('#snapshotInfo').textContent = plan.confirmed_at
      ? `快照确认于 ${new Date(plan.confirmed_at).toLocaleString()}；已确认方案保留，后续改动形成新版本。`
      : '尚无确认快照。确认后，车辆或站序调整会保留旧快照并生成新版本。';
    if (!state.placements) state.placements = [];
    if (!caseById(selectedId)) selectedId = null;
    render();
    scheduleAnalyze();
  }

  async function loadPlans(selectId = null) {
    const data = await api('/api/plans');
    plans = data.plans;
    $('#planSelect').innerHTML = plans.map(p =>
      `<option value="${esc(p.id)}">${esc(p.state.name || p.name)} · ${p.status}</option>`).join('');
    const target = plans.find(p => p.id === selectId) || plans[0];
    if (target) {
      $('#planSelect').value = target.id;
      setPlanData(target);
      loadVersions();
    }
  }

  function mutate(nextState, analyze = true) {
    state = nextState;
    render();
    if (analyze) scheduleAnalyze();
  }

  function scheduleAnalyze() {
    clearTimeout(analyzeTimer);
    analyzeTimer = setTimeout(async () => {
      try {
        report = await api('/api/analyze', {method: 'POST', body: JSON.stringify({state})});
        renderReport();
        renderIssuesOnSvg();
      } catch (err) {
        toast(err.message, 'error');
      }
    }, 160);
  }

  function render() {
    if (!state) return;
    renderTruckForm(false);
    renderCases();
    renderStops();
    renderAxles();
    renderViews();
    renderReport();
  }

  function issueClass(caseId) {
    const ci = report?.case_issues?.[caseId];
    if (ci?.errors?.length) return 'has-error';
    if (ci?.warnings?.length) return 'has-warning';
    return '';
  }

  function renderCases() {
    const tbody = $('#caseTable tbody');
    tbody.innerHTML = state.cases.map(c => {
      const p = placement(c.id);
      const stop = stopById(c.stop_id);
      const status = p
        ? `${Number(p.x).toFixed(2)}/${Number(p.y).toFixed(2)}/${Number(p.z).toFixed(2)}<br>${c.weight_kg}kg ${p.locked ? '🔒' : ''}`
        : `<em>未放置</em><br>${c.weight_kg}kg`;
      return `<tr data-case="${esc(c.id)}" class="${p ? 'placed' : 'unplaced'}">
        <td><span class="case-label">${esc(c.label)}</span><span class="case-id">${esc(c.id)} · ${c.dims.join('×')}</span></td>
        <td>${esc(stop?.city || '—')}</td>
        <td>${status}</td>
        <td class="row-actions">
          <button data-action="select">选</button>
          ${p ? '<button data-action="delete">移走</button>' : '<button data-action="place">放置</button>'}
        </td></tr>`;
    }).join('');
    renderSelected();
  }

  function renderSelected() {
    const card = $('#selectedCard');
    const c = caseById(selectedId);
    if (!c) {
      card.className = 'selected-card empty';
      card.textContent = '请在任一视图选择航空箱';
      return;
    }
    let p = placement(c.id);
    if (!p) {
      p = {case_id: c.id, x: 0, y: 0, z: 0, orientation: (c.allowed_orientations || ['LWH'])[0], locked: false};
    }
    const [dx, dy, dz] = dims(c, p.orientation);
    const stopOptions = state.stops.map(s => `<option value="${esc(s.id)}" ${s.id === c.stop_id ? 'selected' : ''}>${esc(s.city)}</option>`).join('');
    const oriOptions = (c.allowed_orientations || ['LWH']).map(o => `<option value="${o}" ${o === p.orientation ? 'selected' : ''}>${o}</option>`).join('');
    card.className = 'selected-card';
    card.innerHTML = `
      <h3>${esc(c.label)} <small class="case-id">${esc(c.id)}</small></h3>
      <p class="hint">${esc(c.notes || '无备注')}｜承重上限 ${c.max_stack_kg || 0}kg｜禁邻：${esc((c.forbidden_neighbors || []).join(', ') || '无')}</p>
      <div class="inline-fields">
        <label>站点<select data-field="stop">${stopOptions}</select></label>
        <label>方向<select data-field="orientation">${oriOptions}</select></label>
        <label>锁定<select data-field="locked"><option value="false" ${!p.locked ? 'selected' : ''}>否</option><option value="true" ${p.locked ? 'selected' : ''}>是</option></select></label>
        <label>x m<input type="number" step=".05" data-field="x" value="${round(p.x)}"></label>
        <label>y m<input type="number" step=".05" data-field="y" value="${round(p.y)}"></label>
        <label>z m<input type="number" step=".05" data-field="z" value="${round(p.z)}"></label>
      </div>
      <div class="selected-actions">
        <button data-selected="place">确保已放置</button>
        <button data-selected="rotate">旋转方向</button>
        <button data-selected="up">上移 0.05m</button>
        <button data-selected="down">下移 0.05m</button>
        <button data-selected="rear">贴尾门</button>
        <button data-selected="delete" class="danger">移出舱体</button>
      </div>
      <p class="hint">当前外廓 ${dx.toFixed(2)}×${dy.toFixed(2)}×${dz.toFixed(2)} m</p>`;

    card.querySelector('[data-field="stop"]').onchange = e => { c.stop_id = e.target.value; persistSelectedEdit(p); };
    card.querySelector('[data-field="orientation"]').onchange = e => { p.orientation = e.target.value; clampPlacement(p, c); persistSelectedEdit(p); };
    card.querySelector('[data-field="locked"]').onchange = e => { p.locked = e.target.value === 'true'; persistSelectedEdit(p); };
    ['x','y','z'].forEach(f => card.querySelector(`[data-field="${f}"]`).addEventListener('change', e => {
      p[f] = Number(e.target.value); clampPlacement(p, c); persistSelectedEdit(p);
    }));
    card.querySelector('[data-selected="place"]').onclick = () => persistSelectedEdit(p);
    card.querySelector('[data-selected="rotate"]').onclick = () => {
      const allowed = c.allowed_orientations || ['LWH'];
      p.orientation = allowed[(allowed.indexOf(p.orientation) + 1) % allowed.length];
      clampPlacement(p, c); persistSelectedEdit(p);
    };
    card.querySelector('[data-selected="up"]').onclick = () => { p.z = snap(p.z + SNAP); clampPlacement(p, c); persistSelectedEdit(p); };
    card.querySelector('[data-selected="down"]').onclick = () => { p.z = snap(Math.max(0, p.z - SNAP)); persistSelectedEdit(p); };
    card.querySelector('[data-selected="rear"]').onclick = () => { p.x = 0; clampPlacement(p, c); persistSelectedEdit(p); };
    card.querySelector('[data-selected="delete"]').onclick = () => {
      state.placements = state.placements.filter(q => q.case_id !== c.id);
      mutate(state);
    };
  }

  function round(v) { return Math.round(Number(v || 0) * 100) / 100; }
  function clampPlacement(p, c) {
    const [dx, dy, dz] = dims(c, p.orientation);
    p.x = snap(clamp(Number(p.x) || 0, 0, Math.max(0, state.truck.length - dx)));
    p.y = snap(clamp(Number(p.y) || 0, 0, Math.max(0, state.truck.width - dy)));
    p.z = snap(clamp(Number(p.z) || 0, 0, Math.max(0, state.truck.height - dz)));
  }
  function ensurePlacement(p, c) {
    if (!placement(c.id)) state.placements.push(p);
    clampPlacement(p, c);
    selectedId = c.id;
    mutate(state);
  }
  function persistSelectedEdit(p) { ensurePlacement(p, caseById(selectedId)); }

  function renderStops() {
    $('#stopList').innerHTML = state.stops.map((s, i) => `
      <li data-stop="${esc(s.id)}">
        <span class="stop-index">${i + 1}</span>
        <span class="stop-main"><input value="${esc(s.city)}" data-stop-city="${esc(s.id)}"><small>${esc(s.venue || s.id)} · ${state.cases.filter(c => c.stop_id === s.id).length} 箱</small></span>
        <span class="stop-buttons">
          <button data-move-stop="${esc(s.id)}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button data-move-stop="${esc(s.id)}" data-dir="1" ${i === state.stops.length - 1 ? 'disabled' : ''}>↓</button>
          <button data-remove-stop="${esc(s.id)}" class="danger">×</button>
        </span>
      </li>`).join('');
  }

  function renderTruckForm(focus = true) {
    const t = state.truck;
    const set = (id, v) => { const el = $(id); if (document.activeElement !== el || !focus) el.value = v; };
    set('#truckLength', t.length); set('#truckWidth', t.width); set('#truckHeight', t.height);
    set('#floorLimit', t.floor_limit_kg_m2); set('#pointLimit', t.floor_point_limit_kg); set('#gvwLimit', t.gvw_limit_kg);
    set('#doorWidth', t.door.width); set('#doorHeight', t.door.height); set('#doorSill', t.door.sill || 0);
  }
  function renderAxles() {
    $('#axleEditor').innerHTML = state.truck.axles.map((a, i) => `
      <div class="axle-row">
        <label>名称<input data-axle="${i}" data-k="name" value="${esc(a.name)}"></label>
        <label>x m<input data-axle="${i}" data-k="position" type="number" step=".05" value="${a.position}"></label>
        <label>轴限 kg<input data-axle="${i}" data-k="capacity_kg" type="number" value="${a.capacity_kg}"></label>
        <button data-remove-axle="${i}" class="danger">×</button>
      </div>
      <div class="axle-row">
        <label>空车轴荷 kg<input data-axle="${i}" data-k="tare_kg" type="number" value="${a.tare_kg}"></label>
      </div>`).join('');
  }

  function svgEl(content, w, h) {
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">
      <defs><marker id="arrow-${Math.random().toString(36).slice(2)}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#334155"/></marker></defs>
      <rect width="${w}" height="${h}" fill="#fbfdff"/>${content}</svg>`;
  }

  function caseStroke(b) {
    const ci = report?.case_issues?.[b.id];
    if (ci?.errors?.length) return {stroke: '#dc2626', width: 2.8};
    if (ci?.warnings?.length) return {stroke: '#d97706', width: 2.3};
    return {stroke: '#1e293b', width: 1};
  }

  function rectNode(b, x, y, w, h, extraClass = '') {
    const st = caseStroke(b);
    const sel = b.id === selectedId ? ' case-selected' : '';
    const lock = b.locked ? ' 🔒' : '';
    return `<g class="draggable${sel} ${extraClass}" data-id="${esc(b.id)}">
      <rect x="${x}" y="${y}" width="${Math.max(1,w)}" height="${Math.max(1,h)}" rx="3"
        fill="${stopColor(stopRank(b.stop_id))}" fill-opacity=".70" stroke="${st.stroke}" stroke-width="${st.width}"/>
      <text x="${x+5}" y="${y+16}" font-size="12" font-weight="700" pointer-events="none">${esc(b.label)}${lock}</text>
    </g>`;
  }

  function renderViews() {
    if (!state) return;
    const t = state.truck;
    renderTop(t);
    renderSide(t);
    renderTail(t);
    bindSvgDrag();
  }

  function renderTop(t) {
    const s = 72, ml = 38, mt = 34;
    const W = ml + t.length * s + 24, H = mt + t.width * s + 40;
    const x0 = ml, y0 = mt;
    const doorY = y0 + (t.width - t.door.width) / 2 * s;
    let html = `<rect x="${x0}" y="${y0}" width="${t.length*s}" height="${t.width*s}" fill="#fff" stroke="#0f172a" stroke-width="2"/>
      <line x1="${x0}" y1="${doorY}" x2="${x0}" y2="${doorY+t.door.width*s}" stroke="#059669" stroke-width="7"/>
      <text x="${x0+5}" y="${y0-10}" font-size="13" font-weight="700">尾门</text>
      <text x="${x0+t.length*s-45}" y="${y0-10}" font-size="13">车头 →</text>`;
    for (let x = 0; x <= Math.floor(t.length * 2); x++) {
      const px = x0 + x * .5 * s;
      html += `<line x1="${px}" y1="${y0+t.width*s}" x2="${px}" y2="${y0+t.width*s+5}" stroke="#64748b"/><text x="${px-6}" y="${y0+t.width*s+20}" font-size="10">${x*.5}</text>`;
    }
    html += allBoxes().sort((a,b) => a.z - b.z).map(b =>
      rectNode(b, x0+b.x*s+b.z*3, y0+b.y*s-b.z*2, b.dx*s, b.dy*s)).join('');
    $('#topView').innerHTML = svgEl(html, W, H);
  }

  function renderSide(t) {
    const s = 72, ml = 38, mt = 30;
    const W = ml + t.length * s + 24, H = mt + t.height * s + 58;
    const x0 = ml, y0 = mt;
    let html = `<rect x="${x0}" y="${y0}" width="${t.length*s}" height="${t.height*s}" fill="#fff" stroke="#0f172a" stroke-width="2"/>`;
    t.axles.forEach(a => {
      const ax = x0 + a.position * s, ay = y0 + t.height*s + 22;
      html += `<circle cx="${ax}" cy="${ay}" r="11" fill="#fff" stroke="#334155" stroke-width="3"/><text x="${ax-18}" y="${ay+31}" font-size="11">${esc(a.name)}轴</text>`;
    });
    const cg = report?.metrics?.x;
    if (cg != null) {
      const cgx = x0+cg*s;
      html += `<line x1="${cgx}" y1="${y0-6}" x2="${cgx}" y2="${y0+t.height*s+4}" stroke="#dc2626" stroke-dasharray="5 4"/><text x="${cgx-20}" y="${y0-10}" font-size="11" fill="#dc2626">重心</text>`;
    }
    html += allBoxes().sort((a,b) => b.z - a.z).map(b =>
      rectNode(b, x0+b.x*s, y0+(t.height-b.z-b.dz)*s, b.dx*s, b.dz*s)).join('');
    $('#sideView').innerHTML = svgEl(html, W, H);
  }

  function renderTail(t) {
    const s = 120, ml = 38, mt = 25;
    const W = ml + t.width*s + 25, H = mt + t.height*s + 35;
    const x0 = ml, y0 = mt;
    const doorX = x0 + (t.width-t.door.width)/2*s;
    const doorY = y0 + (t.height-t.door.sill-t.door.height)*s;
    let html = `<rect x="${x0}" y="${y0}" width="${t.width*s}" height="${t.height*s}" fill="#fff" stroke="#0f172a" stroke-width="2"/>
      <rect x="${doorX}" y="${doorY}" width="${t.door.width*s}" height="${t.door.height*s}" fill="#d1fae5" stroke="#059669" stroke-width="3" stroke-dasharray="8 5"/>
      <text x="${doorX+8}" y="${doorY+20}" fill="#047857" font-weight="700" font-size="13">门洞</text>`;
    html += allBoxes().sort((a,b) => b.x - a.x).map(b => {
      const alpha = .28 + .5 * clamp(1 - b.x / t.length, 0, 1);
      const st = caseStroke(b);
      const sel = b.id === selectedId ? ' case-selected' : '';
      const x = x0+b.y*s, y = y0+(t.height-b.z-b.dz)*s;
      return `<g class="draggable${sel}" data-id="${esc(b.id)}">
        <rect x="${x}" y="${y}" width="${b.dy*s}" height="${b.dz*s}" fill="${stopColor(stopRank(b.stop_id))}" fill-opacity="${alpha}" stroke="${st.stroke}" stroke-width="${st.width}"/>
        <text x="${x+5}" y="${y+17}" font-size="12" font-weight="700" pointer-events="none">${esc(b.label)}</text>
      </g>`;
    }).join('');
    $('#tailView').innerHTML = svgEl(html, W, H);
  }

  function renderIssuesOnSvg() {
    // Rebuilding is cheap and keeps drag transforms from fighting class updates.
    renderViews();
  }

  function svgPoint(evt, svg) {
    const pt = svg.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  }

  function bindSvgDrag() {
    $$('.svg-box svg').forEach(svg => {
      svg.addEventListener('pointerdown', evt => {
        const g = evt.target.closest('[data-id]');
        if (!g) return;
        const c = caseById(g.dataset.id);
        const p = placement(c.id);
        selectedId = c.id;
        renderCases();
        if (p.locked) { toast('该箱体已锁定；如需移动请先取消锁定。', 'warn'); return; }
        const b = box(c);
        drag = {
          id: c.id, view: svg.closest('.svg-box').id, g, svg,
          startX: evt.clientX, startY: evt.clientY,
          ox: p.x, oy: p.y, oz: p.z, b,
        };
        g.setPointerCapture(evt.pointerId);
      });
      svg.addEventListener('pointermove', evt => {
        if (!drag || drag.svg !== svg) return;
        const c = caseById(drag.id), p = placement(drag.id), b = box(c);
        const scale = drag.view === 'tailView' ? 120 : 72;
        const dxm = (evt.clientX - drag.startX) / scale;
        const dym = (evt.clientY - drag.startY) / scale;
        if (drag.view === 'topView') { p.x = snap(drag.ox + dxm); p.y = snap(drag.oy + dym); }
        if (drag.view === 'sideView') { p.x = snap(drag.ox + dxm); p.z = snap(drag.oz - dym); }
        if (drag.view === 'tailView') { p.y = snap(drag.oy + dxm); p.z = snap(drag.oz - dym); }
        clampPlacement(p, c);
        renderViews();
      });
      svg.addEventListener('pointerup', () => {
        if (drag) { scheduleAnalyze(); drag = null; }
      });
      svg.addEventListener('pointercancel', () => { drag = null; });
    });
  }

  function fmtKg(v) { return `${Math.round(v)}kg`; }
  function renderReport() {
    if (!report) {
      ['metricSafety','metricAxle','metricCg','metricFloor','metricRehandle'].forEach(id => $(`#${id}`).innerHTML = '<b>复算中</b><span>--</span>');
      return;
    }
    const m = report.metric || report.metrics;
    const safety = $('#metricSafety');
    safety.className = `metric ${report.error_count ? 'bad' : report.warning_count ? 'warn' : 'good'}`;
    safety.innerHTML = `<b>${report.error_count ? `${report.error_count} 个错误` : report.warning_count ? `${report.warning_count} 个告警` : '通过'}</b><span>安全状态</span>`;
    const ax = report.axle_loads;
    const axle = $('#metricAxle');
    axle.className = `metric ${ax.some(a => !a.ok) ? 'bad' : ax.some(a => a.utilization >= .9) ? 'warn' : 'good'}`;
    axle.innerHTML = `<b>${ax.map(a => `${a.name[0]} ${Math.round(a.total_kg)}/${Math.round(a.capacity_kg)}`).join(' · ')}</b><span>轴荷（含自重）</span>`;
    $('#metricCg').innerHTML = `<b>${m.x.toFixed(2)} / ${m.y.toFixed(2)} m</b><span>货物重心 x/y（目标 ${m.target_cg_x.toFixed(2)}）</span>`;
    const floor = $('#metricFloor');
    floor.className = `metric ${m.floor_max_point_kg > state.truck.floor_point_limit_kg ? 'bad' : 'warn'}`;
    floor.innerHTML = `<b>${Math.round(m.floor_max_point_kg)}格 / ${Math.round(m.floor_average_kg_m2)}m²</b><span>最大点载kg / 均载kg·m⁻²</span>`;
    const re = $('#metricRehandle');
    re.className = `metric ${m.rehandle_count ? 'warn' : 'good'}`;
    re.innerHTML = `<b>${m.rehandle_count}</b><span>倒箱次数</span>`;
    renderIssueList();
    renderUnload();
  }

  function renderIssueList() {
    const showErr = $('#showErrors').checked, showWarn = $('#showWarnings').checked, showInfo = $('#showInfos').checked;
    const filtered = report.issues.filter(i =>
      i.severity === 'error' ? showErr : i.severity === 'warning' ? showWarn : showInfo);
    $('#issueList').innerHTML = filtered.map((i, n) => `
      <div class="issue ${i.severity}" data-issue="${n}" data-case="${esc(i.case_ids?.[0] || '')}">
        <div><b>${i.severity === 'error' ? '错误' : i.severity === 'warning' ? '警告' : '信息'}</b> <code>${esc(i.code)}</code></div>
        <div>${esc(i.message)}</div>
      </div>`).join('') || '<p class="hint">当前没有匹配的问题。</p>';
  }

  function renderUnload() {
    const groups = new Map();
    report.unloading.steps.forEach(s => {
      if (!groups.has(s.stop_id)) groups.set(s.stop_id, []);
      groups.get(s.stop_id).push(s);
    });
    $('#unloadList').innerHTML = [...groups.entries()].map(([sid, steps]) => {
      const stop = stopById(sid);
      return `<div><b>${esc(stop?.city || sid)}</b>${steps.map(s => `
        <div class="unload-step"><span class="n">${s.step}</span><div>${esc(s.label)}${s.temporary_move_case_ids.length ? `<br><small class="hint">先移：${esc(s.temporary_move_case_ids.join(', '))}</small>` : ''}</div></div>`).join('')}</div>`;
    }).join('');
  }

  async function savePlan(reason = '手工编辑') {
    try {
      const data = await api(`/api/plans/${encodeURIComponent(planId)}`, {
        method: 'PUT', body: JSON.stringify({state, reason}),
      });
      planId = data.plan.id; state = data.plan.state; report = data.report;
      await loadPlans(planId);
      toast(data.plan.latest_version?.reason ? `已保存为 v${data.plan.latest_version.version_no}` : '已保存');
    } catch (err) { toast(err.message, 'error'); }
  }

  async function download(kind) {
    try {
      const res = await fetch('/api/export/' + kind, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({state})});
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || '导出失败');
      }
      const blob = await res.blob();
      const ext = kind === 'layers-json' || kind === 'recompute' ? 'json' : 'md';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `tour-load-${kind}.${ext}`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) { toast(err.message, 'error'); }
  }

  async function loadVersions() {
    if (!planId) return;
    try {
      const data = await api(`/api/plans/${encodeURIComponent(planId)}/versions`);
      $('#versionList').innerHTML = data.versions.map(v => `
        <div class="version-card">
          <header><span>v${v.version_no} · ${v.status === 'confirmed' ? '已确认' : '草稿'}</span><small>${new Date(v.created_at).toLocaleString()}</small></header>
          <p>${esc(v.reason)}</p>
          <div class="affected">${v.affected_case_ids.map(id => `<span>${esc(id)}</span>`).join('') || '<span>无受影响箱体</span>'}</div>
          <div class="button-row"><button data-restore="${v.version_no}">查看/恢复此版本</button></div>
        </div>`).join('');
    } catch (_) { /* versions are loaded after main plan */ }
  }

  // Events
  $('#planSelect').addEventListener('change', async e => {
    const p = plans.find(x => x.id === e.target.value);
    if (p) { setPlanData(p); await loadVersions(); }
  });
  $('#sampleBtn').onclick = async () => {
    const p = await api('/api/plans', {method:'POST', body: JSON.stringify({
      name: `坏方案副本 ${new Date().toLocaleTimeString()}`,
      state: plans.find(p => p.id === 'sample-tour')?.state || state,
    })});
    await loadPlans(p.id); toast('已载入内置触发数据');
  };
  $('#newPlanBtn').onclick = async () => {
    const p = await api('/api/plans', {method:'POST', body: JSON.stringify({name: '新巡演方案', state: {...state, name: '新巡演方案', placements: []}})});
    await loadPlans(p.id);
  };
  $('#autoBtn').onclick = async () => {
    try {
      const data = await api('/api/auto', {method:'POST', body: JSON.stringify(state)});
      state = data.state; report = data.report;
      render(); renderReport();
      toast(report.error_count ? '自动编排完成，但仍有安全冲突' : '自动编排完成：硬约束优先，倒箱已尽量减少');
    } catch (err) { toast(err.message, 'error'); }
  };
  $('#saveBtn').onclick = () => savePlan('手工编辑');
  $('#confirmBtn').onclick = async () => {
    try {
      const force = report?.error_count && confirm('当前仍有安全错误。仍要保留教学演示快照吗？');
      const data = await api(`/api/plans/${planId}/confirm`, {method:'POST', body: JSON.stringify({force, reason:'用户确认快照'})});
      await loadPlans(planId);
      toast('已确认快照');
    } catch (err) { toast(err.message, 'error'); }
  };
  $('#exportBtn').onclick = e => { e.stopPropagation(); $('.export-menu').classList.toggle('open'); };
  document.addEventListener('click', () => $('.export-menu').classList.remove('open'));
  $('#exportMenu').addEventListener('click', e => { const k = e.target.dataset.export; if (k) download(k); });

  $('#planName').addEventListener('change', e => { state.name = e.target.value; scheduleAnalyze(); });
  $('#caseTable').addEventListener('click', e => {
    const tr = e.target.closest('tr[data-case]'); if (!tr) return;
    const c = caseById(tr.dataset.case);
    if (e.target.dataset.action === 'select') selectedId = c.id;
    if (e.target.dataset.action === 'delete') state.placements = state.placements.filter(p => p.case_id !== c.id);
    if (e.target.dataset.action === 'place') {
      const p = {case_id:c.id, x:0,y:0,z:0,orientation:(c.allowed_orientations||['LWH'])[0], locked:false};
      selectedId = c.id; ensurePlacement(p, c); return;
    }
    render(); scheduleAnalyze();
  });

  $('#stopList').addEventListener('click', e => {
    const id = e.target.dataset.moveStop;
    if (id) {
      const i = state.stops.findIndex(s => s.id === id), d = Number(e.target.dataset.dir);
      const j = i + d;
      if (j >= 0 && j < state.stops.length) {
        [state.stops[i], state.stops[j]] = [state.stops[j], state.stops[i]];
        mutate(state);
      }
    }
    const removeId = e.target.dataset.removeStop;
    if (removeId) {
      if (state.stops.length <= 1) return toast('至少保留一个站点', 'warn');
      if (!confirm('删除站点后，该站点箱体将改挂第一站。')) return;
      const fallback = state.stops.find(s => s.id !== removeId).id;
      state.cases.forEach(c => { if (c.stop_id === removeId) c.stop_id = fallback; });
      state.stops = state.stops.filter(s => s.id !== removeId);
      mutate(state);
    }
  });
  $('#stopList').addEventListener('change', e => {
    const id = e.target.dataset.stopCity;
    if (id) stopById(id).city = e.target.value;
    renderCases(); scheduleAnalyze();
  });
  $('#addStopBtn').onclick = () => {
    state.stops.push({id: `stop-${Date.now()}`, city: `新城市 ${state.stops.length+1}`, venue: ''});
    mutate(state);
  };

  ['#truckLength','#truckWidth','#truckHeight','#floorLimit','#pointLimit','#gvwLimit'].forEach(sel => {
    $(sel).addEventListener('change', () => {
      const t = state.truck;
      t.length = +$('#truckLength').value; t.width = +$('#truckWidth').value; t.height = +$('#truckHeight').value;
      t.floor_limit_kg_m2 = +$('#floorLimit').value; t.floor_point_limit_kg = +$('#pointLimit').value; t.gvw_limit_kg = +$('#gvwLimit').value;
      state.placements.forEach(p => { const c = caseById(p.case_id); if (c) clampPlacement(p, c); });
      mutate(state);
    });
  });
  ['#doorWidth','#doorHeight','#doorSill'].forEach(sel => {
    $(sel).addEventListener('change', () => {
      state.truck.door.width = +$('#doorWidth').value; state.truck.door.height = +$('#doorHeight').value; state.truck.door.sill = +$('#doorSill').value;
      mutate(state);
    });
  });
  $('#axleEditor').addEventListener('input', e => {
    const i = e.target.dataset.axle;
    if (i === undefined) return;
    const k = e.target.dataset.k;
    state.truck.axles[i][k] = k === 'name' ? e.target.value : Number(e.target.value);
    if (k === 'position') state.truck.axles.sort((a,b) => a.position - b.position);
    renderAxles(); scheduleAnalyze();
  });
  $('#axleEditor').addEventListener('click', e => {
    const i = e.target.dataset.removeAxle;
    if (i !== undefined) { state.truck.axles.splice(Number(i), 1); mutate(state); }
  });
  $('#addAxleBtn').onclick = () => {
    state.truck.axles.push({name:`轴${state.truck.axles.length+1}`, position: state.truck.length/2, tare_kg:0, capacity_kg:5000});
    state.truck.axles.sort((a,b)=>a.position-b.position); mutate(state);
  };

  $('#applyJsonBtn').onclick = () => {
    try {
      const parsed = JSON.parse($('#jsonInput').value);
      const next = parsed.state || parsed;
      if (!next.truck || !Array.isArray(next.cases)) throw new Error('JSON 至少需要 truck 与 cases');
      state = next; mutate(state);
      toast('JSON 已应用并复算');
    } catch (err) { toast(err.message, 'error'); }
  };
  $('#copyJsonBtn').onclick = async () => {
    $('#jsonInput').value = JSON.stringify(state, null, 2);
    try { await navigator.clipboard.writeText($('#jsonInput').value); toast('已复制当前 JSON'); }
    catch { toast('已填入文本框，可手动复制'); }
  };
  $('#versionList')?.addEventListener('click', async e => {
    const no = e.target.dataset.restore;
    if (!no) return;
    const v = await api(`/api/plans/${planId}/versions/${no}`);
    state = v.state; selectedId = null; render(); scheduleAnalyze();
    toast(`已载入 v${no}，保存后会成为当前新版本`);
  });
  $('.tabs').addEventListener('click', e => {
    const tab = e.target.dataset.tab; if (!tab) return;
    $$('.tabs button').forEach(b => b.classList.toggle('active', b === e.target));
    $$('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.panel === tab));
    if (tab === 'data') $('#jsonInput').value = JSON.stringify(state, null, 2);
    if (tab === 'versions') loadVersions();
  });
  $('#issueList').addEventListener('click', e => {
    const item = e.target.closest('[data-case]');
    if (item?.dataset.case) { selectedId = item.dataset.case; renderCases(); renderViews(); }
  });
  ['showErrors','showWarnings','showInfos'].forEach(id => $('#'+id).addEventListener('change', renderIssueList));

  loadPlans().catch(err => toast(err.message, 'error'));
})();
