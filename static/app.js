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
  let selectedLashId = null;
  let selectedAnchorId = null;
  let tieMode = false;
  let tie = null; // in-progress strap connection

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];
  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
  const snap = (v) => Math.round(v / SNAP) * SNAP;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ---------------- lashing helpers ----------------
  function anchorById(id) { return (state.truck.anchors || []).find(a => a.id === id); }
  function lashById(id) { return (state.lashings || []).find(l => l.id === id); }
  function faceNormal(f) {
    return ({'-x':[-1,0,0],'+x':[1,0,0],'-y':[0,-1,0],'+y':[0,1,0]})[f] || [0,0,0];
  }
  function caseFacePoint(b, face, u = .5, v = .5) {
    const {x, y, z, dx, dy, dz} = b;
    if (face === '-x') return [x, y + u*dy, z + v*dz];
    if (face === '+x') return [x+dx, y + u*dy, z + v*dz];
    if (face === '-y') return [x + u*dx, y, z + v*dz];
    if (face === '+y') return [x + u*dx, y+dy, z + v*dz];
    return [x+dx/2, y+dy/2, z+dz/2];
  }
  function lashEndpoints(l) {
    const ep = (e) => {
      if (e.kind === 'anchor') {
        const a = anchorById(e.id);
        return a ? [+a.x, +a.y, +a.z] : null;
      }
      const c = caseById(e.id);
      const b = c ? box(c) : null;
      return b ? caseFacePoint(b, e.face, +e.u, +e.v) : null;
    };
    return {p0: ep(l.from), p1: ep(l.to)};
  }
  function strapRow(id) { return report?.lashing?.straps?.find(s => s.id === id); }
  function anchorRow(id) { return report?.lashing?.anchors?.find(a => a.id === id); }
  function lashColor(l) {
    const row = strapRow(l.id);
    if (!l.locked) return '#64748b';
    if (row?.pending) return '#7c3aed';
    if ((row?.utilization || 0) > 1 || (row?.codes || []).includes('LASH_OVERLOAD')) return '#dc2626';
    if ((row?.codes || []).length || (row?.utilization || 0) >= .9) return '#d97706';
    return '#059669';
  }
  function anchorColor(id) {
    const row = anchorRow(id);
    if (!row) return '#2563eb';
    if (row.capacity_kg && row.load_kg > row.capacity_kg + 1e-6) return '#dc2626';
    if (row.capacity_kg && row.load_kg / row.capacity_kg >= .9) return '#d97706';
    return '#2563eb';
  }
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
    selectedId = null; selectedLashId = null; selectedAnchorId = null;
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
        renderLashingUI();
      } catch (err) {
        toast(err.message, 'error');
      }
    }, 160);
  }

  function render() {
    if (!state) return;
    if (!state.truck.anchors) state.truck.anchors = [];
    if (!state.truck.accel) state.truck.accel = {forward:.8,rearward:.5,lateral:.5,up:.3,down:1};
    if (!state.lashings) state.lashings = [];
    renderTruckForm(false);
    renderCases();
    renderStops();
    renderAxles();
    renderAnchors();
    renderLashingUI();
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
    const lc = report?.lashing?.cases ? Object.fromEntries(report.lashing.cases.map(c => [c.case_id, c])) : {};
    tbody.innerHTML = state.cases.map(c => {
      const p = placement(c.id);
      const stop = stopById(c.stop_id);
      let lashTag = '';
      if (p) {
        const cr = lc[c.id];
        const n = cr?.lashing_ids?.length || 0;
        const minM = cr?.min_margin;
        const cls = minM == null ? '' : minM < 1 ? 'bad' : minM < 1.15 ? 'warn' : 'good';
        lashTag = `<span class="lash-tag ${cls}">🔗${n}${minM != null ? ` ·${minM.toFixed(2)}` : ''}</span>`;
      }
      const status = p
        ? `${Number(p.x).toFixed(2)}/${Number(p.y).toFixed(2)}/${Number(p.z).toFixed(2)}<br>${c.weight_kg}kg ${p.locked ? '🔒' : ''}${lashTag}`
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
    const faces = ['-x','+x','-y','+y'];
    const faceChk = faces.map(f =>
      `<label class="chk"><input type="checkbox" data-case-face="${f}" ${(c.lash_faces||faces).includes(f)?'checked':''}>${f}</label>`).join(' ');
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
        <label>摩擦系数 μ<input type="number" step=".05" min="0" max="1" data-field="friction" value="${round(c.friction ?? .35)}"></label>
      </div>
      <div class="face-row"><span class="hint">系固面：</span>${faceChk}</div>
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
    card.querySelector('[data-field="friction"]').addEventListener('change', e => {
      c.friction = clamp(Number(e.target.value) || 0, 0, 1); persistSelectedEdit(p);
    });
    card.querySelectorAll('[data-case-face]').forEach(chk => chk.addEventListener('change', () => {
      const set = new Set(c.lash_faces || ['-x','+x','-y','+y']);
      const f = chk.dataset.caseFace;
      chk.checked ? set.add(f) : set.delete(f);
      c.lash_faces = ['-x','+x','-y','+y'].filter(f => set.has(f));
      persistSelectedEdit(p);
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
    if (t.accel) {
      set('#accelFwd', t.accel.forward); set('#accelRear', t.accel.rearward);
      set('#accelLat', t.accel.lateral); set('#accelUp', t.accel.up);
    }
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

  // Shared SVG overlay: straps + anchors in each projection.
  function strapNode(l, x1, y1, x2, y2) {
    const color = lashColor(l);
    const sel = l.id === selectedLashId ? ' selected' : '';
    const dash = l.locked ? '' : 'stroke-dasharray="7 4"';
    return `<g class="lash-node${sel}" data-lash="${esc(l.id)}">
      <line class="lash-hit" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>
      <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="2.3" opacity=".92" ${dash}/></g>`;
  }
  function anchorNode(a, cx, cy, r = 5) {
    const color = anchorColor(a.id);
    const sel = a.id === selectedAnchorId ? ' anchor-selected' : '';
    return `<g class="anchor-node${sel}" data-anchor="${esc(a.id)}">
      <circle cx="${cx}" cy="${cy}" r="${r+4}" fill="transparent"/>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="#fff" stroke="${color}" stroke-width="2.6"/>
      <text x="${cx+6}" y="${cy+3}" font-size="9" fill="${color}" pointer-events="none">${esc(a.id)}</text></g>`;
  }
  function lashOverlayTop(x0, y0, s) {
    return state.lashings.map(l => {
      const {p0, p1} = lashEndpoints(l);
      if (!p0 || !p1) return '';
      return strapNode(l, x0+p0[0]*s, y0+p0[1]*s, x0+p1[0]*s, y0+p1[1]*s);
    }).join('');
  }
  function anchorOverlayTop(x0, y0, s) {
    return state.truck.anchors.map(a =>
      anchorNode(a, x0+a.x*s, y0+a.y*s, a.surface === 'floor' ? 5 : 4)).join('');
  }
  function lashOverlaySide(x0, y0, s, t) {
    return state.lashings.map(l => {
      const {p0, p1} = lashEndpoints(l);
      if (!p0 || !p1) return '';
      return strapNode(l, x0+p0[0]*s, y0+(t.height-p0[2])*s, x0+p1[0]*s, y0+(t.height-p1[2])*s);
    }).join('');
  }
  function anchorOverlaySide(x0, y0, s, t) {
    return state.truck.anchors.map(a =>
      anchorNode(a, x0+a.x*s, y0+(t.height-a.z)*s, 4.5)).join('');
  }
  function lashOverlayTail(x0, y0, s, t) {
    return state.lashings.map(l => {
      const {p0, p1} = lashEndpoints(l);
      if (!p0 || !p1) return '';
      return strapNode(l, x0+p0[1]*s, y0+(t.height-p0[2])*s, x0+p1[1]*s, y0+(t.height-p1[2])*s);
    }).join('');
  }
  function anchorOverlayTail(x0, y0, s, t) {
    return state.truck.anchors.map(a =>
      anchorNode(a, x0+a.y*s, y0+(t.height-a.z)*s, 4.5)).join('');
  }
  // Rubber-band preview while connecting in tie mode.
  function tieLineTop(x0, y0, s) { return tieRubber((p)=>[x0+p[0]*s, y0+p[1]*s], s); }
  function tieRubber(proj) {
    if (!tie || !tie.startPoint) return '';
    const [x1, y1] = proj(tie.startPoint);
    return `<line x1="${x1}" y1="${y1}" x2="${tie.cur[0]}" y2="${tie.cur[1]}" stroke="#7c3aed" stroke-width="2" stroke-dasharray="4 3"/>`;
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
    html += lashOverlayTop(x0, y0, s);
    html += anchorOverlayTop(x0, y0, s);
    if (tie) html += tieLineTop(x0, y0, s);
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
    html += lashOverlaySide(x0, y0, s, t);
    html += anchorOverlaySide(x0, y0, s, t);
    if (tie) html += tieRubber((p)=>[x0+p[0]*s, y0+(t.height-p[2])*s]);
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
    html += lashOverlayTail(x0, y0, s, t);
    html += anchorOverlayTail(x0, y0, s, t);
    if (tie) html += tieRubber((p)=>[x0+p[1]*s, y0+(t.height-p[2])*s]);
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

  function svgScale(view) { return view === 'tailView' ? 120 : 72; }
  function viewName(svg) { return svg.closest('.svg-box').id; }

  function startTieFrom(evt, svg, end) {
    const view = viewName(svg);
    let point = null;
    if (end.kind === 'anchor') {
      const a = anchorById(end.id);
      point = [+a.x, +a.y, +a.z];
    } else {
      const b = box(caseById(end.id));
      point = caseFacePoint(b, end.face, end.u, end.v);
    }
    const pt = svgPoint(evt, svg);
    tie = {view, from: end, startPoint: point, cur: [pt.x, pt.y]};
    svg.setPointerCapture(evt.pointerId);
    renderViews();
  }

  function finishTieOn(evt, svg) {
    const view = viewName(svg);
    const anchorG = evt.target.closest('[data-anchor]');
    const caseG = evt.target.closest('[data-id]');
    let to = null;
    if (anchorG) {
      to = {kind: 'anchor', id: anchorG.dataset.anchor, face: '', u: .5, v: .5};
    } else if (caseG) {
      const c = caseById(caseG.dataset.id);
      const b = box(c);
      const scale = svgScale(view);
      // decide face from release position relative to box rect
      const pt = svgPoint(evt, svg);
      let face, u, v;
      if (view === 'topView') {
        const bx = 38 + b.x*scale, by = 34 + b.y*scale;
        const dxl = (pt.x - bx) / scale, dxr = (bx + b.dx*scale - pt.x) / scale;
        const dyl = (pt.y - by) / scale, dyr = (by + b.dy*scale - pt.y) / scale;
        face = Math.min(dxl, dxr, dyl, dyr) === dxl ? '-x' : Math.min(dxl,dxr,dyl,dyr) === dxr ? '+x'
             : Math.min(dxl,dxr,dyl,dyr) === dyl ? '-y' : '+y';
        u = .5; v = .55;
      } else if (view === 'sideView') {
        const bx = 38 + b.x*scale;
        face = pt.x < bx + b.dx*scale/2 ? '-x' : '+x';
        u = .5; v = clamp((b.z + b.dz - (state.truck.height - (pt.y-30)/scale)) / Math.max(b.dz,.01), .1, .95);
      } else {
        const by = 38 + b.y*scale;
        face = pt.x < by + b.dy*scale/2 ? '-y' : '+y';
        u = .5; v = clamp((b.z + b.dz - (state.truck.height - (pt.y-25)/scale)) / Math.max(b.dz,.01), .1, .95);
      }
      if (!(c.lash_faces || []).includes(face)) {
        toast(`${c.label} 的 ${face} 面不允许系固`, 'warn');
        tie = null; renderViews(); return;
      }
      to = {kind: 'case', id: c.id, face, u, v};
    }
    if (!to) { tie = null; renderViews(); return; }
    if (tie.from.kind === to.kind && tie.from.id === to.id && tie.from.face === to.face) {
      toast('绑带两端不能是同一点', 'warn'); tie = null; renderViews(); return;
    }
    if (tie.from.kind === 'anchor' && to.kind === 'anchor') {
      toast('绑带至少一端必须接箱体', 'warn'); tie = null; renderViews(); return;
    }
    const lash = {
      id: 'L' + Date.now().toString(36).toUpperCase() + Math.floor(Math.random()*900+100),
      label: '新绑带',
      from: tie.from, to,
      pretension_kg: state.truck.strap_defaults?.pretension_kg ?? 200,
      capacity_kg: state.truck.strap_defaults?.capacity_kg ?? 1000,
      locked: false, review_signature: '',
    };
    state.lashings.push(lash);
    selectedLashId = lash.id;
    tie = null;
    mutate(state);
    switchToLashingTab();
  }

  function switchToLashingTab() {
    const btn = $('.tabs button[data-tab="lashing"]');
    if (btn) btn.click();
  }

  function bindSvgDrag() {
    $$('.svg-box svg').forEach(svg => {
      if (svg.dataset.bound === '1') return;
      svg.dataset.bound = '1';
      svg.addEventListener('pointerdown', evt => {
        const view = viewName(svg);
        svg.closest('.svg-box').classList.toggle('tying', tieMode);
        const lashG = evt.target.closest('[data-lash]');
        const anchorG = evt.target.closest('[data-anchor]');
        // Selecting a strap.
        if (lashG && !tieMode) {
          selectedLashId = lashG.dataset.lash;
          selectedId = null; selectedAnchorId = null;
          renderCases(); renderLashingUI(); switchToLashingTab();
          return;
        }
        // Selecting / dragging from an anchor.
        if (anchorG) {
          selectedAnchorId = anchorG.dataset.anchor;
          if (tieMode) {
            startTieFrom(evt, svg, {kind: 'anchor', id: anchorG.dataset.anchor, face: '', u: .5, v: .5});
          } else {
            renderAnchors();
          }
          return;
        }
        const g = evt.target.closest('[data-id]');
        if (!g) return;
        const c = caseById(g.dataset.id);
        const p = placement(c.id);
        // Tie mode: start a case-end connection from the touched face.
        if (tieMode) {
          const scale = svgScale(view);
          const pt = svgPoint(evt, svg);
          const b = box(c);
          let face, u, v;
          if (view === 'topView') {
            const bx = 38 + b.x*scale, by = 34 + b.y*scale;
            const dxl = (pt.x - bx)/scale, dxr = (bx + b.dx*scale - pt.x)/scale;
            const dyl = (pt.y - by)/scale, dyr = (by + b.dy*scale - pt.y)/scale;
            const m = Math.min(dxl, dxr, dyl, dyr);
            face = m === dxl ? '-x' : m === dxr ? '+x' : m === dyl ? '-y' : '+y';
            u = .5; v = .55;
          } else if (view === 'sideView') {
            const bx = 38 + b.x*scale;
            face = pt.x < bx + b.dx*scale/2 ? '-x' : '+x';
            u = .5; v = clamp((b.z + b.dz - (state.truck.height - (pt.y-30)/scale)) / Math.max(b.dz,.01), .1, .95);
          } else {
            const by = 38 + b.y*scale;
            face = pt.x < by + b.dy*scale/2 ? '-y' : '+y';
            u = .5; v = clamp((b.z + b.dz - (state.truck.height - (pt.y-25)/scale)) / Math.max(b.dz,.01), .1, .95);
          }
          if (!(c.lash_faces || []).includes(face)) {
            toast(`${c.label} 的 ${face} 面不允许系固`, 'warn'); return;
          }
          startTieFrom(evt, svg, {kind: 'case', id: c.id, face, u, v});
          return;
        }
        selectedId = c.id;
        selectedLashId = null; selectedAnchorId = null;
        renderCases(); renderLashingUI();
        if (p.locked) { toast('该箱体已锁定；如需移动请先取消锁定。', 'warn'); return; }
        const b = box(c);
        drag = {
          id: c.id, view, g, svg,
          startX: evt.clientX, startY: evt.clientY,
          ox: p.x, oy: p.y, oz: p.z, b,
        };
        g.setPointerCapture(evt.pointerId);
      });
      svg.addEventListener('pointermove', evt => {
        if (tie && tie.view === viewName(svg)) {
          const pt = svgPoint(evt, svg);
          tie.cur = [pt.x, pt.y];
          renderViews();
          return;
        }
        if (!drag || drag.svg !== svg) return;
        const c = caseById(drag.id), p = placement(drag.id);
        const scale = svgScale(drag.view);
        const dxm = (evt.clientX - drag.startX) / scale;
        const dym = (evt.clientY - drag.startY) / scale;
        if (drag.view === 'topView') { p.x = snap(drag.ox + dxm); p.y = snap(drag.oy + dym); }
        if (drag.view === 'sideView') { p.x = snap(drag.ox + dxm); p.z = snap(drag.oz - dym); }
        if (drag.view === 'tailView') { p.y = snap(drag.oy + dxm); p.z = snap(drag.oz - dym); }
        clampPlacement(p, c);
        renderViews();
      });
      svg.addEventListener('pointerup', evt => {
        if (tie && tie.view === viewName(svg)) { finishTieOn(evt, svg); return; }
        if (drag) { scheduleAnalyze(); drag = null; }
      });
      svg.addEventListener('pointercancel', () => { drag = null; tie = null; renderViews(); });
    });
  }

  function fmtKg(v) { return `${Math.round(v)}kg`; }

  function endLabel(ep) {
    if (ep.kind === 'anchor') {
      const a = anchorById(ep.id);
      return a ? a.label || a.id : '(缺失锚点)';
    }
    const c = caseById(ep.id);
    return c ? `${c.label} ${ep.face}` : '(缺失箱体)';
  }

  function renderLashingUI() {
    const lash = report?.lashing;
    const badge = $('#schemeBadge');
    if (lash) {
      const pending = lash.pending_ids?.length || 0;
      badge.textContent = `方案 ${lash.scheme_version?.slice(0,8) || '----'} · 锁定 ${lash.locked_count}/${lash.total_count}${pending ? ` · ${pending} 待复核` : ''}`;
      badge.className = 'scheme-badge' + (pending ? ' pending' : '');
    } else {
      badge.textContent = '方案 复算中';
      badge.className = 'scheme-badge';
    }
    $('#lashCount').textContent = `${state.lashings.length} 条`;
    renderLashEditor();
    renderLashList();
    renderLegs();
    renderRelease();
  }

  function renderLashEditor() {
    const el = $('#lashEditor');
    const l = selectedLashId ? lashById(selectedLashId) : null;
    if (!l) {
      el.className = 'lash-editor empty';
      el.textContent = '在视图中点选一条绑带进行编辑，或用下方按钮新建。';
      return;
    }
    const row = strapRow(l.id);
    const status = row?.pending ? '待复核' : l.locked ? '已锁定' : '草稿';
    const codes = (row?.codes || []).map(c => `<code class="${['LASH_ANGLE','LASH_TENSION_MARGIN'].includes(c)?'warn':''}">${c}</code>`).join('');
    const anchorOpts = state.truck.anchors.map(a => `<option value="${esc(a.id)}">${esc(a.id)} · ${esc(a.label||'')}</option>`).join('');
    const caseOpts = state.cases.map(c => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');
    const endSel = (ep, which) => ep.kind === 'anchor'
      ? `<label>端点${which}<select data-le="${which}" data-k="kind_anchor"><option value="anchor" selected>锚点</option><option value="case">箱体</option></select></label>
         <label>锚点<select data-le="${which}" data-k="id_anchor">${state.truck.anchors.map(a=>`<option value="${esc(a.id)}" ${a.id===ep.id?'selected':''}>${esc(a.id)}</option>`).join('')}</select></label>`
      : `<label>端点${which}<select data-le="${which}" data-k="kind_case"><option value="anchor">锚点</option><option value="case" selected>箱体</option></select></label>
         <label>箱体<select data-le="${which}" data-k="id_case">${state.cases.map(c=>`<option value="${esc(c.id)}" ${c.id===ep.id?'selected':''}>${esc(c.label)}</option>`).join('')}</select></label>
         <label>系固面<select data-le="${which}" data-k="face">${['-x','+x','-y','+y'].map(f=>`<option ${f===ep.face?'selected':''}>${f}</option>`).join('')}</select></label>
         <label>沿面 u<input type="number" step=".05" min="0" max="1" data-le="${which}" data-k="u" value="${round(ep.u)}"></label>
         <label>高度 v<input type="number" step=".05" min="0" max="1" data-le="${which}" data-k="v" value="${round(ep.v)}"></label>`;
    el.className = 'lash-editor';
    el.innerHTML = `
      <div class="le-row"><h4>${esc(l.label || l.id)}</h4><span class="le-status ${l.locked?'locked':'draft'} ${row?.pending?'pending':''}">${status}${row?.pending?' 🔶':''}</span></div>
      <input data-le="meta" data-k="label" value="${esc(l.label)}" placeholder="绑带名称">
      <div class="le-row">
        <label>预紧力 kg<input type="number" step="10" min="0" data-le="meta" data-k="pre" value="${round(l.pretension_kg)}"></label>
        <label>额定 kg<input type="number" step="50" min="0" data-le="meta" data-k="cap" value="${round(l.capacity_kg)}"></label>
        <button data-le-action="${l.locked?'unlock':'lock'}" class="${l.locked?'':'primary'}">${l.locked?'解锁复核':'锁定并盖章'}</button>
        <button data-le-action="delete" class="danger">删除</button>
      </div>
      <div class="le-row">${endSel(l.from, 'A')}</div>
      <div class="le-row">${endSel(l.to, 'B')}</div>
      <div class="le-codes">${codes || '<span class="hint">无几何/容量问题</span>'}</div>
      <div class="hint">${row ? `长度 ${(row.length_m||0).toFixed(2)} m · 夹角 ${(row.angle_deg||0).toFixed(0)}° · 工作张力 ${Math.round(row.tension_kg||0)}/${Math.round(row.capacity_kg||0)} kg` : '复算中'}</div>`;
    bindLashEditor(l);
  }

  function bindLashEditor(l) {
    const el = $('#lashEditor');
    el.querySelectorAll('[data-le-action]').forEach(btn => btn.onclick = async () => {
      const act = btn.dataset.leAction;
      if (act === 'delete') {
        state.lashings = state.lashings.filter(x => x.id !== l.id);
        selectedLashId = null;
        mutate(state); return;
      }
      if (act === 'lock') await lockLashings([l.id]);
      if (act === 'unlock') { l.locked = false; l.review_signature = ''; mutate(state); }
    });
    el.querySelectorAll('[data-le="meta"]').forEach(inp => inp.addEventListener('change', () => {
      const k = inp.dataset.k;
      if (k === 'label') l.label = inp.value;
      if (k === 'pre') l.pretension_kg = Math.max(0, +inp.value);
      if (k === 'cap') l.capacity_kg = Math.max(0, +inp.value);
      mutate(state);
    }));
    el.querySelectorAll('[data-le="A"],[data-le="B"]').forEach(inp => {
      inp.addEventListener('change', () => {
        const which = inp.dataset.le === 'A' ? 'from' : 'to';
        const k = inp.dataset.k, ep = l[which];
        if (k === 'kind_anchor' || k === 'kind_case') {
          const kind = inp.value;
          if (kind === 'anchor') l[which] = {kind:'anchor', id: state.truck.anchors[0]?.id || '', face:'', u:.5, v:.5};
          else l[which] = {kind:'case', id: state.cases[0]?.id || '', face:'-x', u:.5, v:.5};
        } else if (k === 'id_anchor') { ep.id = inp.value; }
        else if (k === 'id_case') { ep.id = inp.value; }
        else if (k === 'face') { ep.face = inp.value; }
        else if (k === 'u' || k === 'v') { ep[k] = clamp(+inp.value, 0, 1); }
        mutate(state);
      });
    });
  }

  function renderLashList() {
    const list = $('#lashList');
    if (!state.lashings.length) { list.innerHTML = '<p class="hint">尚无绑带。勾选“绑扎模式”后在三视图中拖接，或点击新建。</p>'; return; }
    list.innerHTML = state.lashings.map(l => {
      const row = strapRow(l.id);
      const cls = !l.locked ? 'draft' : row?.pending ? 'pending'
        : (row?.utilization > 1 || (row?.codes||[]).includes('LASH_OVERLOAD')) ? 'over'
        : (row?.codes?.length || row?.utilization >= .9) ? 'warn' : 'locked';
      const codes = [...new Set(row?.codes || [])].slice(0, 3).map(c => `<code>${c}</code>`).join('');
      return `<div class="lash-item ${cls} ${l.id===selectedLashId?'selected':''}" data-lashrow="${esc(l.id)}">
        <div class="li-top"><span>${esc(l.label || l.id)}</span><span>${l.locked?(row?.pending?'待复核':'🔒'):'草稿'}</span></div>
        <div class="li-meta">${esc(endLabel(l.from))} → ${esc(endLabel(l.to))} · ${Math.round(l.pretension_kg)}/${Math.round(l.capacity_kg)} kg
        ${row ? `· 张力 ${Math.round(row.tension_kg||0)} kg · ${Math.round(row.angle_deg||0)}°` : ''}</div>
        <div class="li-codes">${codes}</div></div>`;
    }).join('');
  }

  function marginClass(v) { return v == null ? '' : v < 1 ? 'bad' : v < 1.15 ? 'warn' : 'good'; }
  function marginTxt(v) { return v == null ? '—' : v.toFixed(2); }

  function renderLegs() {
    const lash = report?.lashing;
    const el = $('#legList');
    const ffEl = $('#firstFailure');
    if (!lash) { el.innerHTML = '<p class="hint">复算中…</p>'; ffEl.innerHTML = ''; return; }
    const ff = lash.first_failure;
    if (ff) {
      const s = ff.suggestion;
      ffEl.className = 'first-failure bad';
      ffEl.innerHTML = `<b>首个失效：${esc(ff.stage_title)}</b> · ${esc(ff.label)} <code>${esc(ff.code)}</code>
        <div>${esc(s?.message || '')}</div>
        ${s?.type === 'add_lashing' ? `<button data-suggest='${esc(JSON.stringify(s))}'>按建议加绑带</button>` : ''}`;
    } else {
      ffEl.className = 'first-failure good';
      ffEl.innerHTML = '<b>各航段系固校核通过</b>（发车前及每站卸货后的剩余载荷均满足防滑/防倾覆要求）';
    }
    el.innerHTML = lash.stages.map((st, i) => {
      const fails = st.failures || [];
      const isFirstFail = lash.first_failure && lash.first_failure.stage_key === st.key && fails.length;
      const ms = (label, v) => v == null ? '' : `<span>${label} <b class="${marginClass(v)}">${marginTxt(v)}</b></span>`;
      let body = `<div class="margins">${ms('防滑', st.min_slip_margin)}${ms('防倾覆', st.min_tip_margin)}${ms('防跳起', st.min_lift_margin)}<span>剩余 ${Math.round(st.remaining_mass_kg||0)} kg</span></div>`;
      if (st.empty) body += '<div class="hint">空车</div>';
      if (fails.length) body += fails.slice(0, 4).map(f => `<div class="leg-fail">✗ ${esc(f.label)} · ${f.code}</div>`).join('');
      if (isFirstFail && lash.first_failure.suggestion) {
        const s = lash.first_failure.suggestion;
        body += `<div class="leg-suggest">增绑建议：${esc(s.message)}
          ${s.type === 'add_lashing' ? `<button data-suggest='${esc(JSON.stringify(s))}'>按建议加绑带</button>` : ''}</div>`;
      }
      return `<div class="leg-card ${fails.length?'bad':'ok'}"><h4><span>${esc(st.title)}</span><span>${fails.length?`${fails.length} 项失效`:'✓'}</span></h4>${body}</div>`;
    }).join('');
  }

  function renderRelease() {
    const lash = report?.lashing;
    const el = $('#releaseList');
    $('#releaseScheme').textContent = lash ? `方案 ${lash.scheme_version?.slice(0,8)}` : '';
    if (!lash) { el.innerHTML = ''; return; }
    el.innerHTML = lash.release_steps.map(rs => `
      <div class="release-stop"><h4>${esc(rs.city)}（${rs.lashings.length} 条）</h4>
      ${rs.lashings.length ? rs.lashings.map((l,i)=>`<div class="rl"><span class="n">${i+1}</span><span>${l.locked?'🔒':'◇'} ${esc(l.label)} <small class="hint">${esc(l.case_labels.join(', '))}</small></span></div>`).join('') : '<div class="hint">本站无需解绑带</div>'}
      </div>`).join('');
  }

  async function lockLashings(ids, lock = true) {
    try {
      const data = await api('/api/lashing/lock', {method:'POST', body: JSON.stringify({state, ids, lock})});
      state = data.state; report = data.report;
      render(); renderReport();
      toast(lock ? `已锁定 ${data.changed.length} 条并盖章（方案 ${data.scheme_version.slice(0,8)}）` : `已解锁 ${data.changed.length} 条`);
    } catch (err) { toast(err.message, 'error'); }
  }

  function renderAnchors() {
    const el = $('#anchorEditor');
    el.innerHTML = state.truck.anchors.map((a, i) => {
      const row = anchorRow(a.id);
      const used = row ? `${Math.round(row.load_kg)}/${Math.round(a.capacity_kg)}` : '';
      const color = anchorColor(a.id);
      return `<div class="anchor-row" data-anchor-row="${i}">
        <label>ID / 名称<input data-anchor="${i}" data-k="label" value="${esc(a.label||a.id)}"></label>
        <label>x<input type="number" step=".05" data-anchor="${i}" data-k="x" value="${round(a.x)}"></label>
        <label>y<input type="number" step=".05" data-anchor="${i}" data-k="y" value="${round(a.y)}"></label>
        <label>z<input type="number" step=".05" data-anchor="${i}" data-k="z" value="${round(a.z)}"></label>
        <label>额定 kg<input type="number" step="50" data-anchor="${i}" data-k="cap" value="${round(a.capacity_kg)}"></label>
        <label>面<select data-anchor="${i}" data-k="surface">
          ${['floor','wall_l','wall_r','front'].map(s=>`<option value="${s}" ${a.surface===s?'selected':''}>${({floor:'地板',wall_l:'左墙',wall_r:'右墙',front:'前墙'})[s]}</option>`).join('')}</select></label>
        <label>共用组<input data-anchor="${i}" data-k="group" value="${esc(a.group||'')}"></label>
        <label>组容量<input type="number" step="50" data-anchor="${i}" data-k="gcap" value="${round(a.group_capacity_kg||0)}"></label>
        <div class="dir-label">方向<div class="dirs">${['+x','-x','+y','-y','+z'].map(d=>`<label class="chk"><input type="checkbox" data-anchor="${i}" data-dir="${d}" ${(a.directions||[]).includes(d)?'checked':''}>${d}</label>`).join('')}</div></div>
        <button data-remove-anchor="${i}" class="danger">×</button>
        <small style="grid-column:1/-1;color:${color}">${esc(a.id)} 受力 ${used || '0'} kg</small>
      </div>`;
    }).join('');
  }

  function renderReport() {
    if (!report) {
      ['metricSafety','metricAxle','metricCg','metricFloor','metricLash','metricRehandle'].forEach(id => $(`#${id}`).innerHTML = '<b>复算中</b><span>--</span>');
      renderLashingUI();
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
    const lash = report.lashing;
    const lm = $('#metricLash');
    if (lash) {
      const slip = lash.min_slip_margin, tip = lash.min_tip_margin;
      const bad = (slip != null && slip < 1) || (tip != null && tip < 1);
      const warn = (slip != null && slip < 1.15) || (tip != null && tip < 1.15);
      lm.className = `metric ${lash.first_failure || bad ? 'bad' : warn ? 'warn' : 'good'}`;
      lm.innerHTML = `<b>${slip==null?'—':slip.toFixed(2)} / ${tip==null?'—':tip.toFixed(2)}</b><span>防滑 / 倾覆余量 · 锁定${lash.locked_count}/${lash.total_count}</span>`;
    }
    renderIssueList();
    renderUnload();
    renderLashingUI();
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

  // ---------------- lashing events ----------------
  $('#tieMode').addEventListener('change', e => {
    tieMode = e.target.checked;
    $$('.svg-box').forEach(b => b.classList.toggle('tying', tieMode));
    if (tieMode) toast('绑扎模式：从锚点/箱体边面拖到另一端；Esc 取消', '');
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && tie) { tie = null; renderViews(); }
  });
  $('#addLashBtn').onclick = () => {
    const a0 = state.truck.anchors[0];
    const c0 = allBoxes()[0];
    if (!a0) return toast('请先在“车辆”页添加锚点', 'warn');
    if (!c0) return toast('请先放置箱体', 'warn');
    const face = (c0.case.lash_faces || ['-x'])[0];
    const lash = {
      id: 'L' + Date.now().toString(36).toUpperCase() + Math.floor(Math.random()*900+100),
      label: '新绑带',
      from: {kind:'anchor', id:a0.id, face:'', u:.5, v:.5},
      to: {kind:'case', id:c0.id, face, u:.5, v:.5},
      pretension_kg: state.truck.strap_defaults?.pretension_kg ?? 200,
      capacity_kg: state.truck.strap_defaults?.capacity_kg ?? 1000,
      locked: false, review_signature: '',
    };
    state.lashings.push(lash);
    selectedLashId = lash.id;
    mutate(state); switchToLashingTab();
  };
  $('#lockAllBtn').onclick = () => lockLashings(null, true);
  $('#unlockAllBtn').onclick = () => lockLashings(null, false);

  $('#lashList').addEventListener('click', e => {
    const item = e.target.closest('[data-lashrow]');
    if (item) { selectedLashId = item.dataset.lashrow; selectedId = null; renderLashList(); renderLashEditor(); renderViews(); }
  });
  $('#legList').addEventListener('click', applySuggestion);
  $('#firstFailure').addEventListener('click', applySuggestion);
  async function applySuggestion(e) {
    const btn = e.target.closest('[data-suggest]');
    if (!btn) return;
    const s = JSON.parse(btn.dataset.suggest);
    if (s.type !== 'add_lashing') return;
    const a = anchorById(s.anchor_id), c = caseById(s.case_id);
    if (!a || !c || !placement(s.case_id)) return toast('建议所需锚点或箱体已不存在', 'warn');
    const lash = {
      id: 'L' + Date.now().toString(36).toUpperCase(),
      label: `增绑 ${c.label}`,
      from: {kind:'anchor', id:a.id, face:'', u:.5, v:.5},
      to: {kind:'case', id:c.id, face:s.face, u:s.u ?? .5, v:s.v ?? .5},
      pretension_kg: s.pretension_kg ?? 200, capacity_kg: a.capacity_kg,
      locked: false, review_signature: '',
    };
    state.lashings.push(lash);
    selectedLashId = lash.id;
    mutate(state);
    toast('已按建议加入草稿绑带，请检查路径后锁定', '');
  }

  $('#anchorEditor').addEventListener('input', e => {
    const i = e.target.dataset.anchor;
    if (i === undefined) return;
    const a = state.truck.anchors[+i];
    const dir = e.target.dataset.dir;
    if (dir) {
      const set = new Set(a.directions || []);
      e.target.checked ? set.add(dir) : set.delete(dir);
      a.directions = [...set];
    } else {
      const k = e.target.dataset.k;
      if (k === 'label') a.label = e.target.value;
      else if (k === 'surface') a.surface = e.target.value;
      else if (k === 'group') a.group = e.target.value;
      else if (['x','y','z','cap','gcap'].includes(k)) a[k === 'cap' ? 'capacity_kg' : k === 'gcap' ? 'group_capacity_kg' : k] = +e.target.value || 0;
    }
    scheduleAnalyze();
  });
  $('#anchorEditor').addEventListener('change', e => {
    // commit structural edits (positions re-render) after input handled
    if (e.target.dataset.anchor !== undefined) { renderViews(); renderLashingUI(); }
  });
  $('#anchorEditor').addEventListener('click', e => {
    const i = e.target.dataset.removeAnchor;
    if (i !== undefined) {
      const removed = state.truck.anchors.splice(+i, 1)[0];
      if (selectedAnchorId === removed.id) selectedAnchorId = null;
      mutate(state);
    }
  });
  $('#addAnchorBtn').onclick = () => {
    const n = state.truck.anchors.length + 1;
    state.truck.anchors.push({
      id: 'A' + Date.now().toString(36).toUpperCase().slice(-6),
      label: `锚点 ${n}`, surface: 'floor',
      x: .3, y: +(state.truck.width/2).toFixed(2), z: 0,
      capacity_kg: 1000, group: '', group_capacity_kg: 0,
      directions: ['+x','-x','+y','-y','+z'],
    });
    mutate(state);
  };
  $('#anchorRowsBtn').onclick = () => {
    const t = state.truck;
    const xs = [0.25, t.length/4, t.length/2, 3*t.length/4, t.length-0.25];
    xs.forEach((x, i) => [0.12, t.width-0.12].forEach((y, j) => {
      state.truck.anchors.push({
        id: `R${i+1}${j?'R':'L'}`, label: `地板排 ${i+1} ${j?'右':'左'}`,
        surface: 'floor', x: +x.toFixed(2), y, z: 0,
        capacity_kg: 1000, group: '', group_capacity_kg: 0,
        directions: ['+x','-x','+y','-y','+z'],
      });
    }));
    mutate(state);
  };
  ['#accelFwd','#accelRear','#accelLat','#accelUp'].forEach((sel, idx) => {
    const keys = ['forward','rearward','lateral','up'];
    $(sel).addEventListener('change', () => {
      state.truck.accel[keys[idx]] = Math.max(0, +$(sel).value);
      mutate(state);
    });
  });

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
