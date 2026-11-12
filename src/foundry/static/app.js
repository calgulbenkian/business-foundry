'use strict';
/*
  Business Foundry — Studio interface.

  Three panes: sources, a grouped middle list, and an inspector of labelled rows. Each group of
  steps in the planning catalog (a task's category, and each acquisition analysis separately)
  is one coloured line. The line runs down the middle list through each step's state glyph, and
  the Journey map draws every line converging on one interchange.

  Everything shown comes from the server: task states, prerequisites, reopen notes, evidence
  reviews and withdrawals, staleness flags and every money figure. Every figure shown as text is
  the server's decimal string, formatted here and never recalculated. The one exception is
  drawing: the cash chart converts figures to place its points and label its axis, and says that
  it is approximate. This file never invents a state the records do not hold.

  A response is applied, to the page or to anything kept in `state`, only if the business and the
  load it was requested for are still the ones on screen (and, for something filled into the
  inspector, only while the same item is still shown). Older responses are dropped.

  Everything the page waits for (an answer, a promise chain, a dialog waiting to be shown) belongs
  to the session that started it, captured before the request is sent. Once that session has
  ended it changes nothing: no dialog, message, stored state or focus. A form belongs to the
  business on screen when it opened: it sends to that business, with the ids and versions it was
  filled from, and it is closed if the page changes to another business before it is sent. The
  answer to a command speaks for its own business: on another business's page it only says, by
  name, whether that business's change was saved.
*/

/* ---------------- basics ---------------- */
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
const capFirst = (s) => String(s || '').replace(/^./, (c) => c.toUpperCase());
const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
const saved = {
  get(k) {
    try {
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, v);
    } catch {
      /* storage may be unavailable; the interface works without it */
    }
  },
};

/* Every refresh, and every sign-out, starts a new load. A response that belongs to an older load
   is dropped: it was requested for a business or a state of the records no longer on screen. */
let loadSeq = 0;
/* Every sign-out starts a new session; a late 401 from an earlier session is not acted on. */
let sessionSeq = 0;
const state = {
  actor: null,
  businesses: [],
  // the business whose records are on screen; it changes only together with the snapshot
  bid: saved.get('foundry.business'),
  // a business asked for (by the selector or by creating one) whose records have not arrived yet
  wantBid: null,
  // the load whose records are on screen
  loaded: 0,
  snapshot: null,
  runs: [],
  principals: null,
  audit: null,
  auditPromise: null,
  source: 'today',
  selected: null,
  remembered: {},
  scenario: 'base',
  open: null,
  // a command the server accepted that no successful reload has shown yet
  behind: false,
  // step decisions accepted since the last successful reload: task id -> the state recorded
  accepted: {},
};

const canReview = () => ['owner', 'reviewer'].includes(state.actor?.role);
const canWrite = () => Boolean(state.actor) && state.actor.role !== 'viewer';
const isOwner = () => state.actor?.role === 'owner';
const objs = (kind) => (state.snapshot?.objects || []).filter((o) => !kind || o.kind === kind);
const find = (id) => objs().find((o) => o.id === id);
const currency = () => state.snapshot?.business.data.currency || 'USD';
const businessName = () => state.snapshot?.business.data.name || '';
/* The business on screen, as a form or a command remembers it: where it sends, and the name its
   result is reported under if the page has moved on by the time the answer arrives. */
const here = () => ({ bid: state.bid, name: businessName() });

/* Formatter output goes straight into innerHTML, so it must never carry markup. Intl output is
   digits, separators and currency signs; this makes that a guarantee rather than an assumption.
   An ASCII apostrophe (some locales group digits with one) becomes the typographic one. */
const noMarkup = (s) =>
  String(s)
    .replace(/[&<>"]/g, '')
    .replaceAll("'", '\u2019');
const decimalText = (v) =>
  new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(
    String(v),
  );
/* Money arrives from the server as exact decimal strings. It is formatted here, never
   recalculated: Intl.NumberFormat formats a decimal string without converting the sum. */
function money(v, cur = currency()) {
  if (v === null || v === undefined || v === '') return '—';
  try {
    return noMarkup(
      new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency: cur,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(String(v)),
    );
  } catch {
    // A currency code Intl rejects: the amount alone, and the code reduced to letters and digits.
    const code = String(cur ?? '').replace(/[^A-Za-z0-9]/g, '');
    return noMarkup(decimalText(v) + (code ? ' ' + code : ''));
  }
}
const ratio = (v, digits = 2) =>
  v === null || v === undefined
    ? '—'
    : noMarkup(
        new Intl.NumberFormat(undefined, {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        }).format(String(v)),
      );
const percent = (v) =>
  v === null || v === undefined
    ? 'Not meaningful'
    : noMarkup(
        new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 1 }).format(
          String(v),
        ),
      );
/* Axis labels for the cash chart only: approximate, from a converted number, never a figure. */
const shortMoney = (v) =>
  noMarkup(
    new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(v),
  );
/* The sign of a decimal string (-1, 0 or 1), read from its digits. It is never converted to a
   binary number, so no rounding can turn a small amount into zero or the reverse. Anything
   that is not a decimal string counts as 0. */
function decimalSign(v) {
  const m = /^\s*([+-]?)(\d*)(?:\.(\d*))?(?:[eE][+-]?\d+)?\s*$/.exec(String(v ?? ''));
  if (!m || !(m[2] || m[3])) return 0;
  if (!/[1-9]/.test(m[2] + (m[3] || ''))) return 0;
  return m[1] === '-' ? -1 : 1;
}
const dDay = (v) =>
  v ? new Date(v).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';
const dFull = (v) =>
  v
    ? new Date(v).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
    : '';
const dateTime = (v) =>
  new Date(v).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
/* A calendar date (YYYY-MM-DD) is shown as that day, not shifted by the viewer's time zone. */
function plainDate(v) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v || ''));
  if (!m) return v ? String(v) : '';
  return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  });
}
const CALC_NOTE =
  'Calculated by Business Foundry with exact decimal arithmetic. No assistant was involved.';

/* ---------------- the lines and steps, derived from the snapshot ---------------- */
const PALETTE = ['s', 'm', 'p', 'c', 'e', 'v', 'o', 'b', 'r', 't'];
const KNOWN_COLOUR = {
  Setup: 's',
  Finance: 'm',
  Rights: 'p',
  'Sales to cash': 'c',
  Market: 'e',
  Delivery: 'v',
  Operate: 'o',
  Acquisition: 'b',
};
const FINISHED = new Set(['verified', 'not_applicable']);
/* A step of a planning guide that no longer applies. When the planning jurisdiction changes, the
   server keeps the old guide's steps as history in this state, and nothing leads out of it. Such
   a step is on no line and in no list of steps, and nothing the page counts includes it. */
const replaced = (o) => o?.kind === 'task' && o.state === 'superseded';

function buildModel(snapshot) {
  // the server's counts in `stats` cover live steps only, and so does every count made from this
  const tasks = (snapshot?.objects || []).filter((o) => o.kind === 'task' && !replaced(o));
  const lines = [];
  const byKey = new Map();
  for (const t of tasks) {
    const category = String(t.data.category || 'Steps');
    const key = category + (t.data.deal_id ? '\u0000' + t.data.deal_id : '');
    if (!byKey.has(key)) {
      const line = { key, category, dealId: t.data.deal_id || null, steps: [] };
      byKey.set(key, line);
      lines.push(line);
    }
    byKey.get(key).steps.push(t);
  }
  const perCategory = {};
  for (const l of lines) perCategory[l.category] = (perCategory[l.category] || 0) + 1;
  const used = new Set();
  const codes = new Set();
  lines.forEach((l, i) => {
    l.id = 'ln' + i;
    const deal = l.dealId ? snapshot.objects.find((o) => o.id === l.dealId) : null;
    l.name = perCategory[l.category] > 1 && deal ? `${l.category} · ${deal.title}` : l.category;
    l.deal = deal;
    let k = KNOWN_COLOUR[l.category];
    if (!k || used.has(k)) k = PALETTE.find((x) => !used.has(x)) || PALETTE[i % PALETTE.length];
    used.add(k);
    l.k = k;
    l.code = lineCode(l.category, codes);
  });
  const width = String(tasks.length).length < 2 ? 2 : String(tasks.length).length;
  const steps = [];
  const byId = new Map();
  let n = 0;
  for (const l of lines) {
    l.steps = l.steps.map((t, j) => {
      n += 1;
      const s = { n: String(n).padStart(width, '0'), id: t.id, task: t, line: l, index: j };
      steps.push(s);
      byId.set(t.id, s);
      return s;
    });
  }
  const verified = tasks.filter((t) => t.state === 'verified').length;
  const na = tasks.filter((t) => t.state === 'not_applicable').length;
  return {
    lines,
    steps,
    byId,
    verified,
    na,
    toGo: tasks.length - verified - na,
  };
}
/* A two-letter bullet from the line's name, kept unique across the map. */
function lineCode(name, codes) {
  const letters = String(name).replace(/[^A-Za-z0-9]/g, '') || 'L';
  const first = letters[0].toUpperCase();
  const tries = [];
  for (const c of letters.slice(1)) tries.push(first + c.toLowerCase());
  for (let d = 2; d < 100; d++) tries.push(first + d);
  const code = tries.find((c) => !codes.has(c)) || first;
  codes.add(code);
  return code;
}
let M = buildModel(null);
const lineStats = (l) => ({
  total: l.steps.length,
  verified: l.steps.filter((s) => s.task.state === 'verified').length,
  review: l.steps.some((s) => s.task.state === 'needs_review'),
});

/* ---------------- states ---------------- */
/* Every task state the server records, each with its own glyph. */
const STATES = {
  pending: { g: 'none', label: 'Not started', key: 'nobody has begun it' },
  in_progress: { g: 'progress', label: 'In progress', key: 'someone is preparing it' },
  prepared: { g: 'prepared', label: 'Prepared', key: 'drafted, waiting for approval' },
  approved: { g: 'approved', label: 'Approved', key: 'a person reviewed it' },
  submitted: { g: 'submitted', label: 'Submitted', key: 'sent to an outside body' },
  verified: { g: 'verified', label: 'Verified', key: 'the result is on record' },
  not_applicable: { g: 'na', label: 'Not applicable', key: 'reviewed and ruled out' },
  needs_review: { g: 'review', label: 'Needs review', key: 'what it relied on changed' },
};
/* A replaced step is history, not one of the states above: it has no place on the map or in its
   key, and it is drawn with the neutral dash. */
const REPLACED = { g: 'none', label: 'Replaced', key: 'kept as history' };
const stateOf = (t) =>
  t.state === 'superseded'
    ? REPLACED
    : STATES[t.state] || { g: 'none', label: label(t.state), key: '' };
const ALLOWED = {
  pending: ['in_progress', 'prepared', 'not_applicable'],
  in_progress: ['prepared', 'not_applicable', 'needs_review'],
  prepared: ['approved', 'in_progress', 'needs_review', 'not_applicable'],
  approved: ['submitted', 'verified', 'needs_review'],
  submitted: ['verified', 'needs_review'],
  verified: ['needs_review'],
  needs_review: ['in_progress', 'prepared', 'not_applicable'],
  not_applicable: ['needs_review'],
};
/* The moves this person may record. The server enforces every rule again. */
function choices(t) {
  let list = (ALLOWED[t.state] || []).slice();
  if (t.state === 'approved')
    list = list.filter((s) => s !== (t.data.external ? 'verified' : 'submitted'));
  return list
    .filter((s) => canReview() || !['approved', 'verified', 'not_applicable'].includes(s))
    .filter((s) => state.actor?.role !== 'agent' || s !== 'submitted');
}
const FORWARD = {
  pending: 'prepared',
  in_progress: 'prepared',
  needs_review: 'prepared',
  prepared: 'approved',
  approved: 'submitted',
  submitted: 'verified',
};
function forward(t) {
  if (t.state === 'approved') return t.data.external ? 'submitted' : 'verified';
  return FORWARD[t.state] || null;
}
function moveLabel(to, t) {
  return {
    in_progress: 'Start work…',
    prepared: 'Mark prepared…',
    approved: 'Approve…',
    submitted: 'I’ve submitted it…',
    verified: t.data.external ? 'Add the receipt…' : 'Verify…',
    needs_review: 'Reopen for review…',
    not_applicable: 'Not applicable…',
  }[to];
}
function moveText(to, t) {
  return {
    in_progress: 'Record that work on this step has started. Nothing is sent anywhere.',
    prepared:
      'Record that this step is prepared and ready for a person to approve. Preparing does not approve, send or sign anything.',
    approved:
      'Approving records that you reviewed the prepared work and its evidence. It needs reviewed evidence and every prerequisite complete. Business Foundry will not send or sign anything.',
    submitted:
      'Record that you sent this to the outside body yourself. Do this only after you have sent it, and attach a reviewed external receipt. Business Foundry does not send anything.',
    verified: t.data.external
      ? 'Record the outside body’s result and attach their reviewed receipt. Verifying means you checked the recorded result; it is not a legal certification.'
      : 'Verifying records that you checked the result of this internal review. It needs reviewed evidence.',
    needs_review:
      'Reopen this step. Steps that depend on it reopen too, and every earlier decision stays in its history.',
    not_applicable:
      'Record a reviewed decision that this step does not apply here. Attach reviewed evidence for that decision.',
  }[to];
}
/* What the current person can do next, if anything: derived from the state and their role. */
function yourMove(t) {
  if (!canWrite()) return null;
  if (t.state === 'prepared') return canReview() ? 'approve it' : null;
  if (t.state === 'approved') {
    if (t.data.external) return state.actor.role === 'agent' ? null : 'submit it';
    return canReview() ? 'verify it' : null;
  }
  if (t.state === 'needs_review') return 'prepare again';
  if (t.state === 'in_progress') return 'finish preparing';
  return null;
}
const lastIndex = (list, fn) => {
  for (let i = list.length - 1; i >= 0; i--) if (fn(list[i])) return i;
  return -1;
};
const taskHistory = (t) => (Array.isArray(t.data.history) ? t.data.history : []);
function reopened(t) {
  const hist = taskHistory(t);
  const i = lastIndex(hist, (e) => e && e.to === 'needs_review');
  return i < 0 ? null : hist[i];
}
function shortState(s) {
  const t = s.task;
  const mv = yourMove(t);
  if (t.state === 'needs_review') return 'Needs review · ' + (mv || 'reopened');
  if (t.state === 'prepared') return 'Prepared · ' + (mv || 'to approve');
  if (t.state === 'approved')
    return 'Approved · ' + (mv || (t.data.external ? 'to submit' : 'to verify'));
  if (t.state === 'submitted') return 'Submitted · awaiting receipt';
  if (t.state === 'pending' && t.blockers?.length)
    return `Not started · waits on ${t.blockers.length}`;
  return stateOf(t).label;
}
function longState(s) {
  const t = s.task;
  const mv = yourMove(t);
  const parts = [stateOf(t).label.toLowerCase()];
  if (t.state === 'needs_review') parts.push(`reopened: ${reopened(t)?.note || 'inputs changed'}`);
  if (t.state === 'submitted') parts.push('waiting on the outside body’s receipt');
  if (t.state === 'pending' && t.blockers?.length)
    parts.push(`waiting on ${plural(t.blockers.length, 'prerequisite')}`);
  if (mv) parts.push(`your move: ${mv}`);
  return parts.join(', ');
}
function nextText(s) {
  const t = s.task;
  if (t.state === 'needs_review') {
    const r = reopened(t);
    return `reopened${r?.at ? ' ' + dDay(r.at) : ''}`;
  }
  if (t.state === 'prepared')
    return canReview() ? 'next, approve it' : 'waiting on an owner or reviewer to approve';
  if (t.state === 'approved')
    return t.data.external ? 'next, submit it yourself' : 'next, verify the result';
  if (t.state === 'submitted')
    return 'waiting on the outside body; add their receipt when it arrives';
  if (t.state === 'pending')
    return t.blockers?.length
      ? `waiting on ${plural(t.blockers.length, 'prerequisite')}`
      : 'ready to prepare';
  if (t.state === 'in_progress') return 'being prepared';
  if (t.state === 'verified')
    return t.data.external ? 'submitted and verified' : 'all stages recorded';
  if (t.state === 'not_applicable') return 'reviewed and ruled out';
  return '';
}
function who(id) {
  if (!id) return 'unknown';
  if (id === state.actor?.id) return 'you';
  const p = (state.principals || []).find((x) => x.id === id);
  return p ? p.name : 'another access key';
}

/* ---------------- glyphs (10px, one distinct shape per state) ---------------- */
function shapes(st, off) {
  return {
    none: '<circle cx="5" cy="5" r="3.9" fill="none" stroke="currentColor" stroke-width="1.2" stroke-dasharray="1.9 1.45"/>',
    progress:
      '<circle cx="5" cy="5" r="3.9" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M5 1.1A3.9 3.9 0 0 0 5 8.9Z" fill="currentColor"/>',
    prepared:
      '<rect x="1.35" y="1.35" width="7.3" height="7.3" rx=".6" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    approved: off
      ? '<path d="M5 1 9 5 5 9 1 5Z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>'
      : '<path d="M5 .3 9.7 5 5 9.7 .3 5Z" fill="currentColor"/>',
    submitted: off
      ? '<path d="M1 3.8h3.9V1.6L9 5 4.9 8.4V6.2H1Z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>'
      : '<path d="M.5 3.5h4.3V.7L9.6 5 4.8 9.3V6.5H.5Z" fill="currentColor"/>',
    verified: off
      ? '<circle cx="5" cy="5" r="4.1" fill="none" stroke="currentColor" stroke-width="1.2"/>'
      : '<circle cx="5" cy="5" r="4.7" fill="currentColor"/><path class="k" d="M2.8 5.1 4.3 6.6 7.3 3.5" stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round"/>',
    na: '<circle cx="5" cy="5" r="3.9" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M2.4 7.6 7.6 2.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
    review: off
      ? '<path d="M5 1.2 9 8.8H1Z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>'
      : '<path d="M5 .4 9.8 9.3H.2Z" fill="currentColor" stroke="currentColor" stroke-width=".6" stroke-linejoin="round"/><path class="k" d="M5 3.7v2.5" stroke-width="1.35" stroke-linecap="round"/><circle class="kf" cx="5" cy="7.8" r=".75"/>',
  }[st];
}
function g(st, off) {
  return `<svg class="g ${off ? 'g-off' : 'g-' + st}" data-glyph="${st}" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true" focusable="false">${shapes(st, off)}</svg>`;
}
const tick =
  '<svg class="g g-tick" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true" focusable="false"><path d="M1.5 5.3 4 7.6 8.6 2.6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ring =
  '<svg class="g g-approved" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true" focusable="false"><circle cx="5" cy="5" r="3.8" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>';
const dash =
  '<svg class="g g-off" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true" focusable="false"><path d="M2 5h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
const ymIcon =
  '<svg class="ym" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" focusable="false"><rect x="0" y="1" width="12" height="7" fill="currentColor"/><path d="M4 8h4l-2 3Z" fill="currentColor"/></svg>';
const taskGlyph = (t) => (t.state === 'superseded' ? dash : g(stateOf(t).g));
const bul = (l, sm) =>
  `<span class="bul${sm ? ' sm' : ''} lc-${l.k}" aria-hidden="true">${h(l.code)}</span>`;
const ICON = {
  journey:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="M1 3h4l3 3h2M1 11h4l3-3h2M1 7h9"/><rect x="10.2" y="4.2" width="2.8" height="5.6" rx="1.4" stroke-width="1.3"/></svg>',
  today:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="1.65" y="2.65" width="10.7" height="9.7" rx="1.6"/><path d="M1.65 5.6h10.7M4.6 1.2v2.6M9.4 1.2v2.6"/></svg>',
  evidence:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><path d="M3 1.65h5.2l2.8 2.8v7.9H3Z"/><path d="M8 1.65v3h3M5 7.5h4M5 9.8h4"/></svg>',
  records:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="1.65" y="1.65" width="10.7" height="10.7" rx="1.6"/><path d="M1.65 5.3h10.7M1.65 8.7h10.7M5 1.65v10.7"/></svg>',
  money:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" aria-hidden="true"><path d="M1.65 12.35h10.7M2.8 12.3V8.2M5.6 12.3V5.6M8.4 12.3V7M11.2 12.3V3.4"/></svg>',
  deals:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><path d="M1.65 5.2 7 1.65l5.35 3.55v7.15H1.65Z"/><path d="M5 12.35V8.3h4v4.05"/></svg>',
  documents:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><path d="M2.6 1.65h6l2.8 2.8v7.9H2.6Z"/><path d="M4.6 6.5h5M4.6 8.6h5M4.6 10.6h3"/></svg>',
  sales:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1.65 4.3h9.2l-2.2-2.2M12.35 9.7h-9.2l2.2 2.2"/></svg>',
  history:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" aria-hidden="true"><circle cx="7" cy="7" r="5.35"/><path d="M7 4v3.2l2.2 1.4"/></svg>',
  access:
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><path d="M1.65 2.65h10.7v6.8H6.4L3.6 12V9.45H1.65Z"/></svg>',
};

/* =====================================================================
   MAP GEOMETRY
   Every map is built here from the steps, on a fixed grid: horizontal, vertical and 45° segments
   only, each map with one corner radius, stations evenly spaced along each line. Station labels
   are wrapped against measured text widths so they cannot run into a neighbour. Any number of
   lines and steps lays out, including one line and none.
   ===================================================================== */
