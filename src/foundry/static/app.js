'use strict';
const $ = (s) => document.querySelector(s);
const h = (s) =>
  String(s ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const label = (s) =>
  String(s || '')
    .replaceAll('_', ' ')
    .replace(/^./, (c) => c.toUpperCase());
const badge = (s) => `<span class="badge ${h(s)}">${h(label(s))}</span>`;
const state = {
  actor: null,
  businesses: [],
  bid: localStorage.getItem('foundry.business'),
  snapshot: null,
  view: 'overview',
  scenario: 'base',
};
const sections = [
  ['overview', '◈', 'Overview'],
  ['work', '☷', 'Work & decisions'],
  ['records', '▤', 'Business records'],
  ['evidence', '◇', 'Evidence'],
  ['finances', '↗', 'Financial model'],
  ['deals', '⊞', 'Acquisitions'],
  ['documents', '▧', 'Documents'],
  ['sales', '⇄', 'Sales to cash'],
  ['activity', '◷', 'History'],
  ['settings', '⚙', 'Access & AI'],
];
const kinds = [
  'customer',
  'offer',
  'asset',
  'contract',
  'obligation',
  'role',
  'entity',
  'person',
  'ownership',
  'decision',
  'risk',
];
const templates = {
  business_plan: 'Business plan',
  structure_options: 'Structure decision brief',
  contract_brief: 'Customer contract brief',
  operating_manual: 'Operating manual',
  readiness_report: 'Readiness report',
  investment_memo: 'Investment memo',
};
const canReview = () => ['owner', 'reviewer'].includes(state.actor?.role);
const canWrite = () => state.actor?.role !== 'viewer';
const objs = (kind) => (state.snapshot?.objects || []).filter((o) => !kind || o.kind === kind);
const find = (id) => objs().find((o) => o.id === id);
const currency = () => state.snapshot?.business.data.currency || 'USD';
const money = (v) =>
  new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: currency(),
    maximumFractionDigits: 0,
  }).format(Number(v || 0));
const shortMoney = (v) =>
  new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(
    Number(v),
  );
const dateTime = (v) =>
  new Date(v).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
const action = (a, t, id = '', style = 'secondary compact') =>
  `<button type="button" class="${style}" data-action="${a}" ${id ? `data-id="${h(id)}"` : ''}>${h(t)}</button>`;
const empty = (title, text, a = '', button = '') =>
  `<div class="empty"><h2>${h(title)}</h2><p>${h(text)}</p>${a && canWrite() ? action(a, button, '', 'primary') : ''}</div>`;
const heading = (title, text, button = '') =>
  `<div class="page-heading"><div><p class="eyebrow">${h(state.snapshot?.business.data.name || 'BUSINESS FOUNDRY')}</p><h1>${h(title)}</h1><p>${h(text)}</p></div>${button}</div>`;
const input = (name, title, value = '', type = 'text', hint = '', attrs = '') =>
  `<label>${h(title)}<input aria-label="${h(title)}" name="${h(name)}" type="${type}" value="${h(value)}" ${attrs}>${hint ? `<span class="hint">${h(hint)}</span>` : ''}</label>`;
const area = (name, title, value = '', hint = '') =>
  `<label class="full">${h(title)}<textarea aria-label="${h(title)}" name="${h(name)}" required>${h(value)}</textarea>${hint ? `<span class="hint">${h(hint)}</span>` : ''}</label>`;
const select = (name, title, choices, current = '', hint = '', multi = false) =>
  `<label>${h(title)}<select aria-label="${h(title)}" name="${h(name)}" ${multi ? 'multiple' : ''}>${choices
    .map((c) => {
      const [v, t] = Array.isArray(c) ? c : [c, label(c)];
      return `<option value="${h(v)}" ${Array.isArray(current) ? (current.includes(v) ? 'selected' : '') : current === v ? 'selected' : ''}>${h(t)}</option>`;
    })
    .join('')}</select>${hint ? `<span class="hint">${h(hint)}</span>` : ''}</label>`;
const evidenceChoices = (onlyReviewed = false) =>
  objs('evidence')
    .filter((o) => !onlyReviewed || o.state === 'verified')
    .map((o) => [o.id, `${o.title} · ${label(o.state)}`]);
const evidenceSelect = (
  name = 'evidence_ids',
  title = 'Supporting evidence',
  current = [],
  reviewed = false,
  multi = true,
) =>
  select(
    name,
    title,
    (multi ? [] : [['', 'Choose evidence…']]).concat(evidenceChoices(reviewed)),
    current,
    multi ? 'Select one or more. On a computer, use Ctrl or Command to select several.' : '',
    multi,
  );
let toastTimer;
function toast(text) {
  $('#toast').textContent = text;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($('#toast').hidden = true), 5500);
}
async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  if (options.method && !['GET', 'HEAD'].includes(options.method))
    headers['Idempotency-Key'] ||= crypto.randomUUID();
  let response;
  try {
    response = await fetch('/api' + path, { ...options, headers, credentials: 'same-origin' });
  } catch {
    throw new Error(
      'Connection interrupted. Retry the unchanged form to check the same request safely.',
    );
  }
  const data = await response.json().catch(() => ({ detail: 'Unexpected server response.' }));
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') showLogin();
    throw new Error(data.detail || 'The request could not be completed.');
  }
  return data;
}
function showLogin() {
  $('#app').hidden = true;
  $('#login').hidden = false;
}
function showDialog(title, html) {
  $('#dialog-title').textContent = title;
  $('#dialog-content').innerHTML = html;
  $('#dialog').showModal();
}
function formDialog(title, fields, submit, button = 'Save', intro = '') {
  showDialog(
    title,
    `${intro ? `<p>${h(intro)}</p>` : ''}<form id="active-form"><div class="form-grid">${fields}</div><p class="error spaced" role="alert"></p><div class="form-actions">${action('close-dialog', 'Cancel')}<button class="primary" type="submit">${h(button)}</button></div></form>`,
  );
  const form = $('#active-form');
  const command = crypto.randomUUID();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    $('#toast').hidden = true;
    const b = form.querySelector('[type=submit]');
    b.disabled = true;
    form.querySelector('.error').textContent = '';
    try {
      await submit(new FormData(form), command);
      $('#dialog').close();
      await refresh();
      toast('Saved to the workspace.');
    } catch (error) {
      form.querySelector('.error').textContent = error.message;
    } finally {
      b.disabled = false;
    }
  });
}
const post = (suffix, body, key) =>
  api(`/businesses/${state.bid}${suffix}`, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': key },
  });