const FONT = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif';
const cv = document.createElement('canvas').getContext('2d');
const tw = (t, size, weight = 400) => {
  cv.font = `${weight} ${size}px ${FONT}`;
  return cv.measureText(t).width;
};
function wrapText(text, maxW, size, weight = 400) {
  const out = [];
  let cur = '';
  for (const w of String(text).split(/\s+/).filter(Boolean)) {
    const t = cur ? cur + ' ' + w : w;
    if (!cur || tw(t, size, weight) <= maxW) cur = t;
    else {
      out.push(cur);
      cur = w;
    }
  }
  if (cur) out.push(cur);
  // a single word too wide for the column is cut at measured width, so it cannot overrun
  const fitted = [];
  for (const line of out) {
    if (tw(line, size, weight) <= maxW || line.includes(' ')) {
      fitted.push(line);
      continue;
    }
    let part = '';
    for (const ch of line) {
      if (part && tw(part + ch + '-', size, weight) > maxW) {
        fitted.push(part + '-');
        part = ch;
      } else part += ch;
    }
    if (part) fitted.push(part);
  }
  // no single-word last line when the line above can spare a word and still fit
  const n = fitted.length;
  if (n > 1 && !fitted[n - 1].includes(' ')) {
    const prev = fitted[n - 2].split(' ');
    if (prev.length > 2) {
      const moved = prev.pop() + ' ' + fitted[n - 1];
      if (tw(moved, size, weight) <= maxW) {
        fitted[n - 2] = prev.join(' ');
        fitted[n - 1] = moved;
      }
    }
  }
  return fitted;
}
const f = (n) => Math.round(n * 100) / 100;

/* Polyline with every corner filleted by the same radius R. */
function rpath(pts, R) {
  let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i - 1],
      [x1, y1] = pts[i],
      [x2, y2] = pts[i + 1];
    const l1 = Math.hypot(x1 - x0, y1 - y0),
      l2 = Math.hypot(x2 - x1, y2 - y1);
    if (l1 < 1e-6 || l2 < 1e-6) continue;
    const u1 = [(x1 - x0) / l1, (y1 - y0) / l1],
      u2 = [(x2 - x1) / l2, (y2 - y1) / l2];
    const th = Math.acos(Math.max(-1, Math.min(1, u1[0] * u2[0] + u1[1] * u2[1])));
    if (th < 1e-6) continue;
    const t = R * Math.tan(th / 2);
    const cross = u1[0] * u2[1] - u1[1] * u2[0];
    d += ` L${f(x1 - u1[0] * t)} ${f(y1 - u1[1] * t)} A${R} ${R} 0 0 ${cross > 0 ? 1 : 0} ${f(x1 + u2[0] * t)} ${f(y1 + u2[1] * t)}`;
  }
  const L = pts[pts.length - 1];
  return d + ` L${f(L[0])} ${f(L[1])}`;
}
function linePath(l, pts, R, sw) {
  const d = rpath(pts, R);
  // Every line carries a casing path; the stylesheet leaves it transparent unless the line
  // needs an edge to stand off the surface in the current theme.
  return (
    `<path class="case case-${l.k}" d="${d}" stroke-width="${sw + 3}"/>` +
    `<path class="ln ln-${l.k}" d="${d}" stroke-width="${sw}"/>`
  );
}
function bulletSVG(l, cx, cy, r, size) {
  return `<circle class="bd bd-${l.k}" cx="${f(cx)}" cy="${f(cy)}" r="${r}"/><text class="bt bt-${l.k}" x="${f(cx)}" y="${f(cy + size * 0.36)}" text-anchor="middle" font-size="${size}">${h(l.code)}</text>`;
}
function glyphAt(st, cx, cy, size) {
  return `<svg class="g g-${st}" data-glyph="${st}" x="${f(cx - size / 2)}" y="${f(cy - size / 2)}" width="${size}" height="${size}" viewBox="0 0 10 10" overflow="visible">${shapes(st)}</svg>`;
}
function stationMark(s, cx, cy, pr, gs, selected) {
  return `<circle class="plate" cx="${f(cx)}" cy="${f(cy)}" r="${pr}"/>${glyphAt(stateOf(s.task).g, cx, cy, gs)}${selected ? `<circle class="selr" cx="${f(cx)}" cy="${f(cy)}" r="${pr + 3.5}"/>` : ''}`;
}
function tagSVG(cx, top, anchor) {
  const w = Math.ceil(tw('Your move', 11, 700)) + 12;
  const x = anchor === 'middle' ? cx - w / 2 : cx;
  return `<g class="tag"><rect x="${f(x)}" y="${f(top)}" width="${w}" height="18"/><text x="${f(x + 6)}" y="${f(top + 13)}">Your move</text></g>`;
}
/* A station's label block: wrapped name (number in bold), wrapped state, optional Your move tag. */
function labelBlock(s, maxW) {
  const you = Boolean(yourMove(s.task));
  const name = wrapText(`${s.n} ${s.task.title}`, maxW - 3, 12.5);
  const st = wrapText(shortState(s), maxW, 12, you ? 600 : 400);
  const widths = [
    ...name.map((l) => tw(l, 12.5) + 3),
    ...st.map((l) => tw(l, 12, you ? 600 : 400)),
  ];
  if (you) widths.push(tw('Your move', 11, 700) + 12);
  return { s, name, state: st, tag: you, w: Math.max(...widths), lines: name.length + st.length };
}
function labelSVG(L, x, base, anchor) {
  let out = '',
    y = base;
  L.name.forEach((ln, i) => {
    let t = h(ln);
    if (i === 0 && ln.startsWith(L.s.n))
      t = `<tspan class="t-no">${h(L.s.n)}</tspan>${h(ln.slice(L.s.n.length))}`;
    out += `<text class="t-name" x="${f(x)}" y="${f(y)}" text-anchor="${anchor}">${t}</text>`;
    y += 15;
  });
  L.state.forEach((ln) => {
    out += `<text class="t-state${L.tag ? ' you' : ''}" x="${f(x)}" y="${f(y)}" text-anchor="${anchor}">${h(ln)}</text>`;
    y += 15;
  });
  if (L.tag) out += tagSVG(x, y - 15 + 7, anchor);
  return out;
}
/* height of a label block below its first baseline */
const labelDepth = (L) => 15 * (L.lines - 1) + 4 + (L.tag ? 7 + 18 - 4 : 0);
const stnAria = (s) => `${s.n} ${s.task.title}, ${s.line.name} line: ${longState(s)}`;
const stnOpen = (s, sel) =>
  `<g class="stn" data-id="${h(s.id)}" tabindex="0" role="button" aria-label="${h(stnAria(s))}"${sel === s.id ? ' aria-current="true"' : ''}>`;
const mapTitle = () =>
  `Journey map: ${plural(M.lines.length, 'line')}, ${plural(M.steps.length, 'stop')}, all meeting at Open for business. ${M.verified} verified, ${M.na ? M.na + ' not applicable, ' : ''}${M.toGo} to go.`;
const toGoText = () => (M.toGo === 0 ? 'No stops to go' : `${plural(M.toGo, 'stop')} to go`);

/* An empty map: the interchange alone, waiting for its lines. */
function mapEmpty(cls) {
  return `<svg class="mp ${cls}" viewBox="0 0 330 92" role="img" aria-label="Journey map: no steps yet" data-map="empty">
    <rect class="ich empty-ich" x="20" y="22" width="24" height="48" rx="12"/>
    <text class="t-ich" x="58" y="42">Open for business</text>
    <text class="t-ich3" x="58" y="60">Lines appear here once a business has steps.</text></svg>`;
}

/* ---- 1. the full map, wide: horizontal lines converging on a vertical interchange ---- */
function mapWide(sel) {
  const L = M.lines,
    G = L.length;
  if (!G) return mapEmpty('mp-wide');
  const SW = 6,
    PR = 8.5,
    GS = 12,
    CAP = 12,
    RUN2 = 14,
    BX = 18,
    BR = 15,
    S = 120;
  const maxN = Math.max(...L.map((l) => l.steps.length));
  const labs = L.map((l) => l.steps.map((st) => labelBlock(st, S - 10)));
  const names = L.map((l) => wrapText(l.name, 112, 12.5, 700));
  const counts = L.map((l) => {
    const c = lineStats(l);
    return [`${c.verified} of ${c.total} verified`, `${c.verified} of ${c.total}`];
  });
  const nameW = Math.max(
    ...names.flat().map((t) => tw(t, 12.5, 700)),
    ...counts.map((c) => tw(c[1], 11.5)),
  );
  const firstHalf = Math.max(...labs.map((row) => (row[0] ? row[0].w / 2 : 0)));
  const X1 = Math.ceil(Math.max(136, 3 + nameW + 10 + firstHalf));
  const depthOf = (Lb) => 26 + labelDepth(Lb);
  const nameDepth = (k) => 47 + 15 * (names[k].length - 1);
  const D = Math.ceil(
    Math.max(
      ...labs.flat().map((Lb) => depthOf(Lb) + PR + 12),
      ...L.map((_, k) => nameDepth(k) + 12),
    ),
  );
  const lastX = X1 + (maxN - 1) * S;
  const lastHalf = Math.max(...labs.map((row) => (row.length === maxN ? row[maxN - 1].w / 2 : 0)));
  const clear = Math.ceil(Math.max(lastHalf - 6, 24));
  // the lines close up to pitch E at the interchange; the fan-in is capped so it stays compact
  const mid = (G - 1) / 2;
  const E = G === 1 ? 0 : Math.max(20, Math.min(D, D - 240 / (G - 1)));
  const drop = mid * (D - E);
  const xbS = lastX + clear,
    xd = xbS + drop,
    xcap = xd + RUN2;
  const diag = G === 1 ? Infinity : (G % 2 ? 1 : 0.5) * (D - E) * Math.SQRT2;
  const R = Math.max(2, Math.min(12, diag / 2 / Math.tan(Math.PI / 8)));
  const y0 = 56,
    yk = (k) => y0 + k * D,
    ymid = y0 + mid * D;
  const t1 = 'Open for business',
    t2 = toGoText();
  const W = Math.ceil(
    Math.max(xcap + CAP + 4, xcap + 2 + Math.max(tw(t1, 15, 700), tw(t2, 12.5, 700)) / 2),
  );
  let lines = '',
    bullets = '',
    heads = '',
    stations = '';
  L.forEach((l, k) => {
    const y = yk(k),
      entry = ymid + (k - mid) * E,
      dy = Math.abs(entry - y),
      xb = xd - dy;
    const pts =
      dy < 0.01
        ? [
            [BX, y],
            [xcap, y],
          ]
        : [
            [BX, y],
            [xb, y],
            [xd, entry],
            [xcap, entry],
          ];
    lines += linePath(l, pts, R, SW);
    bullets += bulletSVG(l, BX, y, BR, 12);
    let ny = y + 32;
    for (const ln of names[k]) {
      heads += `<text class="t-line" x="3" y="${ny}">${h(ln)}</text>`;
      ny += 15;
    }
    const room = X1 - (labs[k][0] ? labs[k][0].w / 2 : 0) - 8 - 3;
    const cnt = tw(counts[k][0], 11.5) <= room ? counts[k][0] : counts[k][1];
    heads += `<text class="t-count" x="3" y="${ny}">${h(cnt)}</text>`;
    l.steps.forEach((st, i) => {
      const cx = X1 + i * S,
        Lb = labs[k][i];
      stations +=
        stnOpen(st, sel) +
        `<rect class="hit" x="${f(cx - S / 2 + 3)}" y="${f(y - 14)}" width="${f(S - 6)}" height="${f(depthOf(Lb) + 18)}" rx="6"/>` +
        stationMark(st, cx, y, PR, GS, sel === st.id) +
        labelSVG(Lb, cx, y + 26, 'middle') +
        '</g>';
    });
  });
  const capY = ymid - mid * E - CAP,
    capH = (G - 1) * E + 2 * CAP;
  const ich =
    `<rect class="ich" x="${f(xcap - CAP)}" y="${f(capY)}" width="${2 * CAP}" height="${f(capH)}" rx="${CAP}"/>` +
    `<text class="t-ich" x="${W - 2}" y="${y0 - 34}" text-anchor="end">${t1}</text><text class="t-ich2" x="${W - 2}" y="${y0 - 17}" text-anchor="end">${h(t2)}</text>`;
  const yl = yk(G - 1);
  const H = Math.ceil(
    Math.max(yl + Math.max(...labs[G - 1].map((Lb) => depthOf(Lb) + 4), 0), yl + nameDepth(G - 1)) +
      8,
  );
  return `<svg class="mp mp-wide" viewBox="0 0 ${W} ${H}" data-w="${W}" role="group" aria-labelledby="mw-t" data-map="wide">
    <title id="mw-t">${h(mapTitle())}</title>
    <g aria-hidden="true">${lines}${ich}${bullets}${heads}</g>${stations}</svg>`;
}

/* ---- 2. the full map, tall: each line runs down through its stops, then parks beside the next ---- */
function mapTall(sel) {
  const L = M.lines,
    G = L.length;
  if (!G) return mapEmpty('mp-tall');
  const TS = G <= 5 ? 20 : 14,
    R = 10,
    SW = 6,
    PR = 8.5,
    GS = 12,
    BR = 13,
    A = 18;
  const S = Math.max(40, 20 + TS * (G - 1));
  const LX = S + 18;
  const W = Math.max(330, LX + 180);
  const maxW = W - LX - 2;
  const labs = L.map((l) => l.steps.map((st) => labelBlock(st, maxW)));
  const bottomOf = (Lb) => 4.5 + labelDepth(Lb);
  const T = Math.ceil(Math.max(...labs.flat().map((Lb) => bottomOf(Lb) + 9)) + 16);
  let y = 22,
    lines = '',
    bullets = '',
    heads = '',
    stations = '';
  const tracks = [];
  L.forEach((l, k) => {
    const c = lineStats(l);
    const yTop = y;
    bullets += bulletSVG(l, S, yTop, BR, 11);
    const cnt = `${c.verified} of ${c.total} verified`;
    const headW = W - S - 24;
    const oneLine = tw(l.name, 12.5, 700) + 8 + tw(cnt, 11.5) <= headW;
    const nameLines = oneLine ? [l.name] : wrapText(l.name, headW, 12.5, 700);
    let hy = yTop + 5;
    nameLines.forEach((ln, i) => {
      const tail = oneLine && i === 0 ? `<tspan class="t-count" dx="8">${h(cnt)}</tspan>` : '';
      heads += `<text class="t-line" x="${S + 22}" y="${hy}">${h(ln)}${tail}</text>`;
      hy += 15;
    });
    if (!oneLine) {
      heads += `<text class="t-count" x="${S + 22}" y="${hy}">${h(cnt)}</text>`;
      hy += 15;
    }
    const first = yTop + 38 + (hy - (yTop + 20));
    let yLast = yTop;
    l.steps.forEach((st, i) => {
      const cy = first + i * T;
      yLast = cy;
      const Lb = labs[k][i];
      stations +=
        stnOpen(st, sel) +
        `<rect class="hit" x="${S - 15}" y="${f(cy - 14)}" width="${W - S + 13}" height="${f(bottomOf(Lb) + 20)}" rx="6"/>` +
        stationMark(st, S, cy, PR, GS, sel === st.id) +
        labelSVG(Lb, LX, cy + 4.5, 'start') +
        '</g>';
    });
    if (!l.steps.length) yLast = yTop + 20;
    const d = TS * (G - 1 - k);
    tracks.push({ l, yTop, yLast, d });
    const lastBottom = l.steps.length ? bottomOf(labs[k][l.steps.length - 1]) : 0;
    y = Math.ceil(Math.max(yLast + A + 30, yLast + lastBottom + 28));
  });
  const last = tracks[G - 1];
  const lastLab = labs[G - 1][labs[G - 1].length - 1];
  const yCap = Math.ceil(
    Math.max(
      last.yLast + (lastLab ? bottomOf(lastLab) : 0) + 34,
      ...tracks.map((t) => t.yLast + A + t.d + R + 6),
    ),
  );
  tracks.forEach((t) => {
    const pts =
      t.d === 0
        ? [
            [S, t.yTop],
            [S, yCap],
          ]
        : [
            [S, t.yTop],
            [S, t.yLast + A],
            [S - t.d, t.yLast + A + t.d],
            [S - t.d, yCap],
          ];
    lines += linePath(t.l, pts, R, SW);
  });
  const ich =
    `<rect class="ich" x="${S - (G - 1) * TS - 12}" y="${yCap - 12}" width="${(G - 1) * TS + 24}" height="24" rx="12"/>` +
    `<text class="t-ich" x="${S + 22}" y="${yCap - 3}">Open for business</text>` +
    `<text class="t-ich2" x="${S + 22}" y="${yCap + 14}">${h(toGoText())}</text>` +
    `<text class="t-ich3" x="${S + 22}" y="${yCap + 30}">${M.verified} of ${M.steps.length} verified</text>`;
  const H = yCap + 38;
  return `<svg class="mp mp-tall" viewBox="0 0 ${W} ${H}" role="group" aria-labelledby="mt-t" data-map="tall">
    <title id="mt-t">${h(mapTitle())}</title>
    <g aria-hidden="true">${lines}${ich}${bullets}${heads}</g>${stations}</svg>`;
}

/* ---- 3. the Today strip: the same lines at 330px, position on every line ---- */
function mapMini() {
  const L = M.lines,
    G = L.length;
  if (!G) return mapEmpty('');
  const Y0 = 13,
    DY = 20,
    X0 = 40,
    BX = 10,
    BR = 9,
    E = 10,
    R = 6,
    SW = 4,
    PR = 6.5,
    GS = 10;
  const maxN = Math.max(1, ...L.map((l) => l.steps.length));
  const mid = (G - 1) / 2,
    drop = mid * (DY - E);
  const texts = ['Open for business', toGoText(), `${M.verified} of ${M.steps.length} verified`];
  const textW = Math.max(tw(texts[0], 12, 700), tw(texts[1], 12, 700), tw(texts[2], 11.5));
  const room = 330 - 2 - textW - 16 - 10 - Math.max(drop + 12, 20) - X0;
  const sp = Math.max(16, Math.min(36, maxN > 1 ? room / (maxN - 1) : 36));
  const lastX = X0 + (maxN - 1) * sp;
  const xd = lastX + Math.max(sp, drop + 12),
    xcap = xd + 10,
    xt = xcap + 16;
  const W = Math.ceil(Math.max(330, xt + textW + 2));
  const yk = (k) => Y0 + k * DY,
    ymid = Y0 + mid * DY;
  let lines = '',
    marks = '';
  L.forEach((l, k) => {
    const y = yk(k),
      entry = ymid + (k - mid) * E,
      dy = Math.abs(entry - y),
      xb = xd - dy;
    lines += linePath(
      l,
      dy < 0.01
        ? [
            [BX, y],
            [xcap, y],
          ]
        : [
            [BX, y],
            [xb, y],
            [xd, entry],
            [xcap, entry],
          ],
      R,
      SW,
    );
    marks += bulletSVG(l, BX, y, BR, 9.5);
    l.steps.forEach((s, i) => {
      const x = X0 + i * sp;
      marks += stationMark(s, x, y, PR, GS, false);
      if (yourMove(s.task)) marks += `<path class="ymk" d="M${f(x - 4)} ${y - 13}h8l-4 5Z"/>`;
    });
  });
  const ich =
    `<rect class="ich thin" x="${f(xcap - 8)}" y="${f(ymid - mid * E - 8)}" width="16" height="${f((G - 1) * E + 16)}" rx="8"/>` +
    `<text class="t-mini" x="${f(xt)}" y="${f(ymid - 7)}">${texts[0]}</text>` +
    `<text class="t-mini" x="${f(xt)}" y="${f(ymid + 9)}">${h(texts[1])}</text>` +
    `<text class="t-mini2" x="${f(xt)}" y="${f(ymid + 24)}">${h(texts[2])}</text>`;
  const H = Math.ceil(Math.max(yk(G - 1) + PR + 4, ymid + 30));
  return `<svg class="mp" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true" focusable="false" data-map="mini">${lines}${ich}${marks}</svg>`;
}

/* ---- 4. the step fragment: one line, this stop, and how far to the interchange ---- */
function mapFrag(s) {
  const l = s.line,
    st = l.steps,
    i = s.index;
  const Y = 46,
    BX = 14,
    BR = 12,
    X0 = 64,
    SP = 58,
    PR = 8.5,
    GS = 12,
    SW = 6;
  const lastX = X0 + (st.length - 1) * SP;
  const xcap = Math.max(300, lastX + 62);
  const W = xcap + 16;
  let out = linePath(
    l,
    [
      [BX, Y],
      [xcap, Y],
    ],
    12,
    SW,
  );
  out += `<rect class="ich" x="${xcap - 10}" y="${Y - 18}" width="20" height="36" rx="10"/>`;
  out += `<text class="t-mini" x="${xcap + 10}" y="16" text-anchor="end">Open for business</text>`;
  out += bulletSVG(l, BX, Y, BR, 10.5);
  const numW = (x) => tw(x.n, 12);
  const curLabel = `${s.n} · this step`;
  const curHalf = tw(curLabel, 12, 700) / 2;
  const fits =
    (i === 0 || curHalf + 4 + numW(st[i - 1]) / 2 <= SP) &&
    (i === st.length - 1 || curHalf + 4 + numW(st[i + 1]) / 2 <= SP);
  st.forEach((x, j) => {
    const cx = X0 + j * SP;
    out += stationMark(x, cx, Y, PR, GS, j === i);
    if (yourMove(x.task)) out += `<path class="ymk" d="M${cx - 5} ${Y - 21}h10l-5 6Z"/>`;
    const text = j === i && fits ? curLabel : x.n;
    out += `<text class="t-num${j === i ? ' cur' : ''}" x="${cx}" y="${Y + 30}" text-anchor="middle">${h(text)}</text>`;
  });
  const after = st.length - 1 - i;
  const xa = X0 + i * SP,
    yd = Y + 42;
  const txt =
    after === 0 ? 'Last stop before the interchange' : `${plural(after, 'more stop')} on this line`;
  const tl = tw(txt, 12);
  const cxT = Math.min(W - 2 - tl / 2, Math.max(2 + tl / 2, (xa + xcap) / 2));
  out += `<path class="dim" d="M${xa} ${yd - 4}v8M${xa} ${yd}H${xcap}M${xcap} ${yd - 4}v8"/>`;
  out += `<text class="t-num" x="${f(cxT)}" y="${yd + 17}" text-anchor="middle">${txt}</text>`;
  const desc =
    `${l.name} line, in order: ` +
    st.map((x) => `${x.n} ${x.task.title}, ${longState(x)}`).join('; ') +
    `; then Open for business. This step is stop ${i + 1} of ${st.length}.`;
  return `<svg class="mp" viewBox="0 0 ${W} ${yd + 24}" width="${W}" height="${yd + 24}" role="img" aria-labelledby="fr-t fr-d" data-map="frag">
    <title id="fr-t">Where step ${h(s.n)} sits on the ${h(l.name)} line</title><desc id="fr-d">${h(desc)}</desc>${out}</svg>`;
}

/* The wide map only where it fits: an inspector of 700px or more, and the map not shrunk below
   85% of its drawn size. Otherwise the tall map, which holds any number of lines readably. */
function fitMaps() {
  for (const box of document.querySelectorAll('.mapbox')) {
    const wide = box.querySelector('.mp-wide');
    if (!wide) continue;
    const w = box.clientWidth;
    const natural = Number(wide.dataset.w || 0);
    box.dataset.fit = w >= 700 && natural * 0.85 <= w ? 'wide' : 'tall';
  }
}
const fitter = typeof ResizeObserver === 'function' ? new ResizeObserver(() => fitMaps()) : null;

/* ---------------- sources ---------------- */
function sourceList() {
  if (!state.snapshot) return [{ id: 'today', name: 'Today', kind: 'top' }];
  return [
    { id: 'journey', name: 'Journey', kind: 'top' },
    { id: 'today', name: 'Today', kind: 'top' },
    ...M.lines.map((l) => ({ id: l.id, name: l.name, kind: 'line', line: l })),
    { id: 'evidence', name: 'Evidence', kind: 'rec' },
    { id: 'records', name: 'Business records', kind: 'rec' },
    { id: 'money', name: 'Money', kind: 'rec' },
    { id: 'deals', name: 'Acquisition analyses', kind: 'rec' },
    { id: 'documents', name: 'Documents', kind: 'rec' },
    { id: 'sales', name: 'Invoices & payments', kind: 'rec' },
    { id: 'history', name: 'History', kind: 'rec' },
    { id: 'access', name: 'Access & assistants', kind: 'rec' },
  ];
}
const sourceById = (id) => sourceList().find((s) => s.id === id);
const RECORD_KINDS = [
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
const waitingOnYou = () => M.steps.filter((s) => yourMove(s.task));

function renderSources() {
  const nav = $('#src-nav');
  let out = '';
  let section = '';
  for (const s of sourceList()) {
    if (s.kind === 'line' && section !== 'line') {
      out += '<div class="src-h">Lines</div>';
      section = 'line';
    }
    if (s.kind === 'rec' && section !== 'rec') {
      out += '<div class="src-h">Records</div>';
      section = 'rec';
    }
    let lead = '',
      n = '',
      title = '';
    if (s.kind === 'line') {
      const c = lineStats(s.line);
      lead = `<span class="ico">${bul(s.line)}</span>`;
      n = (c.review ? g('review') : '') + `${c.verified}/${c.total}`;
      title = `${plural(c.total, 'step')}: ${c.verified} verified${c.review ? ', some need review' : ''}`;
    } else {
      lead = `<span class="ico">${ICON[s.id] || ''}</span>`;
      if (s.id === 'journey') {
        n = `${M.toGo} to go`;
        title = `${M.toGo} of ${plural(M.steps.length, 'stop')} still to go`;
      }
      if (s.id === 'today' && state.snapshot) {
        const w = waitingOnYou().length;
        n = w ? String(w) : '';
        title = `${w} waiting on you`;
      }
      if (s.id === 'evidence') {
        const ev = objs('evidence');
        n = (ev.some((e) => e.state === 'rejected') ? g('review') : '') + ev.length;
      }
      if (s.id === 'records')
        n = String(objs().filter((o) => RECORD_KINDS.includes(o.kind)).length);
      if (s.id === 'money') {
        const p = objs('financial_plan');
        n = (p.some((x) => x.stale) ? g('review') : '') + p.length;
      }
      if (s.id === 'deals') {
        const d = objs('deal');
        n = (d.some((x) => x.stale) ? g('review') : '') + d.length;
      }
      if (s.id === 'documents') {
        const d = objs('document');
        n = (d.some((x) => x.stale) ? g('review') : '') + d.length;
        if (d.some((x) => x.stale)) title = 'Some drafts were made from records that changed since';
      }
      if (s.id === 'sales') {
        const inv = objs('invoice');
        n =
          ([...inv, ...objs('payment')].some((x) => x.evidence_warning) ? g('review') : '') +
          inv.length;
      }
    }
    out += `<button type="button" class="src" data-src="${h(s.id)}" aria-current="${s.id === state.source}"${title ? ` title="${h(title)}"` : ''}>${lead}<span class="lbl">${h(s.name)}</span><span class="n">${n}</span></button>`;
  }
  nav.innerHTML = out;
  $('#src-foot').innerHTML = keyHTML() + accountHTML();
}
function keyHTML() {
  const rows = [
    'pending',
    'in_progress',
    'prepared',
    'approved',
    'submitted',
    'verified',
    'not_applicable',
    'needs_review',
  ].map((s) => [STATES[s].g, STATES[s].label, STATES[s].key]);
  return `<div class="key-h">Key</div><ul class="key">${rows.map(([s, a, b]) => `<li>${g(s)}<b>${a}</b><span>${b}</span></li>`).join('')}<li>${ymIcon}<b>Your move</b><span>waiting on you</span></li></ul>`;
}
function accountHTML() {
  if (!state.actor) return '';
  return `<div class="account"><p>Signed in as <b>${h(state.actor.name)}</b> · ${h(label(state.actor.role))}</p><div class="i-actions">${isOwner() ? btn('+ New business', 'data-action="new-business"') : ''}${btn('Sign out', 'data-action="logout"')}</div></div>`;
}

/* ---------------- middle list ---------------- */
/* Each row is a native button inside its own list item, so it is announced as something that can
   be activated, and its list still reports its length and position. groupRows() puts each run of
   rows into a list of its own; headings, notes and empty states stay outside the lists. */
function row({ key, glyph, num = '', title, right = '', sub = '', fig = false, line = null }) {
  return `<li class="ri"><button type="button" class="row${sub ? ' two' : ''}${num ? '' : ' nonum'}${line ? ' ln lc-' + line.k : ''}"${line ? ` data-line="${line.k}"` : ''} data-key="${h(key)}" aria-current="${key === state.selected}">
    <span class="r-g">${glyph}</span>
    <span class="r-main"><span class="r-l1"><span class="r-num">${h(num)}</span><span class="r-t">${title}</span><span class="r-right${fig ? ' fig' : ''}">${right}</span></span>${sub ? `<span class="r-sub">${sub}</span>` : ''}</span>
  </button></li>`;
}
function stepSub(s) {
  const t = s.task;
  if (t.state === 'needs_review') return 'Reopened: ' + (reopened(t)?.note || 'inputs changed');
  const mv = yourMove(t);
  if (mv) return capFirst(mv) + (t.blockers?.length ? ' · ' + t.blockers[0] : '');
  if (t.state === 'submitted') return 'With the outside body · add their receipt when it arrives';
  if (!FINISHED.has(t.state) && t.blockers?.length) return t.blockers[0];
  return '';
}
/* Under a line's own header the header names the line. Where steps from several lines share a
   heading, as on Today, the row names its line in text, so colour is never the only cue. */
const stepRow = (s, sub, prefix = 'step', named = false) => {
  const text = sub === undefined ? stepSub(s) : sub;
  return row({
    key: prefix + ':' + s.id,
    glyph: taskGlyph(s.task),
    num: s.n,
    title: h(s.task.title),
    right: stateOf(s.task).label,
    sub: h(named ? [`${s.line.name} line`, text].filter(Boolean).join(' · ') : text),
    line: s.line,
  });
};
const grp = (name, n, note) =>
  `<h2 class="grp"><span>${name}</span>${n !== undefined && n !== '' ? `<span class="grp-n">${n}</span>` : ''}</h2>${note ? `<div class="grp-note">${note}</div>` : ''}`;
const grpLine = (l, text, n) =>
  `<h2 class="grp ln lc-${l.k}${l.steps.length ? '' : ' bare'}" data-line="${l.k}">${bul(l, true)}<span>${h(text)}</span><span class="grp-n">${h(n)}</span></h2>`;
function evInfo(o) {
  if (o.state === 'rejected')
    return { glyph: g('review'), label: 'Withdrawn', amber: true, short: 'Withdrawn' };
  if (o.state === 'verified')
    return {
      glyph: tick,
      label: o.data.source_type === 'external_receipt' ? 'Reviewed external receipt' : 'Reviewed',
      short: 'Reviewed',
    };
  return { glyph: ring, label: 'Not yet reviewed', short: 'Not yet reviewed' };
}
const evRow = (o) => {
  const info = evInfo(o);
  const uses = usedBy(o.id).filter((x) => !replaced(x)).length;
  return row({
    key: 'ev:' + o.id,
    glyph: info.glyph,
    title: h(o.title),
    sub: h(
      `${info.label} · ${label(o.data.source_type)} · ${uses ? `used by ${uses}` : 'not used yet'}`,
    ),
  });
};
function headButton(text, action) {
  return canWrite() ? btn(text, `data-action="${action}"`, false, true) : '';
}

function renderList() {
  const list = $('#list');
  const src = sourceById(state.source) || sourceById('today');
  state.source = src.id;
  $('#list-title').textContent = src.name;
  $('#q').placeholder = 'Search ' + src.name;
  let right = '';
  let out = '';
  const snap = state.snapshot;
  if (!snap) {
    out = `<p class="empty">${state.businesses.length ? 'Choose a business above.' : '<b>No business yet.</b> Once you create one, its steps appear here, grouped into lines, with what is waiting on you first.'}</p>`;
    out += row({
      key: 'start:welcome',
      glyph: ICON.journey.replace('width="14" height="14"', 'width="12" height="12"'),
      title: 'Getting started',
      sub: 'What a workspace holds, and the first step',
    });
  } else if (src.id === 'today') {
    const mine = waitingOnYou();
    const review = M.steps.filter((s) => s.task.state === 'needs_review');
    const decide = mine.filter((s) => s.task.state !== 'needs_review');
    const away = M.steps.filter((s) => s.task.state === 'submitted');
    const ready = M.steps.filter(
      (s) => s.task.state === 'pending' && !(s.task.blockers || []).length,
    );
    right = mine.length ? `${mine.length} need you` : '';
    const counts = [
      `<span><b>${M.verified}</b> verified</span>`,
      `<span><b>${away.length}</b> with an outside body</span>`,
      `<span><b>${mine.length}</b> waiting on you</span>`,
      `<span><b>${M.steps.filter((s) => s.task.state === 'pending').length}</b> not started</span>`,
    ];
    if (review.length) counts.push(`<span><b>${review.length}</b> need review</span>`);
    if (M.na) counts.push(`<span><b>${M.na}</b> not applicable</span>`);
    out += `<div class="today-head">
      <div class="th-date">${h(new Date().toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' }))}</div>
      <div class="th-sub">${h(businessName())} · ${plural(M.steps.length, 'step')} on ${plural(M.lines.length, 'line')}</div>
      <button type="button" class="mini" id="mini" aria-label="Open the journey map: ${h(toGoText())}, ${M.verified} of ${M.steps.length} verified">${mapMini()}</button>
      <div class="th-counts">${counts.join('')}<span class="inline-g">${ymIcon} your move</span></div>
    </div>`;
    if (review.length) {
      out += grp(
        'Needs review',
        review.length,
        'What these relied on changed. Earlier decisions stay in their history.',
      );
      for (const s of review) out += stepRow(s, undefined, 'step', true);
    }
    if (decide.length) {
      out += grp('Waiting on you', decide.length);
      for (const s of decide) out += stepRow(s, undefined, 'step', true);
    }
    if (away.length) {
      out += grp('Waiting on someone else', away.length);
      for (const s of away)
        out += stepRow(
          s,
          'With the outside body · add their receipt when it arrives',
          'step',
          true,
        );
    }
    if (ready.length) {
      out += grp('Ready to start', ready.length, 'Every prerequisite for these is complete.');
      for (const s of ready) out += stepRow(s, s.task.data.description || '', 'step', true);
    }
    out += moneyRows(true);
    const changed = changedRows();
    if (changed.length) {
      out += grp('Out of date or unreviewed', changed.length);
      out += changed.join('');
    }
    if (!review.length && !decide.length && !away.length && !ready.length)
      out += `<p class="empty">Nothing is waiting on a decision. Steps that are not started still wait on their prerequisites; they are listed on each line.</p>`;
  } else if (src.id === 'journey') {
    right = toGoText();
    out += row({
      key: 'map:all',
      glyph: ICON.journey.replace('width="14" height="14"', 'width="12" height="12"'),
      title: 'Whole map',
      sub: h(
        `${M.verified} of ${plural(M.steps.length, 'step')} verified · ${plural(M.lines.length, 'line')}`,
      ),
    });
    for (const l of M.lines) {
      const c = lineStats(l);
      out += grpLine(l, l.name, `${c.verified} of ${c.total} verified`);
      for (const s of l.steps) out += stepRow(s, undefined, 'jr');
    }
  } else if (src.kind === 'line') {
    const l = src.line,
      c = lineStats(l);
    right = `${c.verified} of ${c.total} verified`;
    out += grpLine(l, `${l.name} line`, plural(c.total, 'step'));
    if (l.deal) out += `<div class="grp-note">Diligence for ${h(l.deal.title)}</div>`;
    for (const s of l.steps) out += stepRow(s);
  } else if (src.id === 'evidence') {
    const ev = objs('evidence');
    right = plural(ev.length, 'item');
    right += headButton('+ Add evidence', 'new-evidence');
    const groups = [
      ['Not yet reviewed', ev.filter((e) => e.state === 'unverified')],
      ['Reviewed', ev.filter((e) => e.state === 'verified')],
      ['Withdrawn', ev.filter((e) => e.state === 'rejected')],
    ];
    for (const [name, items] of groups) {
      if (!items.length) continue;
      out += grp(
        name,
        items.length,
        name === 'Withdrawn'
          ? 'Originals are never changed. What relied on these was reopened.'
          : '',
      );
      for (const o of items) out += evRow(o);
    }
    if (!ev.length)
      out += `<p class="empty">No evidence yet. Add interview notes, original document text, provider receipts or observations. A person reviews each item before it can support an approval.</p>`;
    out += `<p class="fine empty">Originals are stored as received and never edited. Corrections are new evidence.</p>`;
  } else if (src.id === 'records') {
    const recs = objs().filter((o) => RECORD_KINDS.includes(o.kind));
    right = plural(recs.length, 'record') + headButton('+ Add record', 'new-record');
    for (const kind of RECORD_KINDS) {
      const items = recs.filter((o) => o.kind === kind);
      if (!items.length) continue;
      out += grp(label(kind), items.length);
      for (const o of items)
        out += row({
          key: 'rec:' + o.id,
          glyph: certaintyGlyph(o.state),
          title: h(o.title),
          right: h(label(o.state)),
          sub: h(o.data.details || ''),
        });
    }
    if (!recs.length)
      out += `<p class="empty">Start with the customer and offer: a buyer, a problem, an offer, the rights to deliver it, and someone responsible.</p>`;
  } else if (src.id === 'money') {
    const plans = objs('financial_plan');
    right =
      plural(plans.length, 'version') +
      headButton(plans.length ? '+ New version' : '+ Build model', 'new-forecast');
    out += moneyRows(false);
    if (plans.length) {
      out += grp('Model versions', plans.length, 'Each version is kept as it was calculated.');
      for (const p of plans.slice().reverse())
        out += row({
          key: 'plan:' + p.id,
          glyph: p.stale ? g('review') : p === plans.at(-1) ? tick : dash,
          title: h(p.title),
          right: h(dDay(p.created_at)),
          sub: h(
            `${p.data.inputs.months} months · ${p.data.scenarios.base.summary.statements_balance ? 'statements balance' : 'balance check failed'}${p === plans.at(-1) ? ' · latest' : ''}${p.stale ? ' · needs review' : ''}`,
          ),
        });
    }
  } else if (src.id === 'deals') {
    const deals = objs('deal');
    right =
      plural(deals.length, 'analysis', 'analyses') + headButton('+ Analyze a business', 'new-deal');
    for (const d of deals.slice().reverse())
      out += row({
        key: 'deal:' + d.id,
        glyph: d.stale ? g('review') : tick,
        title: h(d.title),
        right: d.stale ? 'Needs review' : 'Screening',
        // why it needs review is the server's to say, in the analysis itself
        sub: h(`Normalized EBITDA ${money(d.data.analysis.normalized_ebitda)}`),
      });
    if (!deals.length)
      out += `<p class="empty">Screen a target with explicit assumptions. Unsupported add-backs are excluded; reviewed evidence is still a human assessment, not an audit.</p>`;
  } else if (src.id === 'documents') {
    const docs = objs('document');
    right = plural(docs.length, 'draft') + headButton('+ Generate document', 'new-document');
    for (const d of docs.slice().reverse())
      out += row({
        key: 'doc:' + d.id,
        glyph: d.stale ? g('review') : tick,
        title: h(d.title),
        right: h(dDay(d.created_at)),
        sub: h(d.stale ? 'Source records changed since this draft' : 'Matches the current records'),
      });
    if (!docs.length)
      out += `<p class="empty">Generate a business plan, structure brief, contract brief, operating manual, readiness report or investment memo from the current records.</p>`;
  } else if (src.id === 'sales') {
    const inv = objs('invoice'),
      pay = objs('payment');
    right = plural(inv.length, 'invoice') + headButton('+ Draft invoice', 'new-invoice');
    out += moneyRecordedRows();
    if (inv.length) {
      out += grp(
        'Invoices',
        inv.length,
        'A draft is internal. Business Foundry never sends an invoice.',
      );
      for (const o of inv.slice().reverse())
        out += row({
          key: 'inv:' + o.id,
          glyph: o.evidence_warning ? g('review') : invoiceGlyph(o.state),
          title: h(o.title),
          right: money(o.data.amount, o.data.currency),
          fig: true,
          sub: h(
            `${label(o.state)} · due ${plainDate(o.data.due_date)}${o.evidence_warning ? ' · supporting evidence needs review' : ''}`,
          ),
        });
    } else
      out += `<p class="empty">Attach reviewed evidence of the agreement and delivery acceptance, then draft the invoice and record its issue and payment.</p>`;
    if (pay.length) {
      out += grp('Payment receipts', pay.length);
      for (const o of pay.slice().reverse())
        out += row({
          key: 'pay:' + o.id,
          glyph: o.evidence_warning ? g('review') : tick,
          title: h(o.title),
          right: money(o.data.amount, o.data.currency),
          fig: true,
          sub: h(
            `Received ${plainDate(o.data.received_on)}${o.evidence_warning ? ' · evidence withdrawn; reconcile this entry' : ''}`,
          ),
        });
    }
  } else if (src.id === 'history') {
    right = `<a class="btn small" href="/api/businesses/${h(state.bid)}/export">Export business records</a>`;
    out += row({
      key: 'integrity',
      glyph: ICON.history.replace('width="14" height="14"', 'width="12" height="12"'),
      title: 'Integrity check',
      sub: 'History against the current records',
    });
    if (state.audit?.load === state.loaded) {
      const evs = state.audit.data.events.slice().reverse();
      out += grp('Events', evs.length, 'Newest first. Every change keeps its actor and sequence.');
      for (const e of evs)
        out += row({
          key: 'evt:' + e.seq,
          glyph: dash,
          title: h(eventTitle(e)),
          right: h(dDay(e.created_at)),
          sub: h(`${e.payload.title || e.payload.data?.name || e.object_id} · ${who(e.actor_id)}`),
        });
    } else {
      out += `<p class="empty" data-audit-wait>Loading history…</p>`;
      const load = state.loaded;
      const current = () => state.loaded === load && state.source === 'history';
      loadAudit().then(
        () => {
          if (current() && $('#list [data-audit-wait]')) rerender(renderList);
        },
        (e) => {
          const wait = $('#list [data-audit-wait]');
          if (current() && wait)
            wait.textContent = `The history could not be loaded: ${messageFor(e)}`;
        },
      );
    }
  } else if (src.id === 'access') {
    right = isOwner() ? headButton('+ Create access key', 'new-key') : '';
    out += row({
      key: 'ai:overview',
      glyph: ICON.access.replace('width="14" height="14"', 'width="12" height="12"'),
      title: 'Assistants and sharing',
      sub: state.actor.ai_configured
        ? 'Optional review is configured · advice only'
        : 'Not configured · the workspace works without it',
    });
    if (isOwner() && state.principals) {
      out += grp('Access keys', state.principals.length);
      for (const k of state.principals)
        out += row({
          key: 'key:' + k.id,
          glyph: k.revoked ? dash : tick,
          title: h(k.name),
          right: h(k.revoked ? 'Revoked' : label(k.role)),
          sub: h(
            `${k.role === 'owner' ? 'All businesses' : plural(k.business_ids.length, 'business', 'businesses')}${k.id === state.actor.id ? ' · you' : ''}`,
          ),
        });
    }
    if (state.runs.length) {
      out += grp(
        'Assistant reviews',
        state.runs.length,
        'Advice only. No review can approve, submit or verify.',
      );
      for (const r of state.runs.slice().reverse())
        out += row({
          key: 'run:' + r.id,
          glyph: r.state === 'succeeded' ? tick : g('review'),
          title: h(r.data.advice?.summary || r.data.error || 'Provider outcome not confirmed'),
          right: h(dDay(r.created_at)),
          sub: h(`${r.data.request?.model || 'model not recorded'} · ${label(r.state)}`),
        });
    }
  }
  $('#list-right').innerHTML = right;
  out += `<div class="list-key">${keyHTML()}</div>`;
  list.innerHTML = out;
  groupRows(list);
  ensureSelection();
  joinLines();
  applySearch();
}
/* The rows in the markup are list items in sequence with the headings and notes between them.
   Each unbroken run of rows becomes one list, named by the heading above it (or by the list's
   title where it has none), so a list holds only list items. */
function groupRows(list) {
  let ul = null,
    heading = null,
    n = 0;
  for (const el of [...list.children]) {
    if (el.classList.contains('ri')) {
      if (!ul) {
        ul = document.createElement('ul');
        ul.className = 'rows';
        // an unstyled list keeps its role only when it is stated
        ul.setAttribute('role', 'list');
        if (heading) heading.id ||= `grp-${++n}`;
        ul.setAttribute('aria-labelledby', heading ? heading.id : 'list-title');
        el.before(ul);
      }
      ul.append(el);
    } else {
      ul = null;
      if (el.classList.contains('grp')) heading = el;
      else if (!el.classList.contains('grp-note')) heading = null;
    }
  }
}
function certaintyGlyph(s) {
  return s === 'verified' ? tick : s === 'reported' ? ring : dash;
}
function invoiceGlyph(s) {
  return (
    { draft: ring, issued: g('submitted'), partially_paid: g('progress'), paid: g('verified') }[
      s
    ] || dash
  );
}
function latestPlan() {
  return objs('financial_plan').at(-1);
}
function moneyRecordedRows() {
  const s = state.snapshot.stats;
  return (
    grp('Recorded cash', '', CALC_NOTE) +
    row({
      key: 'money:recorded',
      glyph: dash,
      title: 'Collected',
      right: money(s.cash_collected),
      fig: true,
      sub: 'Payments reconciled against reviewed receipts',
    }) +
    row({
      key: 'money:outstanding',
      glyph: dash,
      title: 'Outstanding invoices',
      right: money(s.outstanding_invoices),
      fig: true,
      sub: 'Issued and not yet paid',
    })
  );
}
function moneyRows(today) {
  const p = latestPlan();
  let out = '';
  if (p) {
    const sum = p.data.scenarios.base.summary;
    out += grp(today ? 'Money' : 'Latest model · base case', '', CALC_NOTE);
    out += row({
      key: 'money:ending',
      glyph: p.stale ? g('review') : dash,
      title: 'Ending cash',
      right: money(sum.ending_cash),
      fig: true,
      sub: h(
        `${p.title} · month ${p.data.inputs.months}, base case${p.stale ? ' · needs review' : ''}`,
      ),
    });
    out += row({
      key: 'money:needed',
      glyph: decimalSign(sum.additional_cash_needed) > 0 ? g('review') : dash,
      title: 'Additional cash needed',
      right: money(sum.additional_cash_needed),
      fig: true,
      sub: sum.first_shortfall_month
        ? `First shortfall in month ${h(sum.first_shortfall_month)}`
        : 'No shortfall in the base case',
    });
    if (!today)
      out += row({
        key: 'money:lowest',
        glyph: dash,
        title: 'Lowest cash balance',
        right: money(sum.minimum_cash),
        fig: true,
        sub: 'Base case',
      });
  } else if (!today) {
    out += grp('Latest model');
    out += `<p class="empty">No financial model yet. Start with units, price, delivery cost, fixed costs and when customers actually pay. Business Foundry calculates all three statements.</p>`;
  }
  if (!today || objs('invoice').length) out += moneyRecordedRows();
  return out;
}
function changedRows() {
  const out = [];
  for (const d of objs('document').filter((x) => x.stale))
    out.push(
      row({
        key: 'doc:' + d.id,
        glyph: g('review'),
        title: h(d.title),
        sub: 'Draft made from records that changed since',
      }),
    );
  for (const d of objs('deal').filter((x) => x.stale))
    out.push(
      row({
        key: 'deal:' + d.id,
        glyph: g('review'),
        title: h(d.title),
        sub: 'Acquisition analysis · needs review',
      }),
    );
  for (const p of objs('financial_plan').filter((x) => x.stale))
    out.push(
      row({
        key: 'plan:' + p.id,
        glyph: g('review'),
        title: h(p.title),
        sub: 'Forecast · needs review',
      }),
    );
  for (const o of [...objs('invoice'), ...objs('payment')].filter((x) => x.evidence_warning))
    out.push(
      row({
        key: (o.kind === 'invoice' ? 'inv:' : 'pay:') + o.id,
        glyph: g('review'),
        title: h(o.title),
        sub: 'Its supporting evidence is withdrawn or unreviewed',
      }),
    );
  for (const e of objs('evidence').filter((x) => x.state !== 'verified')) out.push(evRow(e));
  return out;
}
/* A line runs unbroken through consecutive rows of the same line (and up into its bullet);
   it stops short where the line changes, so two lines never touch. */
function joinLines() {
  const items = [...document.querySelectorAll('#list .ri, #list .grp')]
    .filter((el) => !el.closest('[hidden]'))
    .map((el) => (el.classList.contains('ri') ? el.firstElementChild : el));
  items.forEach((el, i) => {
    if (!el.classList.contains('row') || !el.dataset.line) return;
    const prev = items[i - 1],
      next = items[i + 1];
    el.classList.toggle('brk', !(prev && prev.dataset.line === el.dataset.line));
    el.classList.toggle(
      'end',
      !(next && next.classList.contains('row') && next.dataset.line === el.dataset.line),
    );
  });
}
function ensureSelection() {
  const rows = [...document.querySelectorAll('#list .row')];
  if (!rows.some((r) => r.dataset.key === state.selected)) {
    const keep = state.selected && selectionExists(state.selected);
    if (!keep) state.selected = rows[0]?.dataset.key || null;
  }
  state.remembered[state.source] = state.selected;
  rows.forEach((r) => r.setAttribute('aria-current', String(r.dataset.key === state.selected)));
}
/* A selection reached by a link (say, from Today to a step) is kept even if this list does not
   show it, as long as the record still exists. */
function selectionExists(key) {
  const [kind, id] = splitKey(key);
  if (['step', 'jr'].includes(kind))
    return M.byId.has(id) || (kind === 'step' && replaced(find(id)));
  if (['ev', 'rec', 'plan', 'deal', 'doc', 'inv', 'pay'].includes(kind)) return Boolean(find(id));
  if (kind === 'money') return Boolean(state.snapshot);
  return false;
}
const splitKey = (key) => {
  const i = String(key || '').indexOf(':');
  return i < 0 ? [key, ''] : [key.slice(0, i), key.slice(i + 1)];
};
function eventTitle(e) {
  return label(String(e.action).replaceAll('.', ' · ').replaceAll('_', ' '));
}

/* ---------------- inspector helpers ---------------- */
const props = (rows) =>
  `<dl class="props">${rows
    .filter(Boolean)
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`)
    .join('')}</dl>`;
const sec = (title, body, note = '') =>
  `<section class="i-sec"><h3>${title}${note}</h3>${body}</section>`;
const head = ({ kicker, title, glyph = '', stateLabel, next, actions = '', extra = '' }) =>
  `<div class="i-kicker">${kicker}</div><h2 class="i-title" id="insp-title" tabindex="-1">${title}</h2>
   ${stateLabel ? `<div class="i-state">${glyph}<span class="st">${stateLabel}</span>${next ? `<span class="nx">${next}</span>` : ''}</div>` : ''}
   ${actions ? `<div class="i-actions">${actions}</div>` : ''}${extra}`;
const btn = (text, attrs = '', primary = false, small = false, extraCls = '') =>
  `<button type="button" class="btn${primary ? ' primary' : ''}${small ? ' small' : ''}${extraCls ? ' ' + extraCls : ''}" ${attrs}>${h(text)}</button>`;
/* A stored address becomes a link only if it is an http or https URL. Anything else, such as a
   javascript: or data: address, is shown as plain text and cannot be followed. */
function webLink(uri) {
  let ok = false;
  try {
    ok = ['http:', 'https:'].includes(new URL(String(uri)).protocol);
  } catch {
    ok = false;
  }
  return ok
    ? `<a href="${h(uri)}" target="_blank" rel="noopener noreferrer">${h(uri)}</a>`
    : `<span class="uri">${h(uri)}</span>`;
}
const goAttr = (src, key) => `data-go="${h(src)}|${h(key)}"`;
const flag = (body, neutral = false) =>
  `<div class="flag${neutral ? ' neutral' : ''}">${neutral ? '<span></span>' : g('review')}<div>${body}</div></div>`;
/* Why a saved deal analysis or forecast needs review, in the server's own words, one flag for
   each reason: evidence it relied on changed, the planning jurisdiction changed, or both. The
   page writes no reason of its own; where a row has no room for these, it says "needs review". */
const staleFlags = (o) => (o.stale_reasons || []).map((r) => flag(h(r.message))).join('');
function goFor(o) {
  if (o.kind === 'task') {
    const s = M.byId.get(o.id);
    return [s ? s.line.id : 'journey', 'step:' + o.id];
  }
  if (o.kind === 'evidence') return ['evidence', 'ev:' + o.id];
  if (o.kind === 'financial_plan') return ['money', 'plan:' + o.id];
  if (o.kind === 'deal') return ['deals', 'deal:' + o.id];
  if (o.kind === 'document') return ['documents', 'doc:' + o.id];
  if (o.kind === 'invoice') return ['sales', 'inv:' + o.id];
  if (o.kind === 'payment') return ['sales', 'pay:' + o.id];
  return ['records', 'rec:' + o.id];
}
function linkTo(o, text) {
  if (!o) return '<span class="amber">Record not found</span>';
  const [s, k] = goFor(o);
  const prefix = o.kind === 'task' && M.byId.get(o.id) ? M.byId.get(o.id).n + ' ' : '';
  return `<button type="button" class="link" ${goAttr(s, k)}>${h(text ?? prefix + o.title)}</button>`;
}
function usedBy(eid) {
  const out = [];
  for (const o of objs()) {
    if (o.id === eid) continue;
    const d = o.data || {};
    const refs = new Set(Array.isArray(d.evidence_ids) ? d.evidence_ids : []);
    for (const k of [
      'contract_evidence_id',
      'acceptance_evidence_id',
      'evidence_id',
      'issue_evidence_id',
    ])
      if (d[k]) refs.add(d[k]);
    if (o.kind === 'deal')
      for (const a of d.inputs?.adjustments || []) if (a.evidence_id) refs.add(a.evidence_id);
    if (refs.has(eid)) out.push(o);
  }
  return out;
}
function objGlyph(o) {
  if (o.kind === 'task') return taskGlyph(o);
  if (o.kind === 'evidence') return evInfo(o).glyph;
  if (o.kind === 'document' || o.kind === 'deal') return o.stale ? g('review') : tick;
  if (o.kind === 'financial_plan') return o.stale ? g('review') : dash;
  if (o.kind === 'invoice') return o.evidence_warning ? g('review') : invoiceGlyph(o.state);
  if (o.kind === 'payment') return o.evidence_warning ? g('review') : tick;
  if (RECORD_KINDS.includes(o.kind)) return certaintyGlyph(o.state);
  return dash;
}
function objState(o) {
  if (o.kind === 'task') return stateOf(o).label;
  if (o.kind === 'evidence') return evInfo(o).label;
  // a deal analysis or forecast can need review for more than one reason; its own view says which
  if (o.stale)
    return o.kind === 'document'
      ? 'Needs review — made from records that changed since'
      : 'Needs review';
  if (o.evidence_warning) return `${label(o.state)} · its evidence needs review`;
  return label(o.state);
}
function attList(items) {
  if (!items.length) return '<p class="fine">None.</p>';
  return `<div class="att">${items
    .map((o) => {
      const [s, k] = goFor(o);
      const amber =
        (o.kind === 'evidence' && o.state === 'rejected') ||
        o.stale ||
        o.evidence_warning ||
        o.state === 'needs_review';
      const prefix = o.kind === 'task' && M.byId.get(o.id) ? M.byId.get(o.id).n + ' ' : '';
      return `<div class="att-row">${objGlyph(o)}<span class="an">${h(prefix + o.title)}<span class="sub${amber ? ' amber' : ''}">${h(label(o.kind === 'financial_plan' ? 'financial model' : o.kind))} · ${h(objState(o))}</span></span>${btn('Open', goAttr(s, k))}</div>`;
    })
    .join('')}</div>`;
}

/* ---------------- inspector ---------------- */
function renderInspector() {
  const el = $('#insp');
  const [kind, id] = splitKey(state.selected);
  const crumb = $('#crumb');
  let out = '',
    wide = false,
    crumbText = '';
  try {
    if (!state.snapshot) {
      out = inspStart();
      crumbText = 'Getting started';
    } else if (kind === 'step' && M.byId.has(id)) {
      const s = M.byId.get(id);
      out = inspStep(s);
      crumbText = `${s.line.name} › ${s.n} ${s.task.title}`;
    } else if (kind === 'step' && replaced(find(id))) {
      out = inspReplaced(find(id));
      crumbText = `Replaced step › ${find(id).title}`;
    } else if (kind === 'jr' || kind === 'map') {
      const s = kind === 'jr' ? M.byId.get(id) : null;
      out = inspJourney(s);
      wide = true;
      crumbText = s ? `Journey › ${s.n} ${s.task.title}` : 'Journey';
    } else if (kind === 'ev' && find(id)) {
      out = inspEvidence(find(id));
      crumbText = `Evidence › ${find(id).title}`;
    } else if (kind === 'rec' && find(id)) {
      out = inspRecord(find(id));
      crumbText = `Business records › ${find(id).title}`;
    } else if (kind === 'money') {
      out = inspMoney(id);
      crumbText = 'Money';
    } else if (kind === 'plan' && find(id)) {
      out = inspPlan(find(id));
      crumbText = `Money › ${find(id).title}`;
    } else if (kind === 'deal' && find(id)) {
      out = inspDeal(find(id));
      crumbText = `Acquisition analyses › ${find(id).title}`;
    } else if (kind === 'doc' && find(id)) {
      out = inspDocument(find(id));
      crumbText = `Documents › ${find(id).title}`;
    } else if (kind === 'inv' && find(id)) {
      out = inspInvoice(find(id));
      crumbText = `Invoices › ${find(id).title}`;
    } else if (kind === 'pay' && find(id)) {
      out = inspPayment(find(id));
      crumbText = `Payments › ${find(id).title}`;
    } else if (kind === 'integrity' || kind === 'evt') {
      out = inspHistory(kind === 'evt' ? id : null);
      crumbText = 'History';
    } else if (kind === 'ai' || kind === 'key' || kind === 'run') {
      out = inspAccess(kind, id);
      crumbText = 'Access & assistants';
    } else {
      out = `<p class="empty">Choose an item in the list to see it here.</p>`;
    }
  } catch (error) {
    out = `<p class="empty">This item could not be shown. ${h(messageFor(error))}</p>`;
  }
  el.innerHTML = `<div class="i-wrap${wide ? ' wide' : ''}">${out}</div>`;
  crumb.textContent = crumbText;
  const src = sourceById(state.source);
  $('#back-list-lbl').textContent = state.source === 'journey' ? 'Stops' : src?.name || 'Today';
  fitMaps();
  if (kind === 'ev' && find(id)) fillVersions(id);
  if (kind === 'integrity' || kind === 'evt') fillHistory(kind === 'evt' ? id : null);
}

function inspStart() {
  const owner = isOwner();
  return (
    head({
      kicker: 'Getting started',
      title: state.businesses.length ? 'Choose a business' : 'Start your first business',
      actions: owner ? btn('Create a business', 'data-action="new-business"', true) : '',
    }) +
    `<p class="prose spaced">${
      owner
        ? 'A business here is a workspace for one idea or one acquisition. Creating it lays out its steps from the planning catalog, grouped into lines: market, finance, setup, rights, delivery, sales to cash and operations. Each line runs to the same interchange, Open for business.'
        : 'This access key does not cover any business yet. Ask the workspace owner to give it access to one.'
    }</p>` +
    `<figure class="mapbox" data-fit="tall">${mapEmpty('mp-tall')}</figure>` +
    sec(
      'How a step moves',
      `<p class="prose">Every step is prepared, then approved by a person, then, if an outside body is involved, submitted by you, and finally verified once the result is on record. Each stage is recorded separately with its evidence, and only a person can approve, submit or verify.</p><div class="spaced">${keyHTML().replace('<div class="key-h">Key</div>', '')}</div>`,
    ) +
    sec(
      'To look around first',
      `<p class="prose">Run <code>uv run foundry demo</code> to add an explicitly synthetic example business beside your own, then switch between businesses at the top. Its figures and receipts are fictional.</p>`,
    )
  );
}

function transitionButtons(t) {
  if (!canWrite()) return '';
  const fwd = forward(t);
  return choices(t)
    .map((to) =>
      btn(moveLabel(to, t), `data-action="tx" data-id="${h(t.id)}" data-to="${to}"`, to === fwd),
    )
    .join('');
}
/* The form carries the business and the version of the step it is drawn from; that is what it
   sends, whatever the records in memory say by the time it is submitted. */
function txForm(t) {
  const to = state.open.to;
  const opts = choices(t);
  return `<form class="confirm" id="tx-form" data-id="${h(t.id)}" data-bid="${h(state.bid)}" data-version="${h(t.version)}">
    <p id="tx-text">${h(moveText(to, t))}</p>
    <div class="form-grid one spaced">${choose(
      'state',
      'Next state',
      opts.map((s) => [s, STATES[s].label]),
      to,
    )}${evidenceSelect('evidence_ids', 'Evidence for this decision', t.data.evidence_ids || [])}${area('note', 'Decision note', '', 'Explain what was done, reviewed or found not applicable.')}</div>
    <p class="error spaced" role="alert"></p>
    <div class="i-actions"><button type="submit" class="btn primary">Record decision</button>${btn('Not yet', 'data-action="close-inline"')}</div></form>`;
}
/* A decision the server accepted whose reload failed: the step still shows its earlier state, so
   in place of its moves it says the decision is recorded, and nothing offers to send it again. */
const acceptedNote = (to) =>
  `<div class="flag" data-accepted><span></span><div>Your decision to move this step to ${h(STATES[to]?.label || label(to))} is recorded. This page has not loaded it yet; load the current records to continue.<div class="i-actions">${btn('Load current records', 'data-action="reload"')}</div></div></div>`;
function stepFlags(s) {
  const t = s.task;
  let out = '';
  if (t.state === 'needs_review') {
    const r = reopened(t);
    out += flag(
      `<b>This step was reopened.</b> ${h(r?.note || 'Its inputs changed.')}${r?.at ? ` · ${h(dDay(r.at))}` : ''}${r?.actor_id ? `, recorded by ${h(who(r.actor_id))}` : ''}. Earlier decisions stay in its history; prepare it again from the current records.`,
    );
  }
  const withdrawn = (t.data.evidence_ids || [])
    .map(find)
    .filter((e) => e && e.state === 'rejected');
  if (withdrawn.length)
    out += flag(
      `<b>Evidence this step cites has been withdrawn:</b> ${withdrawn.map((e) => h(e.title)).join(', ')}.`,
    );
  if (!FINISHED.has(t.state) && t.blockers?.length)
    out += flag(
      `<b>Before it can be approved, submitted or verified:</b><ul>${t.blockers.map((b) => `<li>${h(b)}</li>`).join('')}</ul>`,
      true,
    );
  return out;
}
const STAGE_VERB = {
  prepared: 'Prepared',
  approved: 'Approved',
  submitted: 'Submitted',
  verified: 'Verified',
};
const RANK = { prepared: 1, approved: 2, submitted: 3, verified: 4 };
function stageGuide(st, t) {
  if (st === 'prepared')
    return [
      canWrite() ? 'Prepare it' : 'Waiting to be prepared',
      'Attach evidence and explain what was done.',
    ];
  if (st === 'approved')
    return [
      canReview() ? 'Waiting on you to approve' : 'Waiting on an owner or reviewer to approve',
      'Approval needs reviewed evidence and every prerequisite complete.',
    ];
  if (st === 'submitted')
    return [
      'Submit it yourself',
      'Send it to the outside body, then record it here with a reviewed external receipt. Business Foundry cannot submit for you.',
    ];
  return t.data.external
    ? ['Add the outside body’s receipt', 'Record their reviewed receipt when it arrives.']
    : ['Verify the result', 'A person checks the result and records it.'];
}
function ladder(rows) {
  return `<div class="ladder">${rows
    .map(
      (
        r,
      ) => `<div class="lad${r.off ? ' off' : ''}${r.next ? ' next' : ''}" data-stage="${r.s}" data-reached="${r.off ? 'no' : 'yes'}">
    <span class="lg">${r.dash ? dash : g(r.s, r.off)}</span><span class="ln">${r.name}</span>${r.date ? `<span class="ld">${h(r.date)}</span>` : ''}<span class="lw">${r.what || ''}</span></div>`,
    )
    .join('')}</div>`;
}
function stageLadder(t) {
  const hist = taskHistory(t);
  const cut = lastIndex(hist, (e) => e && e.to === 'needs_review');
  const cycle = hist.slice(cut + 1),
    before = cut >= 0 ? hist.slice(0, cut) : [];
  const lastTo = (list, st) => [...list].reverse().find((e) => e && e.to === st);
  const current = { prepared: 1, approved: 2, submitted: 3, verified: 4 }[t.state] || 0;
  let marked = false;
  const rows = ['prepared', 'approved', 'submitted', 'verified'].map((st) => {
    if (st === 'submitted' && !t.data.external)
      return {
        s: st,
        name: 'Submitted',
        off: true,
        dash: true,
        what: 'Not needed<span class="sub">An internal review has no outside submission.</span>',
      };
    if (current >= RANK[st]) {
      const e = lastTo(cycle, st);
      return {
        s: st,
        name: STAGE_VERB[st],
        date: e ? dDay(e.at) : '',
        what: e
          ? `${STAGE_VERB[st]} by ${h(who(e.actor_id))}${e.note ? `<span class="sub">${h(e.note)}</span>` : ''}`
          : 'Recorded<span class="sub">The history has no entry for this stage.</span>',
      };
    }
    const earlier = lastTo(before, st);
    const was = earlier
      ? `<span class="sub">Earlier: ${STAGE_VERB[st].toLowerCase()} ${h(dDay(earlier.at))}, before the step was reopened</span>`
      : '';
    if (!marked && t.state !== 'not_applicable') {
      marked = true;
      const [main, sub] = stageGuide(st, t);
      return {
        s: st,
        name: STAGE_VERB[st],
        off: true,
        next: true,
        what: `<b>${h(main)}</b><span class="sub">${h(sub)}</span>${was}`,
      };
    }
    return { s: st, name: STAGE_VERB[st], off: true, what: 'Not yet' + was };
  });
  let note = '';
  if (t.state === 'not_applicable') {
    const e = lastTo(hist, 'not_applicable');
    note = `<p class="fine">Reviewed as not applicable${e ? ` on ${h(dFull(e.at))} by ${h(who(e.actor_id))}: ${h(e.note)}` : ''}. Reopen it if the facts change.</p>`;
  }
  return ladder(rows) + note;
}
function journeySec(s) {
  const l = s.line,
    st = l.steps,
    i = s.index;
  const ahead = st.slice(i + 1);
  const items = ahead.map((x) => `${x.n} ${x.task.title} (${stateOf(x.task).label.toLowerCase()})`);
  const between = !ahead.length
    ? `It is the last stop on the ${l.name} line before Open for business.`
    : `Between this step and Open for business: ${items.length > 1 ? items.slice(0, -1).join(', ') + ' and ' + items.at(-1) : items[0]}.`;
  const body = `<div class="frag">${mapFrag(s)}<p>Stop ${i + 1} of ${st.length} on the ${h(l.name)} line. ${h(between)} Across all ${plural(M.lines.length, 'line')}, ${M.toGo} ${M.toGo === 1 ? 'stop is' : 'stops are'} still to go.</p>
    <div class="i-actions">${btn('See the whole journey', goAttr('journey', 'jr:' + s.id))}</div></div>`;
  return sec('On the journey', body, `<span class="h-note">${h(l.name)} line</span>`);
}
function inspStep(s) {
  const t = s.task,
    l = s.line,
    ext = t.data.external;
  const kicker = `${bul(l, true)}<span>Step ${h(s.n)} of ${M.steps.length} · ${h(l.name)} · ${ext ? 'outside body' : 'internal review'}</span>`;
  const accepted = state.accepted[t.id];
  const open = state.open?.kind === 'tx' && state.open.id === t.id ? txForm(t) : '';
  // linked steps are live steps only; a replaced prerequisite blocks nothing (the server agrees)
  const deps = (t.data.depends_on || []).map(find).filter((x) => x && !replaced(x));
  const needed = objs('task').filter(
    (x) => !replaced(x) && (x.data.depends_on || []).includes(t.id),
  );
  const evidence = (t.data.evidence_ids || []).map(
    (id) =>
      find(id) || {
        id,
        kind: 'evidence',
        title: 'Evidence not found',
        state: 'rejected',
        data: {},
      },
  );
  const advice = adviceFor(t.id);
  return (
    head({
      kicker,
      title: h(t.title),
      glyph: taskGlyph(t),
      stateLabel: stateOf(t).label,
      next: h(nextText(s)),
      actions: accepted ? '' : transitionButtons(t),
      extra: (accepted ? acceptedNote(accepted) : open) + stepFlags(s),
    }) +
    sec('Stages', stageLadder(t)) +
    journeySec(s) +
    sec(
      'What this is',
      props([
        ['What it is', h(t.data.description)],
        [
          'Who acts',
          ext
            ? 'An outside body. You send it yourself and record their receipt; Business Foundry does not submit anything.'
            : 'Internal. A person reviews and verifies it.',
        ],
        ['Records needed', h((t.data.required_record_kinds || []).map(label).join(', ') || 'None')],
        ['Owner', h(t.data.owner || '—')],
        ['Applicability', h(label(t.data.applicability || 'requires_review'))],
        t.data.source_uri
          ? [
              'Planning guide',
              `${webLink(t.data.source_uri)}<span class="sub">Guide ${h(t.data.pack_version)}${t.data.source_checked_on ? `, checked ${h(plainDate(t.data.source_checked_on))}` : ''}. Planning guidance, not a compliance certificate.</span>`,
            ]
          : null,
        l.deal ? ['Part of', linkTo(l.deal)] : null,
      ]),
    ) +
    sec('Evidence it cites', attList(evidence), `<span class="h-note">${evidence.length}</span>`) +
    (deps.length || needed.length
      ? sec(
          'Linked steps',
          props([
            deps.length
              ? ['Needs these first', deps.map((d) => `${taskGlyph(d)} ${linkTo(d)}`).join('<br>')]
              : null,
            needed.length
              ? ['Needed by', needed.map((d) => `${taskGlyph(d)} ${linkTo(d)}`).join('<br>')]
              : null,
          ]),
        )
      : '') +
    advice +
    historySec(t)
  );
}
/* Every decision recorded on a step, newest first. */
function historySec(t) {
  const hist = taskHistory(t).slice().reverse();
  return sec(
    'History',
    hist.length
      ? `<div class="att">${hist
          .map(
            (e) =>
              `<div class="att-row">${taskGlyph({ state: e.to })}<span class="an">${h(stateOf({ state: e.from }).label)} → ${h(stateOf({ state: e.to }).label)}<span class="sub">${h(dateTime(e.at))} · ${h(who(e.actor_id))}${e.note ? ' · ' + h(e.note) : ''}</span></span><span></span></div>`,
          )
          .join('')}</div>`
      : '<p class="fine">No decisions recorded yet.</p>',
  );
}
/* A step replaced when the planning jurisdiction changed, reached by a link from a record that
   cites it. It is read-only: it says what happened, shows what it was and its history, and offers
   no decision, because the server allows none. */
function inspReplaced(t) {
  const change = taskHistory(t).findLast((e) => e && e.to === 'superseded');
  const from = change?.jurisdiction_from,
    to = change?.jurisdiction_to;
  const evidence = (t.data.evidence_ids || []).map(find).filter(Boolean);
  return (
    head({
      kicker: 'Step from an earlier planning guide',
      title: h(t.title),
      glyph: dash,
      stateLabel: REPLACED.label,
      next: change?.at ? `on ${h(dFull(change.at))}` : '',
      extra: flag(
        `<b>This step was replaced when the planning jurisdiction changed${from && to ? ` from ${h(from)} to ${h(to)}` : ''}.</b> It belongs to the earlier planning guide and is kept as history. It is on no line, it is not counted, and no decision can be recorded on it.`,
        true,
      ),
    }) +
    sec(
      'What this was',
      props([
        ['What it was', h(t.data.description || '—')],
        t.data.source_uri
          ? [
              'Planning guide',
              `${webLink(t.data.source_uri)}<span class="sub">Guide ${h(t.data.pack_version || '—')}. Planning guidance, not a compliance certificate.</span>`,
            ]
          : null,
      ]),
    ) +
    sec('Evidence it cites', attList(evidence), `<span class="h-note">${evidence.length}</span>`) +
    historySec(t)
  );
}
function adviceFor(id) {
  const notes = [];
  for (const r of state.runs) {
    if (r.state !== 'succeeded' || !r.data.advice) continue;
    for (const rec of r.data.advice.recommendations || [])
      if ((rec.evidence_ids || []).includes(id)) notes.push({ r, rec });
  }
  if (!notes.length) return '';
  return sec(
    'Assistant’s note',
    notes
      .map(
        ({ r, rec }) =>
          `<div class="note-box spaced">${props([
            [
              'From',
              `${h(r.data.request?.model || 'model not recorded')}, ${h(dDay(r.created_at))}${r.data.source_fingerprint !== state.snapshot.source_fingerprint ? '<span class="sub amber">Given on an earlier state of the records</span>' : ''}`,
            ],
            ['Suggests', `${h(rec.action)}<span class="sub">${h(rec.reason)}</span>`],
            ['What it changes', 'Nothing by itself. Only a person can approve, submit or verify.'],
          ])}</div>`,
      )
      .join(''),
    '<span class="h-tag">Advice only</span>',
  );
}

function inspJourney(s) {
  let bar;
  if (s) {
    const ls = longState(s);
    bar = `<div class="selbar">${taskGlyph(s.task)}<div><b>${h(s.n)} ${h(s.task.title)}</b><span class="sub">${h(s.line.name)} line, stop ${s.index + 1} of ${s.line.steps.length} · ${h(capFirst(ls))}</span></div>${btn('Open step ' + s.n, goAttr(s.line.id, 'step:' + s.id), true)}</div>`;
  } else {
    bar = `<div class="selbar none">${M.steps.length ? 'Choose a stop on the map, or in the list of stops, to see where it stands.' : 'This business has no steps yet.'}</div>`;
  }
  const byLine = M.lines.map((l) => {
    const c = lineStats(l);
    const left = l.steps.filter((x) => !FINISHED.has(x.task.state));
    const say = (x) => {
      const mv = yourMove(x.task);
      return `${x.n} ${x.task.title} — ${mv ? 'your move: ' + mv : stateOf(x.task).label.toLowerCase()}`;
    };
    return [
      `<span class="dt-l">${bul(l, true)}${h(l.name)}</span>`,
      `${c.verified} of ${c.total} verified<span class="sub">${left.length ? 'Still to go: ' + h(left.map(say).join('; ')) : 'Nothing left to go'}</span>`,
    ];
  });
  const b = state.snapshot.business.data;
  return (
    head({
      kicker: `Journey · ${h(b.name)}`,
      title: h(toGoText()),
      stateLabel: `${M.verified} of ${plural(M.steps.length, 'step')} verified`,
      next: M.na ? `${M.na} reviewed as not applicable` : '',
      actions: canWrite() ? btn('Edit business', 'data-action="edit-business"') : '',
    }) +
    bar +
    `<figure class="mapbox"><div class="map-scroll">${mapWide(s?.id)}</div>${mapTall(s?.id)}
        <figcaption class="fine">Each line is one group of steps from the planning catalog, in catalog order, meeting at Open for business. A stop shows as verified only once a person has recorded its result with reviewed evidence. Every stop is also in the list of stops, and line by line below.</figcaption></figure>` +
    sec('Line by line', byLine.length ? props(byLine) : '<p class="fine">No lines yet.</p>') +
    sec(
      'About this business',
      props([
        ['Concept', h(b.concept)],
        ['Customer', h(b.customer || '—')],
        ['Accountable', h(b.owner || '—')],
        ['Entry path', b.route === 'acquire' ? 'Acquire a business' : 'Start a business'],
        [
          'Jurisdiction',
          h(b.jurisdiction === 'undecided' ? 'Undecided — review needed' : b.jurisdiction),
        ],
        ['Currency', h(b.currency)],
        b.is_demo
          ? ['Data', 'Synthetic demonstration. No real legal or commercial action has occurred.']
          : null,
      ]),
    )
  );
}

function inspEvidence(o) {
  const info = evInfo(o),
    d = o.data;
  let actions = '';
  if (canReview()) {
    actions += btn(
      'Record human review',
      `data-action="review-evidence" data-id="${h(o.id)}"`,
      o.state === 'unverified',
    );
    if (o.state === 'verified')
      actions += btn(
        'Withdraw verification',
        `data-action="revoke-evidence" data-id="${h(o.id)}"`,
        false,
        false,
        'danger',
      );
  }
  let flags = '';
  if (o.state === 'rejected')
    flags += flag(
      `<b>This evidence was withdrawn</b>${d.review ? ` by ${h(d.review.actor_name)} on ${h(dFull(d.review.at))}: ${h(d.review.note)}` : ''}. Completed steps and verified records that relied on it were reopened. The original text below is unchanged.`,
    );
  else if (o.state === 'unverified')
    flags += flag(
      '<b>Not yet reviewed.</b> A person must review it before it can support an approval, submission or verification.',
      true,
    );
  const cited = usedBy(o.id),
    used = cited.filter((x) => !replaced(x)),
    history = cited.filter(replaced);
  return (
    head({
      kicker: 'Evidence',
      title: h(o.title),
      glyph: info.glyph,
      stateLabel: info.label,
      next: `version ${o.version}`,
      actions,
      extra: flags,
    }) +
    sec(
      'Details',
      props([
        ['Source type', h(label(d.source_type))],
        ['Effective date', h(d.effective_date ? plainDate(d.effective_date) : 'Not supplied')],
        ['Added', h(dFull(o.created_at))],
        d.source_uri
          ? [
              'Source reference',
              `${webLink(d.source_uri)}<span class="sub">A reference only; the server does not fetch it.</span>`,
            ]
          : null,
        d.review
          ? [
              o.state === 'rejected' ? 'Withdrawn by' : 'Reviewed by',
              `${h(d.review.actor_name)}, ${h(dFull(d.review.at))}<span class="sub">${h(d.review.note)}</span>`,
            ]
          : ['Review', 'None recorded'],
      ]),
    ) +
    sec(
      'Original',
      `<pre class="original">${h(d.content)}</pre><p class="fine">Stored as received. Business Foundry never edits an original; a correction is new evidence. Fingerprint <code>${h(d.content_sha256)}</code></p>`,
    ) +
    sec('Used by', attList(used), `<span class="h-note">${used.length}</span>`) +
    (history.length
      ? sec(
          'Cited by replaced steps',
          attList(history),
          `<span class="h-note">${history.length}</span>`,
        )
      : '') +
    sec(
      'Versions',
      `<div data-versions="${h(o.id)}"><p class="fine">Loading the recorded versions…</p></div>`,
    )
  );
}
/* The history for the records on screen. It is cached against the load that requested it, so a
   request made before a save cannot fill the cache after the save's refresh has started anew. */
function loadAudit() {
  const load = state.loaded,
    bid = state.bid;
  if (state.audit?.load === load) return Promise.resolve(state.audit.data);
  if (state.auditPromise?.load !== load) {
    const p = api(`/businesses/${bid}/audit`).then((data) => {
      if (state.loaded === load) state.audit = { load, data };
      return data;
    });
    p.load = load;
    state.auditPromise = p;
    p.catch(() => {
      if (state.auditPromise === p) state.auditPromise = null;
    });
  }
  return state.auditPromise;
}
/* Fill a placeholder the inspector just drew, once the history arrives, but only while that same
   placeholder is on screen: the same item, from the same load. Any re-render replaces it. */
function fillFromAudit(el, fill, failed) {
  const load = state.loaded,
    key = state.selected;
  const current = () => el.isConnected && state.loaded === load && state.selected === key;
  loadAudit().then(
    (a) => {
      if (current()) fill(a);
    },
    (e) => {
      if (current()) failed(e);
    },
  );
}
function fillVersions(oid) {
  const el = document.querySelector(`#insp [data-versions="${CSS.escape(oid)}"]`);
  if (!el) return;
  fillFromAudit(
    el,
    (a) => {
      const evs = a.events.filter(
        (e) => e.object_id === oid && e.payload && 'version' in e.payload,
      );
      el.innerHTML = evs.length
        ? `<div class="att">${evs
            .slice()
            .reverse()
            .map((e) => {
              const p = e.payload;
              const st =
                p.state === 'rejected'
                  ? 'Withdrawn'
                  : p.state === 'verified'
                    ? 'Reviewed'
                    : 'Not yet reviewed';
              const note =
                p.data?.review && p.version > 1
                  ? ` · ${p.data.review.actor_name}: ${p.data.review.note}`
                  : '';
              return `<div class="att-row">${p.state === 'rejected' ? g('review') : p.state === 'verified' ? tick : ring}<span class="an">Version ${h(p.version)} — ${h(st)}<span class="sub">${h(dateTime(e.created_at))} · ${h(eventTitle(e))}${h(note)}</span></span><span></span></div>`;
            })
            .join(
              '',
            )}</div><p class="fine">Each version is kept in the history. The original text is the same in every version.</p>`
        : '<p class="fine">No versions recorded.</p>';
    },
    (e) => {
      el.innerHTML = `<p class="fine">The history could not be loaded: ${h(messageFor(e))}</p>`;
    },
  );
}