function mountNav() {
  $('#nav').innerHTML = sections
    .map(
      ([v, icon, t]) =>
        `<button class="nav-item ${state.view === v ? 'active' : ''}" data-action="navigate" data-id="${v}"><span aria-hidden="true">${icon}</span>${t}</button>`,
    )
    .join('');
  $('#breadcrumb').textContent = 'Workspace / ' + sections.find((s) => s[0] === state.view)[2];
}
async function refresh() {
  state.businesses = await api('/businesses');
  if (!state.businesses.some((b) => b.id === state.bid))
    state.bid = state.businesses[0]?.id || null;
  if (state.bid) localStorage.setItem('foundry.business', state.bid);
  $('#business-select').innerHTML =
    state.businesses
      .map(
        (b) =>
          `<option value="${h(b.id)}" ${b.id === state.bid ? 'selected' : ''}>${h(b.data.name)}</option>`,
      )
      .join('') || '<option>No businesses yet</option>';
  state.snapshot = state.bid ? await api('/businesses/' + state.bid) : null;
  $('#demo-banner').hidden = !state.snapshot?.business.data.is_demo;
  $('#actor-name').textContent = state.actor.name + ' · ' + label(state.actor.role);
  $('[data-action="new-business"]').hidden = state.actor.role !== 'owner';
  await render();
}
async function render() {
  mountNav();
  if (!state.snapshot) {
    $('#content').innerHTML =
      heading(
        'Give your idea a foundation.',
        'Start with a concept. Build the records, assumptions and evidence as the business takes shape.',
      ) +
      empty(
        'Your first workspace',
        'Create a business to get its operating plan, work and decision trail started.',
        state.actor.role === 'owner' ? 'new-business' : '',
        'Create a business',
      );
    return;
  }
  const pages = {
    overview,
    work,
    records,
    evidence,
    finances,
    deals,
    documents,
    sales,
    activity,
    settings,
  };
  $('#content').innerHTML = await pages[state.view]();
}
function overview() {
  const s = state.snapshot.stats,
    b = state.snapshot.business.data;
  const tasks = objs('task')
    .filter((t) => !['verified', 'not_applicable'].includes(t.state))
    .sort((a, b) => a.blockers.length - b.blockers.length)
    .slice(0, 4);
  const plan = objs('financial_plan').at(-1);
  return (
    heading(
      'Make the next decision count.',
      'One place for the work, evidence and economics behind your business.',
      action('navigate', 'View the work', 'work', 'primary'),
    ) +
    `<div class="stats"><div class="stat"><span class="label">Work verified</span><strong>${s.verified}<span class="muted"> / ${s.tasks}</span></strong><p>${s.not_applicable} reviewed as not applicable</p></div><div class="stat"><span class="label">Cash collected</span><strong>${money(s.cash_collected)}</strong><p>Recorded against reviewed receipts</p></div><div class="stat"><span class="label">Outstanding invoices</span><strong>${money(s.outstanding_invoices)}</strong><p>Issued invoices awaiting payment</p></div><div class="stat"><span class="label">Needs attention</span><strong>${s.needs_review + s.overdue_obligations}</strong><p>${s.needs_review} reopened steps · ${s.overdue_obligations} past-due obligations</p></div></div>` +
    `<div class="grid two"><div class="card hero-card"><div><p class="eyebrow">${b.route === 'acquire' ? 'BUILD ON WHAT EXISTS' : 'BUILD FROM THE GROUND UP'}</p><h2>${h(b.name)}</h2><p>${h(b.concept)}</p></div><div class="actions"><span class="inline-tag">${h(b.jurisdiction === 'undecided' ? 'Jurisdiction to review' : b.jurisdiction)}</span><span class="inline-tag">${h(currency())}</span>${canWrite() ? action('edit-business', 'Edit business') : ''}</div></div><div class="card"><div class="card-heading"><h3>Your next useful steps</h3><span class="small">${tasks.length} to explore</span></div>${tasks.map((t, i) => `<div class="row"><span class="number">0${i + 1}</span><div class="row-main"><h3>${h(t.title)}</h3><p>${h(t.blockers[0] || 'Ready to prepare and review')}</p></div>${action('task', 'Open', t.id)}</div>`).join('') || '<p class="muted">All recorded steps are resolved. Keep recurring obligations and changed assumptions under review.</p>'}</div></div>` +
    `<div class="section-title"><h2>Understand the economics</h2>${action('navigate', 'Open financial model', 'finances')}</div>` +
    (plan
      ? `<div class="card">${chart(plan.data.scenarios)}<div class="legend"><span><i class="line-base"></i>Base</span><span><i class="line-downside"></i>Downside</span><span><i class="line-upside"></i>Upside</span></div><p class="small">Projected cash over ${plan.data.inputs.months} months. Forecast assumptions are separate from recorded payments.</p></div>`
      : empty(
          'Make the assumptions visible',
          'Model customers, pricing, delivery costs and collection timing before committing capital.',
          'new-forecast',
          'Build a financial model',
        ))
  );
}
function chart(scenarios) {
  const colors = { base: '#28674f', downside: '#bd9150', upside: '#84a698' };
  const all = Object.values(scenarios).flatMap((s) => s.months.map((m) => Number(m.cash)));
  const low = Math.min(0, ...all),
    high = Math.max(1, ...all),
    range = high - low,
    width = window.matchMedia('(max-width:760px)').matches ? 400 : 860,
    height = 210,
    left = 55,
    right = 20,
    top = 15,
    bottom = 28;
  const y = (v) => top + ((high - v) / range) * (height - top - bottom);
  const n = scenarios.base.months.length;
  let svg = `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Projected cash balance for base, downside and upside scenarios">`;
  for (let i = 0; i < 4; i++) {
    const v = low + (range * i) / 3;
    svg += `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${y(v)}" y2="${y(v)}"/><text x="${left - 10}" y="${y(v) + 3}" text-anchor="end">${h(shortMoney(v))}</text>`;
  }
  for (const [name, s] of Object.entries(scenarios)) {
    const points = s.months
      .map(
        (m, i) =>
          `${left + (i / Math.max(n - 1, 1)) * (width - left - right)},${y(Number(m.cash))}`,
      )
      .join(' ');
    svg += `<polyline points="${points}" fill="none" stroke="${colors[name]}" stroke-width="${name === 'base' ? 3 : 1.8}" ${name === 'downside' ? 'stroke-dasharray="5 4"' : ''}/>`;
  }
  svg += `<text x="${left}" y="${height - 5}">Month 1</text><text x="${width - right}" y="${height - 5}" text-anchor="end">Month ${n}</text></svg>`;
  return svg;
}
function work() {
  const tasks = objs('task'),
    groups = [...new Set(tasks.map((t) => t.data.category))];
  return (
    heading(
      'The work behind the business.',
      'Prepare, approve, submit and verify. Each step retains its dependencies, evidence and human decisions.',
    ) +
    `<div class="notice">Verification records a reviewer’s assessment of attached evidence. It is not an automatic legal or commercial certification.</div>` +
    groups
      .map(
        (g) =>
          `<section class="task-group"><h2>${h(g)}</h2>${tasks
            .filter((t) => t.data.category === g)
            .map(
              (t) =>
                `<div class="task-row"><div><h3>${h(t.title)}</h3><p>${h(t.data.deal_id ? find(t.data.deal_id)?.title : 'Core business')} ${t.data.external ? '· External action' : ''}</p></div><p>${h(t.blockers.length ? `${t.blockers.length} prerequisites · ${t.blockers[0]}` : 'Prerequisites recorded')}</p>${badge(t.state)}${action('task', 'Review', t.id)}</div>`,
            )
            .join('')}</section>`,
      )
      .join('')
  );
}
function records() {
  const data = objs().filter((o) => kinds.includes(o.kind));
  return (
    heading(
      'A shared picture of the business.',
      'Keep what you know, what you assume and who owns it in connected records.',
      canWrite() ? action('new-record', '+ Add record', '', 'primary') : '',
    ) +
    (data.length
      ? `<div class="record-grid">${data.map((o) => `<article class="card record-card"><div class="card-heading"><span class="eyebrow">${h(label(o.kind))}</span>${badge(o.state)}</div><h3>${h(o.title)}</h3><p>${h(o.data.details)}</p><div class="row"><span class="small">${h(o.data.owner)}${o.data.due_date ? ' · Due ' + h(o.data.due_date) : ''}<br>${o.data.evidence_ids.length} sources · Version ${o.version}</span>${action('record', canWrite() ? 'Edit' : 'View', o.id)}</div></article>`).join('')}</div>`
      : empty(
          'Start with the customer and offer',
          'Build the minimum useful picture: a buyer, a problem, an offer, the rights to deliver it, and someone responsible.',
          'new-record',
          'Add your first record',
        ))
  );
}
function evidence() {
  return (
    heading(
      'What supports the decision?',
      'Retain the original text, its source and a human review. Evidence content is immutable; corrections become new records.',
      canWrite() ? action('new-evidence', '+ Add evidence', '', 'primary') : '',
    ) +
    (objs('evidence').length
      ? `<div class="card">${objs('evidence')
          .map(
            (o) =>
              `<div class="row"><div class="row-main"><h3>${h(o.title)}</h3><p>${h(label(o.data.source_type))} · ${o.data.review ? 'Reviewed by ' + h(o.data.review.actor_name) : 'Awaiting human review'} · ${h(dateTime(o.created_at))}</p></div>${badge(o.state)}${action('evidence', 'Open', o.id)}</div>`,
          )
          .join('')}</div>`
      : empty(
          'Give your work a source',
          'Add interview notes, original document text, provider receipts or observations. Review evidence before using it to verify consequential work.',
          'new-evidence',
          'Add evidence',
        ))
  );
}
const statementFields = {
  income: [
    ['revenue', 'Revenue'],
    ['cost_of_sales', 'Cost of sales'],
    ['fixed_costs', 'Fixed costs'],
    ['ebitda', 'EBITDA'],
    ['depreciation', 'Depreciation'],
    ['interest', 'Interest'],
    ['tax', 'Tax'],
    ['net_income', 'Net income'],
  ],
  cash: [
    ['receipts', 'Receipts'],
    ['operating_cash_flow', 'Operating cash flow'],
    ['capex', 'Capital spending'],
    ['debt_principal', 'Debt principal'],
    ['net_cash_flow', 'Net cash flow'],
    ['cash', 'Closing cash'],
  ],
  balance: [
    ['cash', 'Cash'],
    ['receivables', 'Receivables'],
    ['net_fixed_assets', 'Fixed assets, net'],
    ['total_assets', 'Total assets'],
    ['debt', 'Debt'],
    ['equity', 'Equity'],
    ['balance_check', 'Balance check'],
  ],
};
function finances() {
  const plans = objs('financial_plan'),
    p = plans.at(-1);
  let html = heading(
    'Follow the cash, not just the idea.',
    'Linked statements and explicit scenarios turn assumptions into a plan you can challenge.',
    canWrite() ? action('new-forecast', p ? '+ New version' : '+ Build model', '', 'primary') : '',
  );
  if (!p)
    return (
      html +
      empty(
        'Model the first operating cycle',
        'Start with units, price, delivery cost, fixed costs and when customers actually pay. The model calculates all three statements.',
        'new-forecast',
        'Build financial model',
      )
    );
  const s = p.data.scenarios[state.scenario],
    sum = s.summary;
  html += `<div class="notice ${sum.statements_balance ? 'good' : 'error'}">${sum.statements_balance ? 'All three statements balance.' : 'Statement balance check failed.'} ${h(p.title)} · ${h(currency())} · ${p.data.inputs.months} months · ${plans.length} saved version${plans.length > 1 ? 's' : ''}</div><div class="finance-summary"><div><span>Ending cash · ${h(state.scenario)}</span><strong>${money(sum.ending_cash)}</strong></div><div><span>Additional cash needed</span><strong>${money(sum.additional_cash_needed)}</strong></div><div><span>First cash shortfall</span><strong>${sum.first_shortfall_month ? 'Month ' + sum.first_shortfall_month : 'None modeled'}</strong></div></div><div class="card"><div class="card-heading"><h3>Cash balance over time</h3><div class="actions">${['base', 'downside', 'upside'].map((c) => action('scenario', label(c), c, c === state.scenario ? 'primary compact' : 'secondary compact')).join('')}</div></div>${chart(p.data.scenarios)}<p class="small">${h(p.data.scenario_definitions[state.scenario])}. Negative cash is an unfunded shortfall; the model does not silently borrow to cover it.</p></div>`;
  for (const [statement, fields] of Object.entries(statementFields))
    html += `<div class="section-title"><h2>${statement === 'income' ? 'Income statement' : statement === 'cash' ? 'Cash flow' : 'Balance sheet'} · ${h(label(state.scenario))}</h2></div><div class="card table-wrap"><table><thead><tr><th>Month</th>${fields.map((f) => `<th>${f[1]}</th>`).join('')}</tr></thead><tbody>${s.months.map((m) => `<tr><td>${m.month}</td>${fields.map((f) => `<td>${money(m[f[0]])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  html += `<div class="grid two spaced"><div class="card"><h3>Discounted cash flow</h3>${sum.dcf ? `<div class="row"><span>Enterprise value</span><strong>${money(sum.dcf.enterprise_value)}</strong></div><div class="row"><span>Equity value</span><strong>${money(sum.dcf.equity_value)}</strong></div><div class="row"><span>Value from terminal assumption</span><strong>${sum.dcf.terminal_share === null ? 'Not meaningful' : (Number(sum.dcf.terminal_share) * 100).toFixed(1) + '%'}</strong></div><p class="small spaced">${h(sum.dcf.basis)}. This is a scenario value, not a market price.</p>` : '<p class="muted">Requires at least 12 months and positive terminal cash flow.</p>'}</div><div class="card"><h3>What the model assumes</h3>${p.data.assumptions.map((a) => `<p class="small">${h(a)}</p>`).join('')}<details><summary>Input assumptions and prior versions</summary><pre>${h(JSON.stringify(p.data.inputs, null, 2))}</pre>${plans.map((x) => `<p class="small">${h(x.title)} · ${h(dateTime(x.created_at))} ${action('inspect', 'View', x.id)}</p>`).join('')}</details></div></div>`;
  return html;
}
function deals() {
  return (
    heading(
      'Buy with a clear investment thesis.',
      'Review earnings, price, financing and what must transfer into the operating business.',
      canWrite() ? action('new-deal', '+ Analyze a business', '', 'primary') : '',
    ) +
    (objs('deal').length
      ? objs('deal')
          .map((o) => {
            const a = o.data.analysis;
            return `<article class="card spaced"><div class="card-heading"><div><p class="eyebrow">ACQUISITION SCREENING</p><h2>${h(o.title)}</h2></div>${badge(o.stale ? 'needs_review' : o.state)}</div><p class="muted">${h(o.data.inputs.thesis)}</p>${o.stale ? '<div class="notice">Supporting evidence changed. Create a new analysis before relying on these adjustments.</div>' : ''}<div class="finance-summary"><div><span>Normalized EBITDA</span><strong>${money(a.normalized_ebitda)}</strong></div><div><span>Assumed enterprise value range</span><strong>${a.enterprise_value_range ? a.enterprise_value_range.map(money).join(' – ') : 'Not meaningful'}</strong></div><div><span>Buyer equity required</span><strong>${money(a.sources_and_uses.buyer_equity_required)}</strong></div></div><div class="table-wrap"><table><thead><tr><th>Case</th><th>EBITDA</th><th>Cash after debt service</th><th>Coverage ratio</th></tr></thead><tbody>${Object.entries(
              a.scenarios,
            )
              .map(
                ([k, v]) =>
                  `<tr><td>${h(label(k))}</td><td>${money(v.ebitda)}</td><td>${money(v.cash_after_debt_service)}</td><td>${v.coverage_ratio === null ? 'No debt service' : Number(v.coverage_ratio).toFixed(2) + '×'}</td></tr>`,
              )
              .join(
                '',
              )}</tbody></table></div><p class="small spaced">${a.accepted_adjustments.length} reviewed earnings adjustments · ${a.excluded_adjustments.length} unsupported adjustments excluded · Annual debt service ${money(a.annual_debt_service)}</p><details><summary>Sources, uses, adjustments and limitations</summary><pre>${h(JSON.stringify(a, null, 2))}</pre></details><div class="actions">${action('navigate', 'Review diligence work', 'work')}${canWrite() ? action('investment-memo', 'Create investment memo', o.id) : ''}${canWrite() ? action('new-deal', 'Revise assumptions', o.id) : ''}</div></article>`;
          })
          .join('')
      : empty(
          'Compare buying with building',
          'Screen a target with explicit assumptions. Unsupported add-backs are excluded; reviewed evidence is still a human assessment, not an audit.',
          'new-deal',
          'Analyze a target',
        ))
  );
}
function documents() {
  return (
    heading(
      'Useful documents, grounded in the work.',
      'Generate immutable drafts from current records. Changed inputs are flagged, and earlier versions remain available.',
      canWrite() ? action('new-document', '+ Generate document', '', 'primary') : '',
    ) +
    (objs('document').length
      ? `<div class="record-grid">${objs('document')
          .slice()
          .reverse()
          .map(
            (o) =>
              `<article class="card doc-card"><div class="card-heading"><span class="eyebrow">${h(templates[o.data.template])}</span>${badge(o.stale ? 'needs_review' : 'draft')}</div><h3>${h(o.title)}</h3><p class="small">${h(dateTime(o.created_at))} · ${o.stale ? 'Source records have changed. Generate a new version.' : 'Reflects the current source records.'}</p><div class="actions">${action('document', 'Read draft', o.id)}<a class="secondary compact" href="/api/businesses/${h(state.bid)}/documents/${h(o.id)}/download">Download</a></div></article>`,
          )
          .join('')}</div>`
      : empty(
          'Turn decisions into reviewable drafts',
          'Create business plans, operating manuals, structure and contract briefs, readiness reports and investment memos. Unknowns stay visible.',
          'new-document',
          'Generate a document',
        ))
  );
}
function sales() {
  const invoices = objs('invoice'),
    payments = objs('payment');
  return (
    heading(
      'From an agreement to collected cash.',
      'Record delivery acceptance, externally issued invoices and reconciled receipts. Creating a draft does not send an invoice.',
      canWrite() ? action('new-invoice', '+ Draft invoice', '', 'primary') : '',
    ) +
    `<div class="finance-summary"><div><span>Issued and outstanding</span><strong>${money(state.snapshot.stats.outstanding_invoices)}</strong></div><div><span>Recorded collections</span><strong>${money(state.snapshot.stats.cash_collected)}</strong></div><div><span>Invoice records</span><strong>${invoices.length}</strong></div></div>` +
    (invoices.length
      ? `<div class="card">${invoices.map((o) => `<div class="row"><div class="row-main"><h3>${h(o.title)}</h3><p>${h(find(o.data.customer_id)?.title)} · Due ${h(o.data.due_date)}${o.evidence_warning ? ' · Supporting evidence needs review' : ''}</p><p>${money(o.data.amount)} total · ${money(o.data.balance)} outstanding</p></div>${badge(o.state)}<div class="actions">${canReview() && o.state === 'draft' ? action('issue-invoice', 'Record issue', o.id) : ''}${canReview() && ['issued', 'partially_paid'].includes(o.state) ? action('new-payment', 'Record payment', o.id) : ''}${action('inspect', 'Details', o.id)}</div></div>`).join('')}</div>`
      : empty(
          'Start with accepted work',
          'Attach reviewed evidence of the agreement and delivery acceptance, then record issue and payment.',
          'new-invoice',
          'Draft an invoice',
        )) +
    `<div class="section-title"><h2>Payment receipts</h2></div>` +
    (payments.length
      ? `<div class="card">${payments.map((o) => `<div class="row"><div><h3>${h(o.title)}</h3><p class="small">${h(o.data.received_on)} · ${h(find(o.data.invoice_id)?.title)}${o.evidence_warning ? ' · Evidence withdrawn; reconcile this entry' : ''}</p></div><strong>${money(o.data.amount)}</strong>${action('inspect', 'Details', o.id)}</div>`).join('')}</div>`
      : '<p class="muted">No reconciled receipts yet.</p>')
  );
}
async function activity() {
  const a = await api(`/businesses/${state.bid}/audit`);
  return (
    heading(
      'A history you can inspect.',
      'Every business change keeps its actor, sequence and previous version.',
      `<a class="secondary" href="/api/businesses/${h(state.bid)}/export">Export business records</a>`,
    ) +
    `<div class="notice ${a.integrity.valid ? 'good' : 'error'}">${a.integrity.valid ? 'History and current records agree.' : 'Integrity check failed. Investigate the record errors before relying on this workspace.'} ${a.integrity.events_checked} events checked.</div><p class="small">${h(a.integrity.limitation)}</p><div class="card">${a.events
      .slice()
      .reverse()
      .map(
        (e) =>
          `<div class="activity"><time>${h(dateTime(e.created_at))}</time><div><strong>${h(label(e.action.replaceAll('.', ' · ')))}</strong><p class="small">${h(e.payload.title || e.payload.data?.name || e.object_id)} · ${h(e.actor_id)}</p></div></div>`,
      )
      .join('')}</div>`
  );
}
async function settings() {
  let html =
    heading(
      'Keep authority explicit.',
      'Give people and agents access to the work they need. Keep business data private and decide when to share it.',
    ) +
    `<div class="grid two"><div class="card"><h3>Optional AI review</h3><p class="muted">Ask for a structured review of the concept and record summaries. Original evidence bodies and full financial tables are not sent. AI advice cannot approve work or execute external actions.</p>${state.actor.ai_configured ? (canWrite() ? action('ai-review', 'Request an AI review', '', 'primary') : '<p class="small">Read-only access.</p>') : '<div class="notice">AI is not configured. The workspace works without it. Configure a provider key and model on the server, or connect an AI client through the agent adapter.</div>'}<p class="small spaced">Provider usage is billed separately. Each request requires explicit sharing. Server limits and response records are retained.</p></div><div class="card"><h3>Your business records</h3><p class="muted">Download a portable snapshot containing records, evidence, documents and history. Keep exports private. For full disaster recovery, use the database backup command.</p><a class="secondary" href="/api/businesses/${h(state.bid)}/export">Export this business</a><details><summary>Backup and restore</summary><p class="small">Run <code>uv run foundry backup /private/path/backup.db</code>. Restore into a new database path using <code>uv run foundry --db /private/path/restored.db restore /private/path/backup.db</code>. Backups contain credential hashes and all businesses; protect them.</p></details></div></div>`;
  if (state.actor.role === 'owner') {
    const keys = await api('/principals');
    html += `<div class="section-title"><h2>People and access keys</h2>${action('new-key', '+ Create access key')}</div><div class="notice">Owner keys cover every business. Other roles are limited to explicitly selected workspaces. Give AI clients an agent key, never an owner or reviewer key.</div><div class="card">${keys.map((k) => `<div class="row"><div class="row-main"><h3>${h(k.name)}</h3><p>${h(label(k.role))} · ${k.role === 'owner' ? 'All businesses' : k.business_ids.length + ' business(es)'}${k.id === state.actor.id ? ' · You' : ''}</p></div>${badge(k.revoked ? 'rejected' : 'active')}${!k.revoked && k.id !== state.actor.id ? action('revoke-key', 'Revoke', k.id) : ''}</div>`).join('')}</div>`;
  }
  const runs = await api(`/businesses/${state.bid}/ai`);
  if (runs.length)
    html += `<div class="section-title"><h2>AI review history</h2></div><div class="card">${runs.map((r) => `<div class="row"><div class="row-main"><h3>${h(r.data.advice?.summary || r.data.error || 'Provider outcome not confirmed')}</h3><p>${h(dateTime(r.created_at))} · ${h(r.data.request.model)}</p><details><summary>Full review and provenance</summary><pre>${h(JSON.stringify(r.data, null, 2))}</pre></details></div>${badge(r.state)}</div>`).join('')}</div>`;
  return html;
}
function businessDialog(edit = false) {
  const b = edit ? state.snapshot.business.data : {};
  const fields =
    input('name', 'Business name', b.name || '', 'text', '', 'required maxlength="240"') +
    select(
      'route',
      'Entry path',
      [
        ['start', 'Start a business'],
        ['acquire', 'Acquire a business'],
      ],
      b.route || 'start',
    ) +
    area(
      'concept',
      'Concept',
      b.concept || '',
      'Describe the buyer, problem, proposed offer and why it could work.',
    ) +
    input('customer', 'Intended customer', b.customer || '') +
    input('owner', 'Accountable owner', b.owner || 'Founder') +
    select(
      'jurisdiction',
      'Planning jurisdiction',
      [
        ['undecided', 'Undecided — review needed'],
        ['US-DE', 'US / Delaware planning guide'],
        ['GB', 'United Kingdom planning guide'],
        ['SE', 'Sweden planning guide'],
        ['other', 'Other — local review required'],
      ],
      b.jurisdiction || 'undecided',
      'This identifies a planning guide, not an entity or tax recommendation.',
    ) +
    input(
      'currency',
      'Model currency',
      b.currency || 'USD',
      'text',
      'Three-letter currency code; financial records use one currency.',
      'pattern="[A-Z]{3}" required',
    ) +
    `<label class="check full"><input name="is_demo" type="checkbox" ${b.is_demo ? 'checked' : ''}>This workspace contains synthetic demonstration data.</label>`;
  formDialog(
    edit ? 'Edit business' : 'Create a business',
    fields,
    async (f, key) => {
      const body = Object.fromEntries(f);
      body.is_demo = f.has('is_demo');
      if (edit) {
        body.expected_version = state.snapshot.business.version;
        await api('/businesses/' + state.bid, {
          method: 'PUT',
          body,
          headers: { 'Idempotency-Key': key },
        });
      } else {
        const result = await api('/businesses', {
          method: 'POST',
          body,
          headers: { 'Idempotency-Key': key },
        });
        state.bid = result.id;
        state.view = 'overview';
      }
    },
    edit ? 'Save changes' : 'Create workspace',
    edit
      ? 'Changes reopen completed work for review. Jurisdiction is fixed in this release; use a new workspace for a reviewed jurisdiction change.'
      : 'A concept creates the operating workspace and dependency-aware work plan. Unknowns can stay unknown until you have evidence.',
  );
}
function recordDialog(id) {
  const o = id ? find(id) : null,
    d = o?.data || {};
  if (!canWrite()) {
    showDialog(o.title, `<pre>${h(JSON.stringify(o, null, 2))}</pre>`);
    return;
  }
  formDialog(
    o ? 'Edit business record' : 'Add business record',
    select('kind', 'Record type', o ? [o.kind] : kinds, o?.kind || 'customer') +
      input('title', 'Title', o?.title || '', 'text', '', 'required') +
      area('details', 'Details', d.details || '') +
      select(
        'certainty',
        'Evidence status',
        canReview() ? ['assumption', 'reported', 'verified'] : ['assumption', 'reported'],
        d.certainty || 'assumption',
      ) +
      input('owner', 'Responsible person', d.owner || state.snapshot.business.data.owner) +
      input('due_date', 'Due date', d.due_date || '', 'date') +
      evidenceSelect('evidence_ids', 'Supporting evidence', d.evidence_ids || []) +
      select(
        'related_ids',
        'Related records',
        objs()
          .filter((x) => kinds.includes(x.kind) && x.id !== id)
          .map((x) => [x.id, x.title]),
        d.related_ids || [],
        'Optional links to the shared business records.',
        true,
      ),
    async (f, key) => {
      const body = Object.fromEntries(f);
      body.evidence_ids = f.getAll('evidence_ids');
      body.related_ids = f.getAll('related_ids');
      body.due_date = body.due_date || null;
      body.properties = d.properties || {};
      if (o) {
        body.expected_version = o.version;
        await api(`/businesses/${state.bid}/records/${id}`, {
          method: 'PUT',
          body,
          headers: { 'Idempotency-Key': key },
        });
      } else await post('/records', body, key);
    },
    'Save record',
  );
}
function newEvidence() {
  formDialog(
    'Add source evidence',
    input('title', 'Evidence title', '', 'text', '', 'required') +
      select(
        'source_type',
        'Source type',
        [
          ['document', 'Document text'],
          ['external_receipt', 'External receipt'],
          ['observation', 'Observation'],
          ['attestation', 'Human attestation'],
        ],
        'document',
      ) +
      area(
        'content',
        'Original source text',
        '',
        'Preserve source wording. Content is immutable after saving; add a new item to correct it.',
      ) +
      input(
        'source_uri',
        'Source URL',
        '',
        'url',
        'Optional reference. The server does not fetch it.',
      ) +
      input('effective_date', 'Effective date', '', 'date'),
    async (f, key) => {
      const body = Object.fromEntries(f);
      body.effective_date = body.effective_date || null;
      await post('/evidence', body, key);
    },
    'Save unverified evidence',
  );
}
function evidenceDialog(id) {
  const o = find(id);
  showDialog(
    o.title,
    `${badge(o.state)}<p class="small spaced">${h(label(o.data.source_type))} · Effective ${h(o.data.effective_date || 'date not supplied')}</p><pre>${h(o.data.content)}</pre>${o.data.source_uri ? `<p><a href="${h(o.data.source_uri)}" target="_blank" rel="noopener noreferrer">Open source reference ↗</a></p>` : ''}${o.data.review ? `<p class="small">Reviewed by ${h(o.data.review.actor_name)}: ${h(o.data.review.note)}</p>` : ''}<details><summary>Content fingerprint</summary><code>${h(o.data.content_sha256)}</code></details>${canReview() ? `<div class="actions">${action('review-evidence', 'Record human review', id, 'primary')}${o.state === 'verified' ? action('revoke-evidence', 'Withdraw verification', id, 'danger') : ''}</div>` : ''}`,
  );
}
function reviewEvidence(id, revoke = false) {
  const o = find(id);
  $('#dialog').close();
  formDialog(
    revoke ? 'Withdraw evidence verification' : 'Record a human review',
    area(
      'note',
      'Review note',
      '',
      'Describe what you checked, its relevance and any limitations.',
    ),
    async (f, key) =>
      post(
        `/evidence/${id}/${revoke ? 'revoke' : 'review'}`,
        { note: f.get('note'), expected_version: o.version },
        key,
      ),
    revoke ? 'Withdraw verification' : 'Mark evidence reviewed',
    revoke
      ? 'Related completed work will reopen. Original source text and previous decisions remain in history.'
      : 'This records your assessment of the source. It does not independently authenticate the document or certify its claims.',
  );
}
function taskDialog(id) {
  const o = find(id),
    allowed = {
      pending: ['in_progress', 'prepared', 'not_applicable'],
      in_progress: ['prepared', 'not_applicable', 'needs_review'],
      prepared: ['approved', 'in_progress', 'needs_review', 'not_applicable'],
      approved: [o.data.external ? 'submitted' : 'verified', 'needs_review'],
      submitted: ['verified', 'needs_review'],
      verified: ['needs_review'],
      needs_review: ['in_progress', 'prepared', 'not_applicable'],
      not_applicable: ['needs_review'],
    };
  let choices = (allowed[o.state] || [])
    .filter((s) => canReview() || !['approved', 'verified', 'not_applicable'].includes(s))
    .filter((s) => state.actor.role !== 'agent' || s !== 'submitted');
  if (!canWrite() || !choices.length) {
    showDialog(
      o.title,
      `<p>${h(o.data.description)}</p>${badge(o.state)}<pre>${h(JSON.stringify(o.data.history, null, 2))}</pre>`,
    );
    return;
  }
  const intro = `${o.data.description}${o.blockers.length ? ' Prerequisites: ' + o.blockers.join('; ') + '.' : ''}`;
  formDialog(
    o.title,
    select('state', 'Next state', choices, choices.includes('prepared') ? 'prepared' : choices[0]) +
      evidenceSelect('evidence_ids', 'Evidence for this decision', o.data.evidence_ids) +
      area(
        'note',
        'Decision note',
        '',
        'Explain what was done, reviewed or found not applicable.',
      ) +
      `<details class="full"><summary>Prior decisions and source guidance</summary><pre>${h(JSON.stringify(o.data.history, null, 2))}</pre>${o.data.source_uri ? `<a href="${h(o.data.source_uri)}" target="_blank" rel="noopener noreferrer">Planning source ↗</a>` : ''}</details>`,
    async (f, key) =>
      post(
        `/tasks/${id}/transition`,
        {
          state: f.get('state'),
          note: f.get('note'),
          evidence_ids: f.getAll('evidence_ids'),
          expected_version: o.version,
        },
        key,
      ),
    'Record decision',
    intro,
  );
}
const financeFields = [
  ['months', 'Forecast months', 36],
  ['opening_equity', 'Opening equity', 50000],
  ['opening_debt', 'Opening debt', 0],
  ['monthly_units', 'First-month units', 5],
  ['unit_price', 'Price per unit', 2000],
  ['unit_cost', 'Cost per unit', 300],
  ['monthly_growth', 'Monthly unit growth', 0.03],
  ['fixed_costs', 'Monthly fixed costs', 6000],
  ['receivable_days', 'Collection delay (days)', 30],
  ['monthly_capex', 'Monthly capital spending', 0],
  ['asset_life_months', 'Asset life (months)', 36],
  ['tax_rate', 'Assumed tax rate', 0.25],
  ['annual_interest_rate', 'Annual debt interest', 0.08],
  ['monthly_principal', 'Monthly debt principal', 0],
  ['discount_rate', 'Annual discount rate', 0.2],
  ['terminal_growth', 'Terminal annual growth', 0.02],
  ['excess_cash', 'Excess cash for equity bridge', 0],
];
function forecastDialog() {
  const p = objs('financial_plan').at(-1)?.data.inputs || {};
  formDialog(
    'Build a financial model',
    input('title', 'Version name', 'Operating plan', 'text', '', 'required') +
      evidenceSelect() +
      financeFields
        .map(([k, t, v]) =>
          input(
            k,
            t,
            p[k] ?? v,
            'number',
            k.includes('rate') || k.includes('growth') ? 'Decimal rate: 0.20 means 20%.' : '',
            'step="any" required',
          ),
        )
        .join(''),
    async (f, key) => {
      const inputs = {};
      for (const [k] of financeFields)
        inputs[k] = ['months', 'receivable_days', 'asset_life_months'].includes(k)
          ? Number(f.get(k))
          : f.get(k);
      await post(
        '/forecasts',
        { title: f.get('title'), inputs, evidence_ids: f.getAll('evidence_ids') },
        key,
      );
    },
    'Calculate and save',
    'Save an immutable model version. The engine calculates base, downside and upside cases using the same linked statements. All figures use the workspace currency.',
  );
}
const dealFields = [
  ['annual_revenue', 'Annual revenue', 1000000],
  ['reported_ebitda', 'Reported EBITDA', 200000],
  ['replacement_management', 'Annual replacement management cost', 0],
  ['multiple_low', 'Low EBITDA multiple', 3],
  ['multiple_high', 'High EBITDA multiple', 5],
  ['purchase_price', 'Negotiated purchase consideration', 800000],
  ['fees', 'Transaction fees', 30000],
  ['working_capital', 'Working capital funding', 50000],
  ['integration_cost', 'Integration costs', 0],
  ['senior_debt', 'New senior debt', 400000],
  ['debt_rate', 'Senior annual interest rate', 0.09],
  ['debt_years', 'Senior term (years)', 7],
  ['seller_note', 'Seller financing', 0],
  ['seller_rate', 'Seller annual interest rate', 0.06],
  ['seller_years', 'Seller term (years)', 5],
  ['annual_capex', 'Annual maintenance capex', 15000],
  ['annual_working_capital_growth', 'Annual working capital increase', 0],
  ['tax_rate', 'Assumed tax rate', 0.25],
  ['cash_acquired', 'Cash in valuation equity bridge', 0],
  ['debt_assumed', 'Debt in valuation equity bridge', 0],
];
function dealDialog(id) {
  const d = id ? find(id).data.inputs : {};
  formDialog(
    'Analyze an acquisition',
    input('target', 'Target business', d.target || '', 'text', '', 'required') +
      evidenceSelect('evidence_ids', 'Source evidence', d.evidence_ids || []) +
      area('thesis', 'Investment thesis', d.thesis || '') +
      dealFields
        .map(([k, t, v]) =>
          input(
            k,
            t,
            d[k] ?? v,
            'number',
            k.includes('rate') ? 'Decimal rate: 0.09 means 9%.' : '',
            'step="any" required',
          ),
        )
        .join('') +
      `<details class="full"><summary>Earnings adjustments (${d.adjustments?.length || 0} retained from prior analysis)</summary><p class="small">Existing adjustments are retained. Add another here; only adjustments with reviewed evidence enter normalized earnings. Management replacement is deducted separately.</p>${d.adjustments?.length ? `<pre>${h(JSON.stringify(d.adjustments, null, 2))}</pre>` : ''}<div class="form-grid">${input('adjustment_label', 'New adjustment label')}${input('adjustment_amount', 'Adjustment amount', 0, 'number', 'Positive adds to EBITDA; negative deducts.', 'step="any"')}${evidenceSelect('adjustment_evidence', 'Adjustment evidence', '', false, false)}</div></details>`,
    async (f, key) => {
      const body = {
        target: f.get('target'),
        thesis: f.get('thesis'),
        evidence_ids: f.getAll('evidence_ids'),
        adjustments: d.adjustments || [],
      };
      for (const [k] of dealFields)
        body[k] = ['debt_years', 'seller_years'].includes(k) ? Number(f.get(k)) : f.get(k);
      if (f.get('adjustment_label'))
        body.adjustments = [
          ...body.adjustments,
          {
            label: f.get('adjustment_label'),
            amount: f.get('adjustment_amount'),
            evidence_id: f.get('adjustment_evidence') || null,
          },
        ];
      await post('/deals', body, key);
    },
    'Calculate and create diligence work',
    'Multiples and financing terms are your assumptions. Negotiated consideration is a transaction use; the cash/debt valuation bridge is not automatically additional funding or debt service.',
  );
}
function documentDialog(dealId = null) {
  formDialog(
    'Generate a working document',
    select(
      'template',
      'Document type',
      Object.entries(templates),
      dealId ? 'investment_memo' : 'business_plan',
    ) +
      select(
        'deal_id',
        'Acquisition analysis',
        [['', 'Not applicable'], ...objs('deal').map((o) => [o.id, o.title])],
        dealId || '',
      ),
    async (f, key) =>
      post('/documents', { template: f.get('template'), deal_id: f.get('deal_id') || null }, key),
    'Generate draft',
    'Documents retain the source version and explicit unknowns. Contract outputs are preparation briefs, not executable legal agreements.',
  );
}
function invoiceDialog() {
  formDialog(
    'Draft an invoice record',
    select('customer_id', 'Customer', [
      ['', 'Choose customer…'],
      ...objs('customer').map((o) => [o.id, o.title]),
    ]) +
      input('description', 'Description', '', 'text', '', 'required') +
      input(
        'amount',
        `Amount (${currency()})`,
        '',
        'number',
        '',
        'required min="0.01" step="0.01"',
      ) +
      input('due_date', 'Due date', '', 'date', '', 'required') +
      evidenceSelect('contract_evidence_id', 'Agreement evidence', '', false, false) +
      evidenceSelect('acceptance_evidence_id', 'Delivery acceptance evidence', '', false, false),
    async (f, key) => post('/invoices', Object.fromEntries(f), key),
    'Save draft',
    'A draft remains internal. A human reviewer must attach reviewed external receipts before recording actual issue.',
  );
}
function issueDialog(id) {
  const o = find(id);
  formDialog(
    'Record external invoice issue',
    evidenceSelect('evidence_id', 'Issue receipt', '', true, false) +
      area('note', 'Issue note', '', 'Record where and when the invoice was actually issued.'),
    async (f, key) =>
      post(`/invoices/${id}/issue`, { ...Object.fromEntries(f), expected_version: o.version }, key),
    'Record issue',
    'This records an action you already completed with your invoicing provider. It does not send anything. Agreement, acceptance and issue evidence must be reviewed external receipts.',
  );
}
function paymentDialog(id) {
  const o = find(id);
  formDialog(
    'Reconcile a payment',
    input(
      'amount',
      `Receipt amount (${currency()})`,
      o.data.balance,
      'number',
      '',
      'step="0.01" min="0.01" required',
    ) +
      input('reference', 'Unique bank or provider reference', '', 'text', '', 'required') +
      input(
        'received_on',
        'Received on',
        new Date().toISOString().slice(0, 10),
        'date',
        '',
        'required',
      ) +
      evidenceSelect('evidence_id', 'Reviewed payment receipt', '', true, false),
    async (f, key) => post(`/invoices/${id}/payments`, Object.fromEntries(f), key),
    'Record payment',
    'Record only money actually received. Partial payments are supported; duplicate references and overpayments are rejected.',
  );
}
function keyDialog() {
  formDialog(
    'Create scoped access',
    input('name', 'Person or agent name', '', 'text', '', 'required') +
      select(
        'role',
        'Role',
        [
          ['viewer', 'Viewer — read only'],
          ['operator', 'Operator — prepare work'],
          ['reviewer', 'Reviewer — human decisions'],
          ['agent', 'Agent — prepare, never approve'],
        ],
        'viewer',
      ) +
      select(
        'business_ids',
        'Business access',
        state.businesses.map((b) => [b.id, b.data.name]),
        [state.bid],
        'Select the workspaces this key can access.',
        true,
      ),
    async (f) => {
      const key = await api('/principals', {
        method: 'POST',
        body: { name: f.get('name'), role: f.get('role'), business_ids: f.getAll('business_ids') },
      });
      setTimeout(
        () =>
          showDialog(
            'Save this access key',
            `<p>The server stores only a hash. Copy this key into a password manager or your client’s secret settings; it will not be shown again.</p><pre>${h(key.token)}</pre><p class="small">${h(key.name)} · ${h(label(key.role))}. Never put the key in GitHub, a screenshot or a shared document.</p>`,
          ),
        200,
      );
    },
    'Create key',
  );
}
function aiDialog() {
  formDialog(
    'Request an AI review',
    area(
      'question',
      'What should the review examine?',
      'What are the most important unsupported assumptions and the next three useful actions for this business?',
    ) +
      `<label class="check full"><input name="share" type="checkbox" required>I agree to send this business concept, record titles, status, details and summary totals to the configured OpenAI API account. Evidence bodies and full financial tables are omitted.</label>`,
    async (f, key) => {
      const r = await post(
        '/ai',
        {
          question: f.get('question'),
          share_with_provider: f.has('share'),
          max_output_tokens: 2000,
        },
        key,
      );
      if (r.state !== 'succeeded') throw new Error(r.data.error || r.data.note);
    },
    'Share and request review',
    'The answer is advice only. It has no authority to change records or approve work. Provider usage may incur charges.',
  );
}
async function handle(a, id) {
  if (a === 'navigate') {
    state.view = id;
    await render();
    window.scrollTo(0, 0);
  } else if (a === 'scenario') {
    state.scenario = id;
    await render();
  } else if (a === 'close-dialog') $('#dialog').close();
  else if (a === 'logout') {
    await api('/logout', { method: 'POST' });
    state.actor = null;
    showLogin();
  } else if (a === 'new-business') businessDialog();
  else if (a === 'edit-business') businessDialog(true);
  else if (a === 'new-record') recordDialog();
  else if (a === 'record') recordDialog(id);
  else if (a === 'new-evidence') newEvidence();
  else if (a === 'evidence') evidenceDialog(id);
  else if (a === 'review-evidence') reviewEvidence(id);
  else if (a === 'revoke-evidence') reviewEvidence(id, true);
  else if (a === 'task') taskDialog(id);
  else if (a === 'new-forecast') forecastDialog();
  else if (a === 'new-deal') dealDialog(id);
  else if (a === 'new-document') documentDialog();
  else if (a === 'investment-memo') documentDialog(id);
  else if (a === 'document') {
    const o = find(id);
    showDialog(
      o.title,
      `${o.stale ? '<div class="notice">Inputs have changed since this draft was generated.</div>' : ''}<pre class="document">${h(o.data.content)}</pre><a class="secondary" href="/api/businesses/${h(state.bid)}/documents/${h(id)}/download">Download Markdown</a>`,
    );
  } else if (a === 'inspect') {
    const o = find(id);
    showDialog(o.title, `<pre>${h(JSON.stringify(o.data, null, 2))}</pre>`);
  } else if (a === 'new-invoice') invoiceDialog();
  else if (a === 'issue-invoice') issueDialog(id);
  else if (a === 'new-payment') paymentDialog(id);
  else if (a === 'new-key') keyDialog();
  else if (a === 'revoke-key') {
    formDialog(
      'Revoke access key',
      '<p class="full">This immediately prevents the selected key and its sessions from accessing this server.</p>',
      async () => api('/principals/' + id, { method: 'DELETE' }),
      'Revoke key',
    );
  } else if (a === 'ai-review') aiDialog();
}
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-action]');
  if (button) handle(button.dataset.action, button.dataset.id).catch((e) => toast(e.message));
});
$('#business-select').addEventListener('change', async (event) => {
  state.bid = event.target.value;
  try {
    await refresh();
  } catch (e) {
    toast(e.message);
  }
});
$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target,
    b = form.querySelector('button');
  b.disabled = true;
  $('#login-error').textContent = '';
  try {
    await api('/login', { method: 'POST', body: { token: new FormData(form).get('token') } });
    form.reset();
    await start();
  } catch (e) {
    $('#login-error').textContent = e.message;
  } finally {
    b.disabled = false;
  }
});
async function start() {
  try {
    state.actor = await api('/me');
    $('#login').hidden = true;
    $('#app').hidden = false;
    await refresh();
  } catch (e) {
    showLogin();
    if (e.message !== 'Sign in with a valid access key.') $('#login-error').textContent = e.message;
  }
}
start();