function inspRecord(o) {
  const d = o.data;
  const evidence = (d.evidence_ids || []).map(find).filter(Boolean);
  const related = (d.related_ids || []).map(find).filter(Boolean);
  return (
    head({
      kicker: `Business record · ${h(label(o.kind))}`,
      title: h(o.title),
      glyph: certaintyGlyph(o.state),
      stateLabel: h(label(o.state)),
      next: `version ${o.version}`,
      actions: btn(canWrite() ? 'Edit' : 'View', `data-action="record" data-id="${h(o.id)}"`),
    }) +
    sec('Details', `<p class="prose">${h(d.details || 'No details recorded.')}</p>`) +
    sec(
      'Record',
      props([
        ['Evidence status', h(label(o.state))],
        ['Responsible', h(d.owner || '—')],
        ['Due', h(d.due_date ? plainDate(d.due_date) : 'None')],
        ['Updated', h(dFull(o.updated_at))],
      ]),
    ) +
    sec('Evidence', attList(evidence), `<span class="h-note">${evidence.length}</span>`) +
    (related.length ? sec('Related records', attList(related)) : '')
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
function chart(scenarios) {
  const all = Object.values(scenarios).flatMap((s) => s.months.map((m) => Number(m.cash)));
  const low = Math.min(0, ...all),
    high = Math.max(1, ...all),
    range = high - low || 1,
    width = 680,
    height = 210,
    left = 55,
    right = 12,
    top = 12,
    bottom = 26;
  const y = (v) => top + ((high - v) / range) * (height - top - bottom);
  const n = scenarios.base.months.length;
  let svg = `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Chart of projected closing cash for the base, downside and upside cases. The chart is drawn approximately; the exact monthly figures are in the cash flow table.">`;
  for (let i = 0; i < 4; i++) {
    const v = low + (range * i) / 3;
    svg += `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${f(y(v))}" y2="${f(y(v))}"/><text x="${left - 8}" y="${f(y(v) + 3)}" text-anchor="end">${h(shortMoney(v))}</text>`;
  }
  if (low < 0)
    svg += `<line class="zero" x1="${left}" x2="${width - right}" y1="${f(y(0))}" y2="${f(y(0))}"/>`;
  for (const name of ['upside', 'downside', 'base']) {
    const s = scenarios[name];
    if (!s) continue;
    const points = s.months
      .map(
        (m, i) =>
          `${f(left + (i / Math.max(n - 1, 1)) * (width - left - right))},${f(y(Number(m.cash)))}`,
      )
      .join(' ');
    svg += `<polyline class="s-${name}" points="${points}" fill="none" stroke-width="${name === 'base' ? 2.5 : 1.8}" ${name === 'downside' ? 'stroke-dasharray="5 4"' : ''}/>`;
  }
  svg += `<text x="${left}" y="${height - 6}">Month 1</text><text x="${width - right}" y="${height - 6}" text-anchor="end">Month ${n}</text></svg>`;
  return (
    svg +
    '<div class="legend" aria-hidden="true"><span><i></i>Base</span><span><i class="l-downside"></i>Downside</span><span><i class="l-upside"></i>Upside</span></div>' +
    '<p class="fine">The chart is drawn approximately and its axis labels are rounded; the exact figures are in the tables.</p>'
  );
}
function inspMoney() {
  const p = latestPlan(),
    s = state.snapshot.stats;
  let out = head({
    kicker: 'Money',
    title: 'Money position',
    stateLabel: p ? h(p.title) : 'No model yet',
    next: p ? `${plural(objs('financial_plan').length, 'version')}` : '',
    actions: p
      ? btn('Open the model', goAttr('money', 'plan:' + p.id), true)
      : canWrite()
        ? btn('Build a financial model', 'data-action="new-forecast"', true)
        : '',
  });
  out += flag(
    `<b>${CALC_NOTE}</b> Forecasts are assumptions; recorded cash comes only from reconciled receipts.`,
    true,
  );
  if (p) {
    const cases = ['base', 'downside', 'upside'].filter((c) => p.data.scenarios[c]);
    out += sec(
      'Latest model',
      `${staleFlags(p)}<div class="tbl-wrap"><table><thead><tr><th>Figure</th>${cases.map((c) => `<th class="num">${label(c)}</th>`).join('')}</tr></thead><tbody>
        <tr><td>Ending cash</td>${cases.map((c) => `<td class="num">${money(p.data.scenarios[c].summary.ending_cash)}</td>`).join('')}</tr>
        <tr><td>Additional cash needed</td>${cases.map((c) => `<td class="num">${money(p.data.scenarios[c].summary.additional_cash_needed)}</td>`).join('')}</tr>
        <tr><td>Lowest cash</td>${cases.map((c) => `<td class="num">${money(p.data.scenarios[c].summary.minimum_cash)}</td>`).join('')}</tr>
        <tr><td>First shortfall</td>${cases.map((c) => `<td class="num">${p.data.scenarios[c].summary.first_shortfall_month ? 'Month ' + h(p.data.scenarios[c].summary.first_shortfall_month) : 'None'}</td>`).join('')}</tr>
        <tr><td>Total revenue</td>${cases.map((c) => `<td class="num">${money(p.data.scenarios[c].summary.total_revenue)}</td>`).join('')}</tr>
      </tbody></table></div><p class="fine">${h(p.title)}, saved ${h(dFull(p.created_at))}, over ${h(p.data.inputs.months)} months.</p>`,
    );
  }
  out += sec(
    'Recorded cash',
    `<div class="figs"><div>Collected</div><div class="v">${money(s.cash_collected)}</div><div>Outstanding invoices</div><div class="v">${money(s.outstanding_invoices)}</div></div><p class="fine">From reconciled payments and issued invoices. An invoice draft is not counted.</p>`,
  );
  return out;
}
function inspPlan(p) {
  const plans = objs('financial_plan');
  const scen = p.data.scenarios[state.scenario] ? state.scenario : 'base';
  const s = p.data.scenarios[scen],
    sum = s.summary;
  const latest = p === plans.at(-1);
  let out = head({
    kicker: `Money · financial model${latest ? ' · latest' : ' · earlier version'}`,
    title: h(p.title),
    extra: staleFlags(p),
    stateLabel: sum.statements_balance ? 'Statements balance' : 'Balance check failed',
    next: `saved ${h(dFull(p.created_at))}`,
    actions: ['base', 'downside', 'upside']
      .filter((c) => p.data.scenarios[c])
      .map((c) =>
        btn(
          label(c),
          `data-action="scenario" data-id="${c}" aria-pressed="${c === scen}"`,
          false,
          true,
          c === scen ? 'on' : '',
        ),
      )
      .join(''),
  });
  out += `<div class="flag neutral"><span></span><div>${sum.statements_balance ? 'All three statements balance.' : 'Statement balance check failed.'} ${h(p.title)} · ${h(currency())} · ${h(p.data.inputs.months)} months · ${plural(plans.length, 'saved version')}. ${CALC_NOTE}</div></div>`;
  out += sec(
    `Summary · ${label(scen)} case`,
    `<div class="figs"><div>Ending cash</div><div class="v">${money(sum.ending_cash)}</div><div>Additional cash needed</div><div class="v">${money(sum.additional_cash_needed)}</div><div>First cash shortfall</div><div class="v">${sum.first_shortfall_month ? 'Month ' + h(sum.first_shortfall_month) : 'None modeled'}</div><div>Lowest cash</div><div class="v">${money(sum.minimum_cash)}</div><div>Total net income</div><div class="v">${money(sum.total_net_income)}</div></div>
     <p class="fine">${h(p.data.scenario_definitions?.[scen] || '')}. Negative cash is an unfunded shortfall; the model does not silently borrow to cover it.</p>`,
  );
  out += sec('Cash over time', chart(p.data.scenarios));
  for (const [statement, fields] of Object.entries(statementFields))
    out += sec(
      `${statement === 'income' ? 'Income statement' : statement === 'cash' ? 'Cash flow' : 'Balance sheet'} · ${label(scen)}`,
      `<div class="tbl-wrap statement"><table><thead><tr><th>Month</th>${fields.map((x) => `<th class="num">${x[1]}</th>`).join('')}</tr></thead><tbody>${s.months.map((m) => `<tr><td>${h(m.month)}</td>${fields.map((x) => `<td class="num">${money(m[x[0]])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`,
    );
  out += sec(
    'Discounted cash flow',
    sum.dcf
      ? `<div class="figs"><div>Enterprise value</div><div class="v">${money(sum.dcf.enterprise_value)}</div><div>Equity value</div><div class="v">${money(sum.dcf.equity_value)}</div><div>Value from terminal assumption</div><div class="v">${percent(sum.dcf.terminal_share)}</div></div><p class="fine">${h(sum.dcf.basis)}. A scenario value, not a market price.</p>`
      : '<p class="fine">Requires at least 12 months and positive terminal cash flow.</p>',
  );
  out += sec(
    'What the model assumes',
    `${(p.data.assumptions || []).map((a) => `<p class="fine">${h(a)}</p>`).join('')}<details class="spaced"><summary>Input assumptions</summary><pre>${h(JSON.stringify(p.data.inputs, null, 2))}</pre></details>`,
  );
  const evidence = (p.data.evidence_ids || []).map(find).filter(Boolean);
  if (evidence.length) out += sec('Evidence', attList(evidence));
  return out;
}
function inspDeal(o) {
  const a = o.data.analysis;
  const line = M.lines.find((l) => l.dealId === o.id);
  let out = head({
    kicker: 'Acquisition analysis',
    title: h(o.title),
    glyph: o.stale ? g('review') : tick,
    stateLabel: o.stale ? 'Needs review' : 'Screening',
    next: `saved ${h(dFull(o.created_at))}`,
    actions:
      (line ? btn('Review diligence work', goAttr(line.id, 'step:' + line.steps[0]?.id)) : '') +
      (canWrite()
        ? btn('Create investment memo', `data-action="investment-memo" data-id="${h(o.id)}"`)
        : '') +
      (canWrite() ? btn('Revise assumptions', `data-action="new-deal" data-id="${h(o.id)}"`) : ''),
    extra: staleFlags(o),
  });
  out += `<p class="prose spaced">${h(o.data.inputs.thesis)}</p>`;
  out += sec(
    'Key figures',
    `<div class="figs"><div>Normalized EBITDA</div><div class="v">${money(a.normalized_ebitda)}</div><div>Assumed enterprise value range</div><div class="v">${a.enterprise_value_range ? a.enterprise_value_range.map((v) => money(v)).join(' – ') : 'Not meaningful'}</div><div>Buyer equity required</div><div class="v">${money(a.sources_and_uses.buyer_equity_required)}</div><div>Annual debt service</div><div class="v">${money(a.annual_debt_service)}</div></div><p class="fine">${CALC_NOTE}</p>`,
  );
  out += sec(
    'Cases',
    `<div class="tbl-wrap stack"><table><thead><tr><th>Case</th><th class="num">EBITDA</th><th class="num">Cash after debt service</th><th class="num">Coverage ratio</th></tr></thead><tbody>${Object.entries(
      a.scenarios,
    )
      .map(
        ([k, v]) =>
          `<tr><td>${h(label(k))}</td><td class="num" data-l="EBITDA">${money(v.ebitda)}</td><td class="num" data-l="After debt">${money(v.cash_after_debt_service)}</td><td class="num" data-l="Coverage">${v.coverage_ratio === null ? 'No debt service' : ratio(v.coverage_ratio) + '×'}</td></tr>`,
      )
      .join('')}</tbody></table></div>`,
  );
  out += sec(
    'Earnings adjustments',
    props([
      [
        'Accepted',
        a.accepted_adjustments.length
          ? a.accepted_adjustments.map((x) => `${h(x.label)} · ${money(x.amount)}`).join('<br>')
          : 'None',
      ],
      [
        'Excluded',
        a.excluded_adjustments.length
          ? a.excluded_adjustments
              .map(
                (x) =>
                  `${h(x.label)} · ${money(x.amount)}<span class="sub">${h(x.reason || 'No reviewed evidence')}</span>`,
              )
              .join('')
          : 'None',
      ],
    ]) +
      `<details class="spaced"><summary>Sources, uses and limitations</summary><pre>${h(JSON.stringify({ sources_and_uses: a.sources_and_uses, limitations: a.limitations }, null, 2))}</pre></details>`,
  );
  const evidence = (o.data.evidence_ids || []).map(find).filter(Boolean);
  out += sec('Evidence', attList(evidence));
  return out;
}
function inspDocument(o) {
  const templates = TEMPLATES;
  return (
    head({
      kicker: `Document · ${h(templates[o.data.template] || label(o.data.template))}`,
      title: h(o.title),
      glyph: o.stale ? g('review') : tick,
      stateLabel: o.stale ? 'Out of date' : 'Draft',
      next: `generated ${h(dFull(o.created_at))}`,
      actions:
        btn('Read draft', `data-action="document" data-id="${h(o.id)}"`, true) +
        `<a class="btn" href="/api/businesses/${h(state.bid)}/documents/${h(o.id)}/download">Download</a>` +
        (o.stale && canWrite() ? btn('Generate a new version', 'data-action="new-document"') : ''),
      extra: o.stale
        ? flag(
            '<b>The records this draft was made from have changed since.</b> This copy stays as it was generated. Generate a new version to reflect the current records.',
          )
        : '',
    }) +
    sec(
      'Draft',
      props([
        ['Type', h(templates[o.data.template] || label(o.data.template))],
        ['Generated', h(dateTime(o.data.generated_at || o.created_at))],
        [
          'From',
          `Business version ${h(o.data.business_version)} and ${plural((o.data.source_ids || []).length, 'record')}`,
        ],
        ['Fingerprint', `<code>${h(o.data.content_sha256)}</code>`],
      ]),
    ) +
    sec(
      'Text',
      `<pre class="document">${h(o.data.content)}</pre><p class="fine">A working draft. Contract briefs are preparation notes, not executable agreements.</p>`,
    )
  );
}
function inspInvoice(o) {
  const d = o.data;
  const pays = objs('payment').filter((p) => p.data.invoice_id === o.id);
  const ev = [d.contract_evidence_id, d.acceptance_evidence_id, d.issue_evidence_id].filter(
    Boolean,
  );
  let actions = '';
  if (canReview() && o.state === 'draft')
    actions += btn('Record issue', `data-action="issue-invoice" data-id="${h(o.id)}"`, true);
  if (canReview() && ['issued', 'partially_paid'].includes(o.state))
    actions += btn('Record payment', `data-action="new-payment" data-id="${h(o.id)}"`, true);
  actions += btn('Details', `data-action="inspect" data-id="${h(o.id)}"`);
  let flags = '';
  if (o.evidence_warning)
    flags += flag(
      '<b>Evidence behind this invoice is withdrawn or unreviewed.</b> Reconcile it before relying on this entry.',
    );
  if (o.state === 'draft')
    flags += flag(
      'A draft stays internal. Business Foundry does not send invoices; record the issue after you have sent it yourself.',
      true,
    );
  return (
    head({
      kicker: 'Invoice',
      title: h(o.title),
      glyph: o.evidence_warning ? g('review') : invoiceGlyph(o.state),
      stateLabel: label(o.state),
      next: `due ${h(plainDate(d.due_date))}`,
      actions,
      extra: flags,
    }) +
    sec(
      'Amounts',
      `<div class="figs"><div>Amount</div><div class="v">${money(d.amount, d.currency)}</div><div>Paid</div><div class="v">${money(d.paid, d.currency)}</div><div class="rule"></div><div>Outstanding</div><div class="v">${money(d.balance, d.currency)}</div></div><p class="fine">${CALC_NOTE}</p>`,
    ) +
    sec(
      'Invoice',
      props([
        ['Customer', linkTo(find(d.customer_id))],
        ['Due', h(plainDate(d.due_date))],
        [
          'Issued',
          d.issued_at
            ? `${h(dFull(d.issued_at))}<span class="sub">${h(d.issue_note || '')}</span>`
            : 'Not yet',
        ],
      ]),
    ) +
    sec('Evidence', attList(ev.map(find).filter(Boolean))) +
    sec('Payments', attList(pays))
  );
}
function inspPayment(o) {
  const d = o.data;
  return (
    head({
      kicker: 'Payment receipt',
      title: h(o.title),
      glyph: o.evidence_warning ? g('review') : tick,
      stateLabel: label(o.state),
      next: `received ${h(plainDate(d.received_on))}`,
      actions: btn('Details', `data-action="inspect" data-id="${h(o.id)}"`),
      extra: o.evidence_warning
        ? flag(
            '<b>The receipt evidence was withdrawn.</b> Reconcile this entry with the accounting system.',
          )
        : '',
    }) +
    sec(
      'Amount',
      `<div class="figs"><div>Received</div><div class="v">${money(d.amount, d.currency)}</div></div><p class="fine">Money actually received, matched to a reviewed receipt.</p>`,
    ) +
    sec(
      'Payment',
      props([
        ['Invoice', linkTo(find(d.invoice_id))],
        ['Reference', h(d.reference)],
        ['Received', h(plainDate(d.received_on))],
      ]),
    ) +
    sec('Evidence', attList([find(d.evidence_id)].filter(Boolean)))
  );
}
function inspHistory(seq) {
  return `<div data-history="${h(seq || '')}"><p class="fine">Loading the history…</p></div>`;
}
function fillHistory(seq) {
  const el = document.querySelector('#insp [data-history]');
  if (!el) return;
  fillFromAudit(
    el,
    (a) => {
      const ok = a.integrity.valid;
      let out;
      if (seq) {
        const e = a.events.find((x) => String(x.seq) === String(seq));
        out = e
          ? head({
              kicker: 'History · event ' + h(e.seq),
              title: h(eventTitle(e)),
              stateLabel: h(dateTime(e.created_at)),
              next: h(who(e.actor_id)),
            }) +
            sec(
              'Event',
              props([
                ['Record', h(e.payload.title || e.payload.data?.name || e.object_id)],
                ['Record id', `<code>${h(e.object_id)}</code>`],
                ['Version', h(e.payload.version ?? '—')],
                ['State', h(label(e.payload.state || '—'))],
                ['Chain hash', `<code>${h(e.hash)}</code>`],
              ]),
            ) +
            sec('Recorded payload', `<pre>${h(JSON.stringify(e.payload, null, 2))}</pre>`)
          : '<p class="empty">Event not found.</p>';
      } else {
        out =
          head({
            kicker: 'History',
            title: 'Integrity check',
            glyph: ok ? tick : g('review'),
            stateLabel: ok ? 'History and current records agree' : 'Integrity check failed',
            next: `${a.integrity.events_checked} events checked`,
            actions: `<a class="btn" href="/api/businesses/${h(state.bid)}/export">Export business records</a>`,
          }) +
          (ok
            ? ''
            : flag(
                `<b>Investigate before relying on this workspace.</b><ul>${(a.integrity.record_errors || []).map((x) => `<li>${h(x)}</li>`).join('')}</ul>`,
              )) +
          sec(
            'What this checks',
            `<p class="prose">Every change is recorded with its actor, sequence and the version it produced, in a hash chain. This check recomputes the chain and compares each current record and revision with its history.</p><p class="fine">${h(a.integrity.limitation)}</p>`,
          ) +
          sec(
            'Backups',
            `<p class="prose">Run <code>uv run foundry backup /private/path/backup.db</code>. Backups hold every business and the credential hashes; keep them private.</p>`,
          );
      }
      el.innerHTML = out;
    },
    (e) => {
      el.innerHTML = `<p class="empty">The history could not be loaded: ${h(messageFor(e))}</p>`;
    },
  );
}
function inspAccess(kind, id) {
  const authority = flag(
    '<b>Assistants advise. They cannot approve, submit, verify or move money.</b> Business Foundry does every sum itself, and every decision stays with a person.',
    true,
  );
  if (kind === 'key') {
    const k = (state.principals || []).find((x) => x.id === id);
    if (!k) return '<p class="empty">Access key not found.</p>';
    return (
      head({
        kicker: 'Access key',
        title: h(k.name),
        glyph: k.revoked ? dash : tick,
        stateLabel: k.revoked ? 'Revoked' : h(label(k.role)),
        actions:
          !k.revoked && k.id !== state.actor.id
            ? btn('Revoke', `data-action="revoke-key" data-id="${h(k.id)}"`, false, false, 'danger')
            : '',
      }) +
      sec(
        'Access',
        props([
          ['Role', h(label(k.role))],
          [
            'Businesses',
            k.role === 'owner'
              ? 'All businesses'
              : h(
                  k.business_ids
                    .map((b) => state.businesses.find((x) => x.id === b)?.data.name || b)
                    .join(', '),
                ),
          ],
          [
            'Can it approve?',
            ['owner', 'reviewer'].includes(k.role)
              ? 'Yes — a person with this key can approve and verify.'
              : k.role === 'agent'
                ? 'No. An agent key can prepare work, never approve evidence or attest an external action.'
                : 'No.',
          ],
        ]),
      )
    );
  }
  if (kind === 'run') {
    const r = state.runs.find((x) => x.id === id);
    if (!r) return '<p class="empty">Review not found.</p>';
    const adv = r.data.advice;
    return (
      head({
        kicker: 'Assistant review',
        title: h(adv?.summary || r.data.error || 'Provider outcome not confirmed'),
        glyph: r.state === 'succeeded' ? tick : g('review'),
        stateLabel: h(label(r.state)),
        next: h(dateTime(r.created_at)),
      }) +
      authority +
      (r.data.source_fingerprint !== state.snapshot.source_fingerprint
        ? flag(
            '<b>Given on an earlier state of the records.</b> The records have changed since this review.',
          )
        : '') +
      (adv
        ? sec(
            'Suggestions',
            props(
              adv.recommendations.map((x) => [
                h(x.action),
                `${h(x.reason)}${x.evidence_ids.length ? `<span class="sub">Cites: ${x.evidence_ids.map((e) => (find(e) ? linkTo(find(e)) : h(e))).join(', ')}</span>` : ''}`,
              ]),
            ),
            '<span class="h-tag">Advice only</span>',
          ) +
          sec(
            'Unknowns',
            adv.unknowns.length
              ? `<ul>${adv.unknowns.map((u) => `<li>${h(u)}</li>`).join('')}</ul>`
              : '<p class="fine">None listed.</p>',
          )
        : '') +
      sec(
        'Provenance',
        `<details><summary>Full request, result and sharing record</summary><pre>${h(JSON.stringify(r.data, null, 2))}</pre></details>`,
      )
    );
  }
  return (
    head({
      kicker: 'Access & assistants',
      title: 'Assistants and sharing',
      stateLabel: state.actor.ai_configured ? 'Optional review configured' : 'Not configured',
      next: 'advice only',
      actions:
        state.actor.ai_configured && canWrite()
          ? btn('Request an AI review', 'data-action="ai-review"', true)
          : '',
    }) +
    authority +
    sec(
      'Optional assistant review',
      `<p class="prose">${state.actor.ai_configured ? 'Ask for a structured review of the concept and record summaries. Original evidence bodies and full financial tables are not sent. Each request asks you first.' : 'No assistant is configured. The workspace works fully without one. To enable reviews, configure a provider key and model on the server, or connect an AI client through the agent adapter with an agent key.'}</p><p class="fine">Provider usage is billed by the provider. Server limits and every request and response are recorded.</p>`,
    ) +
    sec(
      'Your business records',
      `<p class="prose">Download a portable snapshot of records, evidence, documents and history. Keep exports private.</p><div class="i-actions"><a class="btn" href="/api/businesses/${h(state.bid)}/export">Export this business</a></div><details class="spaced"><summary>Backup and restore</summary><p class="fine">Run <code>uv run foundry backup /private/path/backup.db</code>. Restore into a new database path with <code>uv run foundry --db /private/path/restored.db restore /private/path/backup.db</code>. Backups contain credential hashes and all businesses; protect them.</p></details>`,
    ) +
    (isOwner()
      ? sec(
          'People and access keys',
          '<p class="prose">Owner keys cover every business. Other roles are limited to the workspaces selected for them. Give an AI client an agent key, never an owner or reviewer key.</p>',
        )
      : '')
  );
}

/* ---------------- search ---------------- */
const q = $('#q');
function applySearch() {
  const term = q.value.trim().toLowerCase();
  const list = $('#list');
  const rows = [...list.querySelectorAll('.row')];
  // The row and its list item are hidden together, so no empty list item is left announced.
  rows.forEach(
    (r) =>
      (r.hidden = r.parentElement.hidden =
        Boolean(term) && !r.textContent.toLowerCase().includes(term)),
  );
  // A list with no match left is hidden, and so are its heading and note.
  list
    .querySelectorAll('ul.rows')
    .forEach((ul) => (ul.hidden = Boolean(term) && !ul.querySelector('.ri:not([hidden])')));
  list.querySelectorAll('.grp').forEach((gh) => {
    const ul = gh.id ? list.querySelector(`ul[aria-labelledby="${CSS.escape(gh.id)}"]`) : null;
    const off = Boolean(term) && !(ul && !ul.hidden);
    gh.hidden = off;
    if (gh.nextElementSibling?.classList.contains('grp-note')) gh.nextElementSibling.hidden = off;
  });
  const th = list.querySelector('.today-head');
  if (th) th.hidden = Boolean(term);
  let empty = list.querySelector('.empty.search-empty');
  if (term && !rows.some((r) => !r.hidden)) {
    if (!empty) {
      empty = document.createElement('p');
      empty.className = 'empty search-empty';
      list.prepend(empty);
    }
    empty.textContent = `Nothing in ${$('#list-title').textContent} matches “${q.value.trim()}”.`;
  } else if (empty) empty.remove();
  joinLines();
}
q.addEventListener('input', applySearch);
q.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    q.value = '';
    applySearch();
  }
});

/* ---------------- focus ---------------- */
/* Rendering replaces the source list, the middle list and the inspector wholesale. When the
   focused control was inside one of them, focus moves to the same control in the new markup
   (the same source, row, action or link), or, if it is gone, to a deliberate place in the same
   region: the current source, the selected row, or the inspector's heading. Focus elsewhere
   (the toolbar, a dialog) is never touched. */
const visible = (el) => Boolean(el && el.getClientRects().length);
const focusFirst = (selectors) => {
  for (const sel of selectors) {
    const el = [...document.querySelectorAll(sel)].find(visible);
    if (el) {
      el.focus({ preventScroll: true });
      return true;
    }
  }
  return false;
};
const SELECTED_SOURCE = '#src-nav .src[aria-current="true"]';
const SELECTED_ROW = '#list .row[aria-current="true"]';
const FOCUS_FALLBACK = {
  'src-nav': [SELECTED_SOURCE],
  'src-foot': [SELECTED_SOURCE],
  list: [SELECTED_ROW, '#list .row'],
  'list-right': [SELECTED_ROW, '#list .row'],
  insp: ['#insp-title', SELECTED_ROW],
};
/* Where focus goes when the control that had it disappears outside the panes (the out-of-date
   notice, a dialog closed for it): the selected row, else the first row, else the inspector's
   heading, whichever is visible. On a phone showing the sources, the current source. */
const RESTING_FOCUS = [SELECTED_ROW, '#list .row', '#insp-title', SELECTED_SOURCE];
const SAME_CONTROL = ['data-src', 'data-key', 'data-action', 'data-id', 'data-to', 'data-go'];
/* A selector for the control that stands in the place of `el` once the markup is drawn again:
   the same id, or the same tag with the same identifying attributes. Empty when it has none. */
function sameControl(el) {
  if (el.id) return '#' + CSS.escape(el.id);
  const attrs = SAME_CONTROL.filter((a) => el.hasAttribute(a));
  return attrs.length
    ? el.tagName.toLowerCase() +
        attrs.map((a) => `[${a}="${CSS.escape(el.getAttribute(a))}"]`).join('')
    : '';
}
function focusMemo() {
  const el = document.activeElement;
  const region =
    el && el !== document.body
      ? el.closest('#src-nav, #src-foot, #list-right, #list, #insp')
      : null;
  if (!region) return null;
  return { el, region: region.id, same: sameControl(el) };
}
function rerender(render) {
  const memo = focusMemo();
  render();
  restoreFocus(memo);
}
/* Disabling a focused button while it waits on the server drops focus to the page body, so a
   save takes its memo before that and restores it once the new markup is in place. */
function restoreFocus(memo) {
  if (!memo || memo.el.isConnected) return;
  if (document.activeElement && document.activeElement !== document.body) return;
  focusFirst([
    ...(memo.same ? [`#${memo.region} ${memo.same}`] : []),
    ...FOCUS_FALLBACK[memo.region],
  ]);
}

/* ---------------- navigation ---------------- */
const app = $('#app');
const isPhone = () => window.matchMedia('(max-width:699px)').matches;
function renderAll() {
  rerender(() => {
    renderSources();
    renderList();
    renderInspector();
  });
}
function setSource(id, key) {
  state.source = sourceById(id) ? id : 'today';
  state.selected = key || state.remembered[state.source] || null;
  state.open = null;
  q.value = '';
  renderAll();
}
function select(key) {
  state.selected = key;
  state.remembered[state.source] = key;
  state.open = null;
  document
    .querySelectorAll('#list .row')
    .forEach((r) => r.setAttribute('aria-current', String(r.dataset.key === key)));
  rerender(renderInspector);
}
/* On a phone one pane shows at a time. A pane change that hides the focused control moves focus
   into the pane that appears: its back control going forward, or, coming back, the place the
   person left (the current source, the selected row). */
function setLevel(level) {
  const from = app.dataset.level;
  app.dataset.level = level;
  if (!isPhone()) return;
  window.scrollTo(0, 0);
  if (visible(document.activeElement) && document.activeElement !== document.body) return;
  const into = {
    sources: [SELECTED_SOURCE, '#src-nav .src'],
    list: from === 'detail' ? [SELECTED_ROW, '#back-src'] : ['#back-src'],
    detail: ['#back-list'],
  }[level];
  if (into) focusFirst(into);
}
function pickStation(id) {
  if (state.source !== 'journey') setSource('journey', 'jr:' + id);
  else select('jr:' + id);
  const cur = document.querySelector('#list .row[aria-current="true"]');
  if (cur && !isPhone()) cur.scrollIntoView({ block: 'nearest' });
  const st = [...document.querySelectorAll(`#insp .stn[data-id="${CSS.escape(id)}"]`)].find(
    (el) => el.getBoundingClientRect().width > 0,
  );
  if (st) st.focus({ preventScroll: true });
}
function go(spec) {
  const [src, key] = spec.split('|');
  setSource(src, key);
  setLevel('detail');
  const cur = document.querySelector('#list .row[aria-current="true"]');
  if (cur && !isPhone()) cur.scrollIntoView({ block: 'nearest' });
}

/* ---------------- server ---------------- */
let toastTimer;
function toast(text) {
  $('#toast').textContent = text;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($('#toast').hidden = true), 5500);
}
/* What the server, or the way to it, said. Its message is written for the person. Any other error
   is a fault in this page, and its text (a TypeError, say) is never shown. */
class RequestError extends Error {}
function messageFor(error) {
  if (error instanceof RequestError) return error.message;
  console.error(error);
  return 'Something went wrong in this page. Load the current records and try again.';
}
/* A business command whose answer was lost may have reached the server. Its form keeps the same
   Idempotency-Key, and the server answers a repeat of the same request under the same key with
   the result it recorded, so sending the unchanged form again is safe. This is true only while
   that form is open; a command with other rules passes its own `interrupted` message. */
const SAME_KEY =
  'Connection interrupted. Retry the unchanged form to check the same request safely.';
async function api(path, { interrupted = SAME_KEY, ...options } = {}) {
  const headers = { ...options.headers };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const command = Boolean(options.method) && !['GET', 'HEAD'].includes(options.method);
  if (command) headers['Idempotency-Key'] ||= crypto.randomUUID();
  const session = sessionSeq;
  let response;
  try {
    response = await fetch('/api' + path, { ...options, headers, credentials: 'same-origin' });
  } catch {
    // A read changes nothing, so there is nothing to retry carefully.
    const error = new RequestError(command ? interrupted : 'The server could not be reached.');
    error.interrupted = command;
    throw error;
  }
  const data = await response.json().catch(() => ({ detail: 'Unexpected server response.' }));
  if (!response.ok) {
    if (response.status === 401 && path !== '/login' && session === sessionSeq) showLogin();
    throw new RequestError(data.detail || 'The request could not be completed.');
  }
  return data;
}
/* A command to one business: the business is the one its form was opened for, never the one on
   screen when it is sent. */
const post = (bid, suffix, body, key) =>
  api(`/businesses/${bid}${suffix}`, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': key },
  });
/* Whether a message about a command for `scope` may be read on another business's page: the page
   shows another one now, or is about to. Such a message names its business. */
const offScreen = (scope) =>
  Boolean(scope) &&
  (scope.bid !== state.bid || Boolean(state.wantBid && state.wantBid !== scope.bid));
/* The message for a command that failed when its form is no longer open, or when the page shows
   another business than the one the command was for (`scope`). An interrupted command can no
   longer be sent again under its key, so the message does not suggest it. */
function failedText(error, scope) {
  const elsewhere = offScreen(scope);
  if (error.interrupted && error.message === SAME_KEY)
    return elsewhere
      ? `Connection interrupted. This page cannot tell whether your change was saved to ${scope.name}; check its records before entering it again.`
      : 'Connection interrupted. This page cannot tell whether your change was saved; load the current records and check before entering it again.';
  if (!elsewhere) return messageFor(error);
  return error.recorded
    ? `The review requested for ${scope.name} did not complete. ${messageFor(error)}`
    : `Not saved to ${scope.name}. ${messageFor(error)}`;
}
/* Sign-out requests still waiting for their answer: the AbortController of each. */
const signingOut = new Set();
/* Sign-out, or a 401: the previous person's records leave memory and the page, not just the view.
   Any request still in flight belongs to an older load and an older session, so it is dropped,
   and so is anything waiting to be shown, a new access key included. */
function forget() {
  loadSeq += 1;
  sessionSeq += 1;
  // A sign-out still waiting is cancelled before anyone can sign in again. Its answer deletes the
  // session cookie, and arriving after a new sign-in it would delete the new session's.
  for (const cancel of signingOut) cancel.abort();
  signingOut.clear();
  waiting.length = 0;
  dialogAbout = null;
  opener = null;
  clearTimeout(toastTimer);
  Object.assign(state, {
    actor: null,
    businesses: [],
    wantBid: null,
    loaded: 0,
    snapshot: null,
    runs: [],
    principals: null,
    audit: null,
    auditPromise: null,
    source: 'today',
    selected: null,
    remembered: {},
    scenario: 'base',
    open: null,
    behind: false,
    accepted: {},
  });
  M = buildModel(null);
  if ($('#dialog').open) $('#dialog').close();
  for (const id of ['#dialog-content', '#src-nav', '#src-foot', '#list', '#list-right', '#insp'])
    $(id).innerHTML = '';
  for (const id of ['#dialog-title', '#crumb', '#actor-name', '#tb-date']) $(id).textContent = '';
  $('#list-title').textContent = 'Today';
  $('#back-list-lbl').textContent = 'Today';
  $('#business-select').innerHTML = '';
  $('#demo-tag').hidden = true;
  $('#toast').hidden = true;
  $('#outdated').hidden = true;
  $('#outdated-text').textContent = '';
  q.value = '';
  app.dataset.level = 'list';
}
function showLogin() {
  forget();
  app.hidden = true;
  $('#login').hidden = false;
  $('#login input[name="token"]').focus();
}
/* Load the businesses and the records of the one asked for, then show them. Returns false, and
   changes nothing, when a newer refresh or a sign-out has started meanwhile; its errors are then
   dropped too. When it fails it changes nothing on screen either, and a business that was asked
   for is let go: the selector goes back to the business shown, so no later reload switches to it
   unasked. The error then carries the name of that business as `missed`. */
async function refresh() {
  const load = ++loadSeq;
  const stale = () => load !== loadSeq;
  const wanted = state.wantBid || state.bid;
  let businesses, bid, snapshot, runs, principals;
  try {
    businesses = await api('/businesses');
    if (stale()) return false;
    bid = businesses.some((b) => b.id === wanted) ? wanted : businesses[0]?.id || null;
    [snapshot, runs, principals] = await Promise.all([
      bid ? api('/businesses/' + bid) : Promise.resolve(null),
      bid ? api(`/businesses/${bid}/ai`).catch(() => []) : Promise.resolve([]),
      isOwner() ? api('/principals').catch(() => null) : Promise.resolve(null),
    ]);
  } catch (error) {
    if (stale()) return false;
    if (state.wantBid && state.wantBid !== state.bid)
      error.missed =
        state.businesses.find((b) => b.id === state.wantBid)?.data.name || 'the new business';
    state.wantBid = null;
    $('#business-select').value = state.bid || '';
    throw error;
  }
  if (stale()) return false;
  let closed = null;
  if (bid !== state.bid) {
    // another business: nothing about the previous one stays open over it
    closed = closeForeign(bid, snapshot?.business.data.name || '');
    // and it starts from its Today list
    Object.assign(state, {
      source: 'today',
      selected: null,
      remembered: {},
      open: null,
      scenario: 'base',
    });
  }
  Object.assign(state, {
    businesses,
    bid,
    wantBid: null,
    loaded: load,
    snapshot,
    runs: Array.isArray(runs) ? runs : [],
    principals,
    audit: null,
    auditPromise: null,
    behind: false,
    accepted: {},
  });
  $('#outdated').hidden = true;
  if (bid) saved.set('foundry.business', bid);
  const select = $('#business-select');
  select.innerHTML =
    businesses
      .map(
        (b) =>
          `<option value="${h(b.id)}" ${b.id === bid ? 'selected' : ''}>${h(b.data.name)}</option>`,
      )
      .join('') || '<option value="">No businesses yet</option>';
  select.disabled = !businesses.length;
  M = buildModel(snapshot);
  $('#demo-tag').hidden = !snapshot?.business.data.is_demo;
  $('#actor-name').textContent = state.actor.name + ' · ' + label(state.actor.role);
  $('[data-action="new-business"]').hidden = !isOwner();
  $('#tb-date').textContent = new Date().toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
  if (!state.snapshot) state.source = 'today';
  renderAll();
  if (closed !== null) {
    // the dialog gave focus back to the control that opened it, which the new records replaced
    if (document.activeElement === document.body || !visible(document.activeElement))
      focusFirst(RESTING_FOCUS);
    if (closed) {
      closedNote = { load, text: closed };
      toast(closed);
    }
  }
  return true;
}
/* The note a refresh showed when it closed a form, kept so that a message that follows from the
   same load (a save reported by name) is shown together with it rather than over it. */
let closedNote = { load: 0, text: '' };
/* The business on screen is changing to `bid`. A dialog about the previous one is closed and its
   content removed, and the decision form of a step is taken away with the step. Returns null
   when nothing was closed; otherwise what to tell the person: for a form, that it was closed,
   without saving if nothing was sent from it, and for a view of the records, nothing. */
function closeForeign(bid, name) {
  const now = name ? `this page now shows ${name}` : 'this page no longer shows a business';
  const notes = [];
  const d = $('#dialog'),
    about = dialogAbout,
    dialog = d.open && Boolean(about?.bid) && about.bid !== bid;
  if (dialog) {
    d.close();
    $('#dialog-content').innerHTML = '';
    $('#dialog-title').textContent = '';
    if (about.form)
      notes.push(`“${about.title}” was closed${about.sent ? '' : ' without saving'}; ${now}.`);
  }
  const tx = $('#tx-form');
  if (tx && tx.dataset.bid !== bid) {
    const sent = tx.querySelector('[type=submit]').disabled || Boolean(state.open?.unsure);
    const step = find(tx.dataset.id)?.title || 'a step';
    notes.push(
      `The decision form for “${step}” was closed${sent ? '' : ' without saving'}; ${now}.`,
    );
  }
  return dialog || notes.length ? notes.join(' ') : null;
}
/* A reload that failed. After a command the server accepted, that failure is not the command's:
   the change is saved and only the page is behind. A notice on the page says so, offers to load
   the records again, and stays until a reload succeeds or the person dismisses it. Without such a
   command, a failed switch to another business is reported on its own. */
function reloadFailed(error, again = false) {
  const reason = messageFor(error);
  const shown = businessName();
  const missed = error.missed
    ? shown
      ? ` ${capFirst(error.missed)} was not opened; this page still shows ${shown}.`
      : ` ${capFirst(error.missed)} was not opened.`
    : '';
  if (!state.behind) {
    toast(
      missed
        ? `${capFirst(error.missed)} could not be opened. ${reason}${shown ? ` This page still shows ${shown}.` : ''}`
        : reason,
    );
    return;
  }
  $('#outdated').hidden = false;
  $('#outdated-text').textContent =
    (again
      ? `The current records still could not be loaded. ${reason} Your change was saved`
      : `Your change was saved, but this page could not load the current records. ${reason} What you see may not include it yet`) +
    `; there is no need to enter it again.${missed}`;
}
/* Reload after a command the server accepted, while the page shows the business it was for
   (`scope`; null for a command that is not about one business). The view a save asked to move to
   (`moved`, the view before it) is kept only together with the records that show it. */
async function reloadAfter(before, scope = null) {
  state.behind = true;
  const session = sessionSeq,
    moved = { source: state.source, selected: state.selected };
  try {
    const shown = await refresh();
    // when a newer reload took over, that one shows the records, or reports why it could not
    if (session !== sessionSeq) return;
    const also = shown && closedNote.load === state.loaded ? ' ' + closedNote.text : '';
    toast(savedText(scope) + also);
  } catch (error) {
    if (session !== sessionSeq) return;
    if (before && state.source === moved.source && state.selected === moved.selected)
      Object.assign(state, before);
    renderAll();
    reloadFailed(error);
  }
}
const savedText = (scope) =>
  offScreen(scope) ? `Saved to ${scope.name}.` : 'Saved to the workspace.';

/* ---------------- forms ---------------- */
const TEMPLATES = {
  business_plan: 'Business plan',
  structure_options: 'Structure decision brief',
  contract_brief: 'Customer contract brief',
  operating_manual: 'Operating manual',
  readiness_report: 'Readiness report',
  investment_memo: 'Investment memo',
};
const input = (name, title, value = '', type = 'text', hint = '', attrs = '') =>
  `<label>${h(title)}<input aria-label="${h(title)}" name="${h(name)}" type="${type}" value="${h(value)}" ${attrs}>${hint ? `<span class="hint">${h(hint)}</span>` : ''}</label>`;
const area = (name, title, value = '', hint = '') =>
  `<label class="full">${h(title)}<textarea aria-label="${h(title)}" name="${h(name)}" required>${h(value)}</textarea>${hint ? `<span class="hint">${h(hint)}</span>` : ''}</label>`;
const choose = (name, title, options, current = '', hint = '', multi = false) =>
  `<label>${h(title)}<select aria-label="${h(title)}" name="${h(name)}" ${multi ? 'multiple' : ''}>${options
    .map((c) => {
      const [v, t] = Array.isArray(c) ? c : [c, label(c)];
      const on = Array.isArray(current) ? current.includes(v) : current === v;
      return `<option value="${h(v)}" ${on ? 'selected' : ''}>${h(t)}</option>`;
    })
    .join('')}</select>${hint ? `<span class="hint">${h(hint)}</span>` : ''}</label>`;
const evidenceChoices = (onlyReviewed = false) =>
  objs('evidence')
    .filter((o) => !onlyReviewed || o.state === 'verified')
    .map((o) => [o.id, `${o.title} · ${evInfo(o).short}`]);
const evidenceSelect = (
  name = 'evidence_ids',
  title = 'Supporting evidence',
  current = [],
  reviewed = false,
  multi = true,
) =>
  choose(
    name,
    title,
    (multi ? [] : [['', 'Choose evidence…']]).concat(evidenceChoices(reviewed)),
    current,
    multi ? 'Select one or more. On a computer, use Ctrl or Command to select several.' : '',
    multi,
  );
/* What the open dialog is about: `bid` is the business whose records it shows or writes to, or
   null for a dialog that is not about the business on screen (creating a business, access keys);
   `form` marks a form, `sent` one that has sent its command, `secret` the dialog showing a new
   access key. Set by showDialog; a dialog about one business is closed when the page changes to
   another. */
let dialogAbout = null;
/* Dialogs that arrived while another was open (a new access key, or the notice that one may not
   have been created), shown in turn once no dialog is open. Each belongs to the session it was
   requested in, and the queue is emptied when that session ends. */
const waiting = [];
/* The control that opened the dialog, and the session it opened in. A dialog that takes the place
   of another as it closes (a key that waited behind a form) keeps the first one's opener when
   nothing outside the dialog had focus by then. Emptied when the session ends. */
let opener = null;
function showDialog(title, html, about = null) {
  dialogAbout = about;
  $('#dialog-title').textContent = title;
  $('#dialog-content').innerHTML = html;
  if (!$('#dialog').open) {
    const el = document.activeElement;
    if (el && el !== document.body && !app.hidden && app.contains(el))
      opener = {
        el,
        region: el.closest('#src-nav, #src-foot, #list-right, #list, #insp')?.id || null,
        same: sameControl(el),
        session: sessionSeq,
      };
    $('#dialog').showModal();
  }
}
/* A dialog has closed and none took its place. The browser hands focus back to the control that
   opened it; when that control was drawn again while the dialog was open, or is no longer shown,
   focus would be left on nothing. It goes to the control standing in the opener's place, else to
   the page's resting place. Focus the browser left on a visible control stays where it is, and
   nothing in the workspace is focused once the session that opened the dialog has ended. */
function returnFocus(memo) {
  if (app.hidden || (memo && memo.session !== sessionSeq)) return;
  const el = document.activeElement;
  if (el && el !== document.body && visible(el) && !$('#dialog').contains(el)) return;
  if (memo && memo.el.isConnected && visible(memo.el)) {
    memo.el.focus({ preventScroll: true });
    return;
  }
  const same = !memo?.same ? [] : memo.region ? [`#${memo.region} ${memo.same}`] : [memo.same];
  focusFirst([...same, ...RESTING_FOCUS, '#back-list', '#back-src']);
}
/* Show a dialog that arrived late, without replacing one the person has open. */
function showWhenFree(session, title, html, about = null) {
  if (session !== sessionSeq) return;
  waiting.push({ session, title, html, about });
  showWaiting();
}
function showWaiting() {
  if ($('#dialog').open) return;
  while (waiting.length) {
    const next = waiting.shift();
    if (next.session !== sessionSeq) continue;
    showDialog(next.title, next.html, next.about);
    return;
  }
}
$('#dialog').addEventListener('close', () => {
  // a dialog opened since (by the page, before this event arrived) is left as it is
  if ($('#dialog').open) return;
  // a shown access key leaves the page with its dialog
  if (dialogAbout?.secret) {
    $('#dialog-content').innerHTML = '';
    $('#dialog-title').textContent = '';
  }
  dialogAbout = null;
  showWaiting();
  // a dialog that waited is shown in its place, and keeps its opener
  if ($('#dialog').open) return;
  const memo = opener;
  opener = null;
  returnFocus(memo);
});
/* A form in a dialog. It belongs to the business on screen when it opens (`scope`): `submit`
   receives that scope and sends there. Unless `free` (a form that is not about the business on
   screen), the form is closed if the page changes to another business before it is sent, and
   its result is reported under its own business's name if the page has moved on. `lost` takes
   over when the answer to the command was lost and the command has no safe repeat. */
function formDialog(
  title,
  fields,
  submit,
  button = 'Save',
  intro = '',
  after = null,
  { free = false, lost = null } = {},
) {
  const scope = here(),
    about = { bid: free ? null : scope.bid, form: true, sent: false, title };
  showDialog(
    title,
    `<form id="active-form">${intro ? `<p>${h(intro)}</p>` : ''}<div class="form-grid">${fields}</div><p class="error spaced" role="alert"></p><div class="form-actions">${btn('Cancel', 'data-action="close-dialog"')}<button class="btn primary" type="submit">${h(button)}</button></div></form>`,
    about,
  );
  const form = $('#active-form');
  const command = crypto.randomUUID();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    // a form for a business that is no longer on screen sends nothing (it is closed when the
    // business changes; this is the second line of that rule)
    if (!free && state.bid !== scope.bid) return;
    $('#toast').hidden = true;
    const b = form.querySelector('[type=submit]');
    b.disabled = true;
    form.querySelector('.error').textContent = '';
    const shown = state.bid,
      session = sessionSeq;
    about.sent = true;
    let result;
    try {
      result = await submit(new FormData(form), command, scope);
    } catch (error) {
      if (session !== sessionSeq) return;
      if (lost && error.interrupted) {
        lost(form);
        return;
      }
      // The command itself failed. Its form stays, with the same key, so sending it again
      // unchanged is safe; if the person closed it meanwhile, the message is shown instead.
      b.disabled = false;
      // a refused command kept nothing; one whose answer was lost may have been saved, and the
      // form says so if it is closed later
      if (error.interrupted) about.unsure = true;
      about.sent = Boolean(about.unsure);
      if (form.isConnected && $('#dialog').open) {
        form.querySelector('.error').textContent = messageFor(error);
        // a failed save leaves focus on its button, which disabling had cleared
        if (document.activeElement === document.body) b.focus();
      } else toast(failedText(error, free ? null : scope));
      return;
    }
    if (session !== sessionSeq) return;
    // Saved. The dialog can be closed, and another opened, while a save is in flight; a late
    // result closes only its own form.
    if (form.isConnected && $('#dialog').open && dialogAbout === about) $('#dialog').close();
    // On another business's page the result is only reported, by name: that page is not behind,
    // and its view, state and focus are not this command's to change.
    if (!free && state.bid !== scope.bid) {
      toast(savedText(scope));
      return;
    }
    const before = {
      source: state.source,
      selected: state.selected,
      remembered: { ...state.remembered },
    };
    // the view moves only if the same business is shown and no switch to another is pending
    if (after && state.bid === shown && !state.wantBid) after(result);
    await reloadAfter(before, free ? null : scope);
  });
}
const selectAfter = (source, prefix) => (result) => {
  if (!result?.id) return;
  state.source = source;
  state.selected = prefix + result.id;
  state.remembered[source] = state.selected;
};
function businessDialog(edit = false) {
  const b = edit ? state.snapshot.business.data : {},
    // the version the fields are filled from; a newer one is refused by the server, not overwritten
    version = edit ? state.snapshot.business.version : null;
  const fields =
    input('name', 'Business name', b.name || '', 'text', '', 'required maxlength="240"') +
    choose(
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
    choose(
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
    async (f, key, scope) => {
      const body = Object.fromEntries(f);
      body.is_demo = f.has('is_demo');
      if (edit) {
        body.expected_version = version;
        return api('/businesses/' + scope.bid, {
          method: 'PUT',
          body,
          headers: { 'Idempotency-Key': key },
        });
      }
      return api('/businesses', { method: 'POST', body, headers: { 'Idempotency-Key': key } });
    },
    edit ? 'Save changes' : 'Create workspace',
    edit
      ? 'Changes reopen completed work for review. This form cannot change the jurisdiction: an owner or reviewer changes it as a separate, recorded decision with a reason, using the API or the foundry jurisdiction command.'
      : 'A concept creates the workspace and its dependency-aware steps. Unknowns can stay unknown until you have evidence.',
    (result) => {
      // the new business opens on its Today list once its records arrive
      if (!edit) state.wantBid = result.id;
    },
    // creating a business is not about the one on screen
    { free: !edit },
  );
}
function recordDialog(id) {
  const o = id ? find(id) : null,
    d = o?.data || {};
  if (!canWrite()) {
    showDialog(o.title, `<pre>${h(JSON.stringify(o, null, 2))}</pre>`, { bid: state.bid });
    return;
  }
  formDialog(
    o ? 'Edit business record' : 'Add business record',
    choose('kind', 'Record type', o ? [o.kind] : RECORD_KINDS, o?.kind || 'customer') +
      input('title', 'Title', o?.title || '', 'text', '', 'required') +
      area('details', 'Details', d.details || '') +
      choose(
        'certainty',
        'Evidence status',
        canReview() ? ['assumption', 'reported', 'verified'] : ['assumption', 'reported'],
        d.certainty || 'assumption',
      ) +
      input('owner', 'Responsible person', d.owner || state.snapshot.business.data.owner) +
      input('due_date', 'Due date', d.due_date || '', 'date') +
      evidenceSelect('evidence_ids', 'Supporting evidence', d.evidence_ids || []) +
      choose(
        'related_ids',
        'Related records',
        objs()
          .filter((x) => RECORD_KINDS.includes(x.kind) && x.id !== id)
          .map((x) => [x.id, x.title]),
        d.related_ids || [],
        'Optional links to the shared business records.',
        true,
      ),
    async (f, key, scope) => {
      const body = Object.fromEntries(f);
      body.evidence_ids = f.getAll('evidence_ids');
      body.related_ids = f.getAll('related_ids');
      body.due_date = body.due_date || null;
      body.properties = d.properties || {};
      if (o) {
        body.expected_version = o.version;
        return api(`/businesses/${scope.bid}/records/${id}`, {
          method: 'PUT',
          body,
          headers: { 'Idempotency-Key': key },
        });
      }
      return post(scope.bid, '/records', body, key);
    },
    'Save record',
    o ? 'Changing a record reopens completed steps that rely on records of this type.' : '',
    selectAfter('records', 'rec:'),
  );
}
function newEvidence() {
  formDialog(
    'Add source evidence',
    input('title', 'Evidence title', '', 'text', '', 'required') +
      choose(
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
    async (f, key, scope) => {
      const body = Object.fromEntries(f);
      body.effective_date = body.effective_date || null;
      return post(scope.bid, '/evidence', body, key);
    },
    'Save unverified evidence',
    '',
    selectAfter('evidence', 'ev:'),
  );
}
function reviewEvidence(id, revoke = false) {
  const o = find(id);
  formDialog(
    revoke ? 'Withdraw evidence verification' : 'Record a human review',
    area(
      'note',
      'Review note',
      '',
      'Describe what you checked, its relevance and any limitations.',
    ),
    async (f, key, scope) =>
      post(
        scope.bid,
        `/evidence/${id}/${revoke ? 'revoke' : 'review'}`,
        { note: f.get('note'), expected_version: o.version },
        key,
      ),
    revoke ? 'Confirm withdrawal' : 'Mark evidence reviewed',
    revoke
      ? 'Completed steps that relied on it will reopen, and verified records citing it return to reported. The original source text and every earlier decision remain in the history.'
      : 'This records your assessment of the source. It does not independently authenticate the document or certify its claims.',
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
  ['monthly_retention', 'Monthly unit retention', 0],
  ['fixed_costs', 'Monthly fixed costs', 6000],
  ['fixed_cost_step', 'Monthly fixed-cost step', 0],
  ['fixed_cost_step_month', 'Fixed-cost step start month', ''],
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
  const p = latestPlan()?.data.inputs || {};
  formDialog(
    'Build a financial model',
    input('title', 'Version name', 'Operating plan', 'text', '', 'required') +
      evidenceSelect() +
      financeFields
        .map(([k, t, v]) =>
          k === 'fixed_cost_step_month'
            ? input(
                k,
                t,
                p[k] ?? v,
                'number',
                'Optional month (1–60) when fixed-cost step begins.',
                'min="1" max="60" step="1"',
              )
            : input(
                k,
                t,
                p[k] ?? v,
                'number',
                k.includes('rate') || k.includes('growth') || k.includes('retention')
                  ? 'Decimal rate: 0.20 means 20%.'
                  : '',
                'step="any" required',
              ),
        )
        .join(''),
    async (f, key, scope) => {
      const inputs = {};
      for (const [k] of financeFields) {
        if (k === 'fixed_cost_step_month') {
          const raw = String(f.get(k) ?? '').trim();
          inputs[k] = raw === '' ? null : Number(raw);
        } else
          inputs[k] = ['months', 'receivable_days', 'asset_life_months'].includes(k)
            ? Number(f.get(k))
            : f.get(k);
      }
      return post(
        scope.bid,
        '/forecasts',
        { title: f.get('title'), inputs, evidence_ids: f.getAll('evidence_ids') },
        key,
      );
    },
    'Calculate and save',
    'Save a model version that is never changed afterwards. Business Foundry calculates base, downside and upside cases with the same linked statements, using exact decimal arithmetic in the workspace currency.',
    selectAfter('money', 'plan:'),
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
      `<details class="full"><summary>Earnings adjustments (${d.adjustments?.length || 0} retained from prior analysis)</summary><p class="hint">Existing adjustments are retained. Add another here; only adjustments with reviewed evidence enter normalized earnings. Management replacement is deducted separately.</p>${d.adjustments?.length ? `<pre>${h(JSON.stringify(d.adjustments, null, 2))}</pre>` : ''}<div class="form-grid spaced">${input('adjustment_label', 'New adjustment label')}${input('adjustment_amount', 'Adjustment amount', 0, 'number', 'Positive adds to EBITDA; negative deducts.', 'step="any"')}${evidenceSelect('adjustment_evidence', 'Adjustment evidence', '', false, false)}</div></details>`,
    async (f, key, scope) => {
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
      return post(scope.bid, '/deals', body, key);
    },
    'Calculate and create diligence work',
    'Multiples and financing terms are your assumptions. Negotiated consideration is a transaction use; the cash/debt valuation bridge is not automatically additional funding or debt service.',
    selectAfter('deals', 'deal:'),
  );
}
function documentDialog(dealId = null) {
  formDialog(
    'Generate a working document',
    choose(
      'template',
      'Document type',
      Object.entries(TEMPLATES),
      dealId ? 'investment_memo' : 'business_plan',
    ) +
      choose(
        'deal_id',
        'Acquisition analysis',
        [['', 'Not applicable'], ...objs('deal').map((o) => [o.id, o.title])],
        dealId || '',
      ),
    async (f, key, scope) =>
      post(
        scope.bid,
        '/documents',
        { template: f.get('template'), deal_id: f.get('deal_id') || null },
        key,
      ),
    'Generate draft',
    'Documents keep the source version and explicit unknowns. Contract outputs are preparation briefs, not executable legal agreements.',
    selectAfter('documents', 'doc:'),
  );
}
function invoiceDialog() {
  formDialog(
    'Draft an invoice record',
    choose('customer_id', 'Customer', [
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
    async (f, key, scope) => post(scope.bid, '/invoices', Object.fromEntries(f), key),
    'Save draft',
    'A draft remains internal. A human reviewer must attach reviewed external receipts before recording actual issue.',
    selectAfter('sales', 'inv:'),
  );
}
function issueDialog(id) {
  const o = find(id);
  formDialog(
    'Record external invoice issue',
    evidenceSelect('evidence_id', 'Issue receipt', '', true, false) +
      area('note', 'Issue note', '', 'Record where and when the invoice was actually issued.'),
    async (f, key, scope) =>
      post(
        scope.bid,
        `/invoices/${id}/issue`,
        { ...Object.fromEntries(f), expected_version: o.version },
        key,
      ),
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
    async (f, key, scope) =>
      post(scope.bid, `/invoices/${id}/payments`, Object.fromEntries(f), key),
    'Record payment',
    'Record only money actually received. Partial payments are supported; duplicate references and overpayments are rejected.',
  );
}
function keyDialog() {
  // the name the key is created under, kept to name it if the answer is lost
  let name = '';
  formDialog(
    'Create scoped access',
    input('name', 'Person or agent name', '', 'text', '', 'required') +
      choose(
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
      choose(
        'business_ids',
        'Business access',
        state.businesses.map((b) => [b.id, b.data.name]),
        [state.bid],
        'Select the workspaces this key can access.',
        true,
      ),
    async (f) => {
      // captured before the request: a key that arrives after this session ended is dropped
      const session = sessionSeq;
      name = f.get('name');
      const key = await api('/principals', {
        method: 'POST',
        body: { name, role: f.get('role'), business_ids: f.getAll('business_ids') },
      });
      // Shown once, in this session only, and never over a dialog the person has open: it waits
      // for that dialog (this form included) to close. A change of business does not close it.
      // A key for a session that has ended is not put anywhere.
      if (session === sessionSeq)
        showWhenFree(
          session,
          'Save this access key',
          `<p>The server stores only a hash. Copy this key into a password manager or your client’s secret settings; it will not be shown again.</p><pre>${h(key.token)}</pre><p class="fine">${h(key.name)} · ${h(label(key.role))}. Never put the key in GitHub, a screenshot or a shared document.</p>`,
          { secret: true },
        );
      // the secret is not kept anywhere else
      return { id: key.id, name: key.name };
    },
    'Create key',
    '',
    null,
    {
      free: true,
      // The server cannot answer a repeat of this command from its records: it keeps only a hash
      // of the key, so a second request makes a second key. A lost answer is said to be lost.
      lost: (form) => {
        const session = sessionSeq;
        const text = `Connection interrupted, so this page cannot tell whether the access key “${name}” was created. A key that was created cannot be shown again. Look at the access keys under Access & assistants: if there is a key named “${name}” that you did not receive, revoke it, then create a new one.`;
        const html = `<p>${h(text)}</p><div class="form-actions">${btn('Close', 'data-action="close-dialog"', true)}</div>`;
        if (form.isConnected && $('#dialog').open) {
          showDialog('Access key not confirmed', html);
          $('#dialog-content [data-action="close-dialog"]').focus();
        } else showWhenFree(session, 'Access key not confirmed', html);
        // the list shows whether the key is there; if it cannot be loaded, the notice stands
        refresh().catch((e) => {
          if (session === sessionSeq) reloadFailed(e);
        });
      },
    },
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
    async (f, key, scope) => {
      const r = await post(
        scope.bid,
        '/ai',
        {
          question: f.get('question'),
          share_with_provider: f.has('share'),
          max_output_tokens: 2000,
        },
        key,
      );
      if (r.state !== 'succeeded') {
        // the run is recorded, with its outcome; only the advice is missing
        const error = new RequestError(r.data.error || r.data.note);
        error.recorded = true;
        throw error;
      }
      return r;
    },
    'Share and request review',
    'The answer is advice only. It has no authority to change records or approve work. Provider usage may incur charges.',
    (r) => {
      state.source = 'access';
      state.selected = 'run:' + r.id;
    },
  );
}
async function submitTransition(form) {
  const open = state.open,
    id = form.dataset.id,
    // what the form was drawn from: its business, and the step's version it shows
    scope = { bid: form.dataset.bid, name: businessName() },
    version = Number(form.dataset.version);
  // a form left from a decision that is no longer open, or drawn for a business or step no
  // longer on screen, is taken away rather than sent
  if (open?.kind !== 'tx' || open.id !== id || scope.bid !== state.bid || !find(id)) {
    rerender(renderInspector);
    return;
  }
  const f = new FormData(form);
  const b = form.querySelector('[type=submit]');
  const memo = focusMemo();
  b.disabled = true;
  form.querySelector('.error').textContent = '';
  $('#toast').hidden = true;
  const session = sessionSeq;
  try {
    await post(
      scope.bid,
      `/tasks/${id}/transition`,
      {
        state: f.get('state'),
        note: f.get('note'),
        evidence_ids: f.getAll('evidence_ids'),
        expected_version: version,
      },
      open.key,
    );
  } catch (error) {
    if (session !== sessionSeq) return;
    // a lost answer may have been saved; if the form is closed later, it does not say otherwise
    if (error.interrupted) open.unsure = true;
    // The decision itself failed: the form stays, and sending it again uses the same key.
    if (form.isConnected) {
      form.querySelector('.error').textContent = messageFor(error);
      b.disabled = false;
      if (memo?.el === b && document.activeElement === document.body) b.focus();
    } else toast(failedText(error, scope));
    return;
  }
  if (session !== sessionSeq) return;
  // On another business's page the decision is only reported, by name. That page records
  // nothing for it, is not behind, and keeps its view and focus.
  if (state.bid !== scope.bid) {
    toast(savedText(scope));
    return;
  }
  // Accepted. Close this decision's form if it is still the one open; until a reload shows the
  // step as it now is, the step says the decision is recorded instead of offering its moves.
  state.accepted[id] = f.get('state');
  if (state.open === open) state.open = null;
  await reloadAfter(undefined, scope);
  if (session === sessionSeq && state.bid === scope.bid) restoreFocus(memo);
}

/* ---------------- events ---------------- */
async function handle(a, el) {
  const id = el.dataset.id;
  if (a === 'scenario') {
    state.scenario = id;
    rerender(renderInspector);
  } else if (a === 'close-dialog') $('#dialog').close();
  else if (a === 'reload') {
    // The notice's control and the one on a step whose decision is recorded do the same.
    const memo = focusMemo();
    el.disabled = true;
    try {
      if (await refresh()) {
        // focus left with the control: to its region's place, or, from the notice, to the list
        restoreFocus(memo);
        if (document.activeElement === document.body || !visible(document.activeElement))
          focusFirst(RESTING_FOCUS);
      }
    } catch (e) {
      reloadFailed(e, true);
    } finally {
      el.disabled = false;
      // a reload that failed again leaves focus on the control, so it can be pressed again
      if (el.isConnected && visible(el) && document.activeElement === document.body) el.focus();
    }
  } else if (a === 'dismiss-outdated') {
    $('#outdated').hidden = true;
    // the control that had focus is gone with the notice
    if (document.activeElement === document.body || !visible(document.activeElement))
      focusFirst(RESTING_FOCUS);
  } else if (a === 'logout') {
    const session = sessionSeq,
      cancel = new AbortController();
    signingOut.add(cancel);
    try {
      await api('/logout', {
        method: 'POST',
        signal: cancel.signal,
        // signing out twice is safe: the second either ends the session or is refused as signed out
        interrupted:
          'Connection interrupted, so this page cannot tell whether you were signed out. Signing out again is safe.',
      });
    } catch (error) {
      // cancelled because the session ended another way: nothing to say
      if (cancel.signal.aborted) return;
      throw error;
    } finally {
      signingOut.delete(cancel);
    }
    // an answer for a session that has already ended (a 401 meanwhile) ends nothing newer
    if (session === sessionSeq) showLogin();
  } else if (a === 'new-business') businessDialog();
  else if (a === 'edit-business') businessDialog(true);
  else if (a === 'new-record') recordDialog();
  else if (a === 'record') recordDialog(id);
  else if (a === 'new-evidence') newEvidence();
  else if (a === 'review-evidence') reviewEvidence(id);
  else if (a === 'revoke-evidence') reviewEvidence(id, true);
  else if (a === 'tx') {
    state.open = { kind: 'tx', id, to: el.dataset.to, key: crypto.randomUUID() };
    renderInspector();
    $('#tx-form textarea')?.focus();
  } else if (a === 'close-inline') {
    const was = state.open;
    state.open = null;
    rerender(renderInspector);
    // back to the move that opened the form
    if (was?.kind === 'tx')
      focusFirst([`#insp [data-action="tx"][data-to="${CSS.escape(was.to)}"]`, '#insp-title']);
  } else if (a === 'new-forecast') forecastDialog();
  else if (a === 'new-deal') dealDialog(id);
  else if (a === 'new-document') documentDialog();
  else if (a === 'investment-memo') documentDialog(id);
  else if (a === 'document') {
    const o = find(id);
    showDialog(
      o.title,
      `${o.stale ? '<div class="flag"><span></span><div>The records this draft was made from have changed since it was generated.</div></div>' : ''}<pre class="document">${h(o.data.content)}</pre><div class="i-actions"><a class="btn" href="/api/businesses/${h(state.bid)}/documents/${h(id)}/download">Download Markdown</a></div>`,
      { bid: state.bid },
    );
  } else if (a === 'inspect') {
    const o = find(id);
    showDialog(o.title, `<pre>${h(JSON.stringify(o.data, null, 2))}</pre>`, { bid: state.bid });
  } else if (a === 'new-invoice') invoiceDialog();
  else if (a === 'issue-invoice') issueDialog(id);
  else if (a === 'new-payment') paymentDialog(id);
  else if (a === 'new-key') keyDialog();
  else if (a === 'revoke-key') {
    formDialog(
      'Revoke access key',
      '<p class="full">This immediately prevents the selected key and its sessions from accessing this server.</p>',
      async () =>
        api('/principals/' + id, {
          method: 'DELETE',
          // revoking a key that is already revoked reports it revoked again
          interrupted:
            'Connection interrupted, so this page cannot tell whether the key was revoked. Revoking it again is safe.',
        }),
      'Revoke key',
      '',
      null,
      { free: true },
    );
  } else if (a === 'ai-review') aiDialog();
}
document.addEventListener('click', (event) => {
  const action = event.target.closest('[data-action]');
  if (action) {
    // a failure that arrives after its session ended (a 401 ends it) has nothing left to say
    const session = sessionSeq;
    handle(action.dataset.action, action).catch((e) => {
      if (session === sessionSeq) toast(messageFor(e));
    });
    return;
  }
  const link = event.target.closest('[data-go]');
  if (link) {
    go(link.dataset.go);
    return;
  }
  const src = event.target.closest('.src');
  if (src) {
    setSource(src.dataset.src);
    setLevel(src.dataset.src === 'journey' && isPhone() ? 'detail' : 'list');
    return;
  }
  if (event.target.closest('#mini')) {
    setSource('journey', 'map:all');
    setLevel('detail');
    return;
  }
  const r = event.target.closest('#list .row');
  if (r) {
    select(r.dataset.key);
    setLevel('detail');
    return;
  }
  const stn = event.target.closest('#insp .stn');
  if (stn) pickStation(stn.dataset.id);
});
$('#list').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  const rows = [...document.querySelectorAll('#list .row:not([hidden])')];
  const i = rows.indexOf(document.activeElement);
  if (i < 0) return;
  const nxt = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))];
  e.preventDefault();
  nxt.focus();
  if (!isPhone()) select(nxt.dataset.key);
});
$('#insp').addEventListener('keydown', (e) => {
  const stn = e.target.closest('.stn');
  if (!stn) return;
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    pickStation(stn.dataset.id);
  }
});
$('#insp').addEventListener('change', (e) => {
  if (e.target.matches('#tx-form select[name="state"]')) {
    const t = find($('#tx-form').dataset.id);
    $('#tx-text').textContent = moveText(e.target.value, t);
  }
});
document.addEventListener('submit', (e) => {
  if (e.target.id === 'tx-form') {
    e.preventDefault();
    submitTransition(e.target);
  }
});
$('#back-src').addEventListener('click', () => setLevel('sources'));
$('#back-list').addEventListener('click', () => setLevel('list'));
$('#business-select').addEventListener('change', async (event) => {
  // The records on screen stay those of the business they belong to until the chosen one's
  // records arrive; refresh() then switches both together.
  state.wantBid = event.target.value || null;
  try {
    await refresh();
  } catch (e) {
    reloadFailed(e);
  }
});
if (fitter) fitter.observe($('#insp'));
$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target,
    b = form.querySelector('button');
  b.disabled = true;
  $('#login-error').textContent = '';
  try {
    await api('/login', {
      method: 'POST',
      body: { token: new FormData(form).get('token') },
      // a second sign-in only opens another session
      interrupted: 'Connection interrupted. Signing in again is safe.',
    });
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
    app.hidden = false;
    await refresh();
  } catch (e) {
    showLogin();
    if (e.message !== 'Sign in with a valid access key.') $('#login-error').textContent = e.message;
  }
}
start();
