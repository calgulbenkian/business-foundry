'use strict';
/*
  Palette check for the Studio interface. Everything is measured from the custom properties in
  src/foundry/static/style.css and the line keys in app.js, never from a copied table:

  - every two line colours differ by at least 6 CIE L*, so the lines stay apart in greyscale;
  - every line's two-letter code reaches 4.5:1 against its own disc;
  - under simulated protanopia, deuteranopia and tritanopia no two lines come closer than
    CVD_FLOOR in CIEDE2000;
  - a line that is faint against a theme's surface carries a casing that stands off it;
  - the main text roles reach 4.5:1 on the grounds they sit on, light and dark;
  - the measured block in the stylesheet's header comment matches these figures exactly.

  `node tests/palette.cjs` checks. `node tests/palette.cjs --write` rewrites the header block.
*/
const { readFileSync, writeFileSync } = require('node:fs');
const { join } = require('node:path');

const ROOT = join(__dirname, '..');
const CSS_PATH = join(ROOT, 'src/foundry/static/style.css');
const APP_PATH = join(ROOT, 'src/foundry/static/app.js');

const MIN_LIGHTNESS_GAP = 6; // CIE L*, between any two lines
const MIN_CODE_CONTRAST = 4.5; // WCAG 2 AA, normal text
const MIN_TEXT_CONTRAST = 4.5;
// The five original lines already sit at 7.8 (red and green under deuteranopia); no added line
// may bring any pair closer than that floor allows.
const CVD_FLOOR = 7.5;
const FAINT_LINE = 2; // a line under 2:1 against the surface needs a casing...
const MIN_CASING_CONTRAST = 3; // ...that itself reaches the WCAG non-text 3:1

const BEGIN = '  ---- measured by tests/palette.cjs (npm run palette -- --write): begin ----';
const END = '  ---- measured by tests/palette.cjs: end ----';

/* ---------- colour science ---------- */
function parseHex(value) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value.trim());
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = [...h].map((c) => c + c).join('');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
}
const toLinear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const luminance = ([r, g, b]) => 0.2126 * r + 0.7152 * g + 0.0722 * b; // linear sRGB in
function contrast(a, b) {
  const x = luminance(a.map(toLinear)),
    y = luminance(b.map(toLinear));
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
const labF = (t) => (t > 216 / 24389 ? Math.cbrt(t) : ((24389 / 27) * t + 16) / 116);
/* CIE L*a*b* (D65) from linear sRGB. */
function labFromLinear([r, g, b]) {
  const X = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b,
    Y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b,
    Z = 0.0193339 * r + 0.119192 * g + 0.9503041 * b;
  const fx = labF(X / 0.95047),
    fy = labF(Y),
    fz = labF(Z / 1.08883);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}
const lab = (rgb) => labFromLinear(rgb.map(toLinear));
/* Machado, Oliveira and Fernandes (2009), severity 1.0, applied in linear sRGB. */
const MACHADO = {
  protanopia: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  deuteranopia: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
  tritanopia: [
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930809, 0.147602],
    [0.004733, 0.691367, 0.3039],
  ],
};
function simulatedLab(rgb, kind) {
  const c = rgb.map(toLinear);
  const out = MACHADO[kind].map((row) =>
    Math.min(1, Math.max(0, row[0] * c[0] + row[1] * c[1] + row[2] * c[2])),
  );
  return labFromLinear(out);
}
/* CIEDE2000 (Sharma, Wu and Dalal 2005). */
function deltaE2000([L1, a1, b1], [L2, a2, b2]) {
  const rad = Math.PI / 180;
  const C1 = Math.hypot(a1, b1),
    C2 = Math.hypot(a2, b2),
    Cb7 = ((C1 + C2) / 2) ** 7;
  const G = 0.5 * (1 - Math.sqrt(Cb7 / (Cb7 + 25 ** 7)));
  const a1p = (1 + G) * a1,
    a2p = (1 + G) * a2;
  const C1p = Math.hypot(a1p, b1),
    C2p = Math.hypot(a2p, b2);
  const hue = (a, b) => {
    if (a === 0 && b === 0) return 0;
    const x = Math.atan2(b, a) / rad;
    return x < 0 ? x + 360 : x;
  };
  const h1p = hue(a1p, b1),
    h2p = hue(a2p, b2);
  let dh = 0;
  if (C1p * C2p !== 0) {
    dh = h2p - h1p;
    if (dh > 180) dh -= 360;
    else if (dh < -180) dh += 360;
  }
  const dL = L2 - L1,
    dC = C2p - C1p,
    dH = 2 * Math.sqrt(C1p * C2p) * Math.sin((dh * rad) / 2);
  const Lb = (L1 + L2) / 2,
    Cbp = (C1p + C2p) / 2;
  let hb = h1p + h2p;
  if (C1p * C2p !== 0) {
    if (Math.abs(h1p - h2p) > 180) hb = h1p + h2p < 360 ? (hb + 360) / 2 : (hb - 360) / 2;
    else hb = hb / 2;
  }
  const T =
    1 -
    0.17 * Math.cos((hb - 30) * rad) +
    0.24 * Math.cos(2 * hb * rad) +
    0.32 * Math.cos((3 * hb + 6) * rad) -
    0.2 * Math.cos((4 * hb - 63) * rad);
  const dTheta = 30 * Math.exp(-(((hb - 275) / 25) ** 2));
  const Rc = 2 * Math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7));
  const Sl = 1 + (0.015 * (Lb - 50) ** 2) / Math.sqrt(20 + (Lb - 50) ** 2),
    Sc = 1 + 0.045 * Cbp,
    Sh = 1 + 0.015 * Cbp * T,
    Rt = -Math.sin(2 * dTheta * rad) * Rc;
  return Math.sqrt((dL / Sl) ** 2 + (dC / Sc) ** 2 + (dH / Sh) ** 2 + Rt * (dC / Sc) * (dH / Sh));
}

/* ---------- reading the stylesheet ---------- */
function declarations(body) {
  const out = new Map();
  const re = /--([\w-]+)\s*:\s*([^;]+);(?:[ \t]*\/\*\s*(.*?)\s*\*\/)?/g;
  let m;
  while ((m = re.exec(body))) out.set(m[1], { value: m[2].trim(), note: m[3] || null });
  return out;
}
function readThemes(css) {
  const light = /^:root \{([^}]*)\}/m.exec(css);
  const dark = /@media \(prefers-color-scheme: dark\) \{\s*:root \{([^}]*)\}/.exec(css);
  if (!light || !dark) throw new Error('style.css: could not find the light and dark :root blocks');
  const lightDecl = declarations(light[1]);
  const darkDecl = new Map([...lightDecl, ...declarations(dark[1])]);
  return { light: lightDecl, dark: darkDecl };
}
/* Resolve a token to an [r, g, b] triple, or null for transparent. */
function resolve(theme, name, seen = new Set()) {
  const d = theme.get(name);
  if (!d) throw new Error(`style.css: --${name} is not defined`);
  if (seen.has(name)) throw new Error(`style.css: --${name} refers to itself`);
  seen.add(name);
  const v = d.value;
  if (v === 'transparent') return null;
  const ref = /^var\(--([\w-]+)\)$/.exec(v);
  if (ref) return resolve(theme, ref[1], seen);
  const rgb = parseHex(v);
  if (!rgb) throw new Error(`style.css: --${name} is "${v}", not a hex colour or transparent`);
  return rgb;
}
/* Every innermost rule as { selector, body }, comments removed. */
function rules(css) {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(bare))) out.push({ selector: m[1].trim().replace(/\s+/g, ' '), body: m[2] });
  return out;
}
const hexOut = (rgb) =>
  '#' +
  rgb
    .map((c) =>
      Math.round(c * 255)
        .toString(16)
        .padStart(2, '0'),
    )
    .join('')
    .toUpperCase();

/* ---------- the text roles ---------- */
// Grounds the panes, rows and inspector paint. 'source-hover' is how .src:hover is painted:
// ink at 6% over the source list.
const PANE_GROUNDS = ['surface', 'source', 'toolbar', 'raised', 'hl', 'review-bg', 'source-hover'];
// Body text that can sit on any pane ground. The narrow layout's back control is a link coloured
// --sel in light and --focus in dark (see the max-width: 699px rules).
const PANE_TEXT = [
  { role: 'ink', light: 'ink', dark: 'ink' },
  { role: 'ink-2', light: 'ink-2', dark: 'ink-2' },
  { role: 'bad', light: 'bad', dark: 'bad' },
  // Amber text appears only in the inspector; the source list shows amber as a glyph alone.
  {
    role: 'c-review (amber text)',
    light: 'c-review',
    dark: 'c-review',
    except: ['source', 'source-hover'],
  },
  { role: 'back link', light: 'sel', dark: 'focus' },
];
// Text on the selected source and on a pressed button.
const SEL_SOFT_TEXT = ['ink', 'ink-2', 'btn-ink'];
// Pairs whose text and ground are set by different rules, so the rule scan below cannot see them.
const PAIRS = [
  ['sel-ink-2', 'sel'],
  ['bad', 'btn'],
];
// Tokens that appear in a `color:` declaration but are not text. They are listed so that a new
// text colour cannot slip past this check unclassified.
const NOT_TEXT = {
  'bul-ink': 'line codes, checked with the lines',
  'tag-bg': 'the your-move icon',
  'c-verified': 'state glyph',
  'c-submitted': 'state glyph',
  'c-approved': 'state glyph',
  'c-prepared': 'state glyph',
  'c-progress': 'state glyph',
  'c-na': 'state glyph',
  'c-none': 'state glyph',
  'c-review': 'state glyph and amber text (amber text is checked above)',
  faint: 'state glyph',
  'sel-ink': 'selected row text, checked through its rule',
};

function ground(theme, name) {
  if (name === 'source-hover') {
    const ink = resolve(theme, 'ink'),
      src = resolve(theme, 'source');
    return src.map((c, i) => 0.06 * ink[i] + 0.94 * c);
  }
  return resolve(theme, name);
}

/* ---------- measuring ---------- */
function measure(css, app) {
  const failures = [];
  const fail = (msg) => failures.push(msg);
  const themes = readThemes(css);
  const palette = /const PALETTE = \[([^\]]*)\]/.exec(app);
  if (!palette) throw new Error('app.js: could not find const PALETTE');
  const keys = [...palette[1].matchAll(/'([a-z])'/g)].map((m) => m[1]);
  if (keys.length < 2) throw new Error('app.js: PALETTE lists fewer than two lines');

  const lines = keys.map((k) => {
    const decl = themes.light.get('l-' + k);
    const rgb = resolve(themes.light, 'l-' + k);
    if (!rgb) throw new Error(`style.css: --l-${k} is transparent`);
    const ink = resolve(themes.light, `l-${k}-ink`);
    for (const t of ['dark']) {
      if (hexOut(resolve(themes[t], 'l-' + k)) !== hexOut(rgb))
        fail(`--l-${k} changes in the ${t} theme; a line keeps one colour in both`);
    }
    const L = lab(rgb);
    return {
      k,
      name: decl.note || k,
      rgb,
      hex: hexOut(rgb),
      lab: L,
      ink,
      code: contrast(rgb, ink),
      cases: Object.fromEntries(
        ['light', 'dark'].map((t) => {
          const casing = resolve(themes[t], 'case-' + k);
          const surface = resolve(themes[t], 'surface');
          return [
            t,
            {
              rgb: casing,
              line: contrast(rgb, surface),
              casing: casing ? contrast(casing, surface) : null,
            },
          ];
        }),
      ),
    };
  });

  // 1. Lightness ladder, across every pair.
  let closest = null;
  for (let i = 0; i < lines.length; i++)
    for (let j = i + 1; j < lines.length; j++) {
      const gap = Math.abs(lines[i].lab[0] - lines[j].lab[0]);
      if (!closest || gap < closest.gap) closest = { gap, a: lines[i], b: lines[j] };
      if (gap < MIN_LIGHTNESS_GAP)
        fail(
          `${lines[i].name} (--l-${lines[i].k}) and ${lines[j].name} (--l-${lines[j].k}) are ` +
            `${gap.toFixed(2)} L* apart; every two lines need ${MIN_LIGHTNESS_GAP}`,
        );
    }
  // 2. Each bullet's code against its own disc.
  for (const l of lines)
    if (l.code < MIN_CODE_CONTRAST)
      fail(`${l.name}: its code reaches ${l.code.toFixed(2)}:1, under ${MIN_CODE_CONTRAST}:1`);
  // 3. Simulated colour vision deficiency.
  const cvd = {};
  for (const kind of Object.keys(MACHADO)) {
    const sim = lines.map((l) => simulatedLab(l.rgb, kind));
    let worst = null;
    for (let i = 0; i < lines.length; i++)
      for (let j = i + 1; j < lines.length; j++) {
        const d = deltaE2000(sim[i], sim[j]);
        if (!worst || d < worst.d) worst = { d, a: lines[i], b: lines[j] };
      }
    cvd[kind] = worst;
    if (worst.d < CVD_FLOOR)
      fail(
        `under simulated ${kind}, ${worst.a.name} and ${worst.b.name} are ` +
          `${worst.d.toFixed(2)} apart in CIEDE2000, under ${CVD_FLOOR}`,
      );
  }
  // 4. Faint lines carry a casing, in each theme.
  for (const l of lines)
    for (const t of ['light', 'dark']) {
      const c = l.cases[t];
      if (c.line < FAINT_LINE && !(c.casing && c.casing >= MIN_CASING_CONTRAST))
        fail(
          `${l.name} is ${c.line.toFixed(2)}:1 against the ${t} surface and needs --case-${l.k} ` +
            `reaching ${MIN_CASING_CONTRAST}:1 there`,
        );
    }
  // 5. Every line is wired through its own tokens wherever the stylesheet paints it.
  const all = rules(css);
  const ruleBody = (selector) => all.filter((r) => r.selector === selector).map((r) => r.body);
  const wants = (selector, prop, token) => {
    const re = new RegExp(`(^|[;\\s])${prop}\\s*:\\s*var\\(--${token}\\)`);
    if (!ruleBody(selector).some((b) => re.test(b)))
      fail(`style.css: ${selector} must set ${prop}: var(--${token})`);
  };
  for (const l of lines) {
    wants(`.lc-${l.k}`, '--lc', `l-${l.k}`);
    wants(`.lc-${l.k}`, '--lcase', `case-${l.k}`);
    wants(`.lc-${l.k}`, '--bul-ink', `l-${l.k}-ink`);
    wants(`.mp .ln-${l.k}`, 'stroke', `l-${l.k}`);
    wants(`.mp .case-${l.k}`, 'stroke', `case-${l.k}`);
    wants(`.mp .bd-${l.k}`, 'fill', `l-${l.k}`);
    wants(`.mp .bt-${l.k}`, 'fill', `l-${l.k}-ink`);
    if (l.cases.light.rgb || l.cases.dark.rgb) wants(`.mp .bd-${l.k}`, 'stroke', `case-${l.k}`);
  }
  for (const r of all)
    if (/(^|,\s*)\.mp \.bt\b/.test(r.selector) && /fill\s*:\s*#/.test(r.body))
      fail(`style.css: ${r.selector} hard-codes a code colour; use --l-<key>-ink`);
  if (!/<path class="case case-\$\{l\.k\}"/.test(app) || /const cased\b/.test(app))
    fail('app.js: every map line must draw its casing path; the stylesheet decides its colour');

  // 6. Text roles, light and dark.
  const text = [];
  const check = (theme, role, fg, groundName, rgbFg, rgbGround) => {
    const ratio = contrast(rgbFg, rgbGround);
    text.push({ theme, role, fg, ground: groundName, ratio });
    if (ratio < MIN_TEXT_CONTRAST)
      fail(
        `${theme}: ${role} (--${fg}) on --${groundName} is ${ratio.toFixed(2)}:1, ` +
          `under ${MIN_TEXT_CONTRAST}:1`,
      );
  };
  for (const t of ['light', 'dark']) {
    const T = themes[t];
    for (const r of PANE_TEXT)
      for (const gName of PANE_GROUNDS.filter((x) => !(r.except || []).includes(x)))
        check(t, r.role, r[t], gName, resolve(T, r[t]), ground(T, gName));
    for (const fg of SEL_SOFT_TEXT)
      check(t, fg, fg, 'sel-soft', resolve(T, fg), resolve(T, 'sel-soft'));
    for (const [fg, bg] of PAIRS) check(t, fg, fg, bg, resolve(T, fg), resolve(T, bg));
    // Any rule that sets both a text colour and a background is checked as written.
    for (const r of all) {
      const fg = /(?:^|[;\s])color\s*:\s*var\(--([\w-]+)\)/.exec(r.body);
      const bg = /(?:^|[;\s])background(?:-color)?\s*:\s*var\(--([\w-]+)\)/.exec(r.body);
      if (!fg || !bg || fg[1] === 'bul-ink') continue;
      check(t, r.selector, fg[1], bg[1], resolve(T, fg[1]), resolve(T, bg[1]));
    }
  }
  // Every token used as a text colour is either checked above or declared not to be text.
  const checked = new Set(text.map((x) => x.fg));
  const colourTokens = new Set(
    [...css.matchAll(/(?:^|[;\s{])color\s*:\s*var\(--([\w-]+)\)/g)].map((m) => m[1]),
  );
  for (const tok of colourTokens)
    if (!checked.has(tok) && !(tok in NOT_TEXT))
      fail(`style.css: --${tok} is used as a text colour but the palette check does not cover it`);
  for (const r of all)
    if (/(?:^|[;\s])color\s*:\s*#/.test(r.body))
      fail(`style.css: ${r.selector} sets a literal text colour; use a token the check covers`);

  return { lines, closest, cvd, text, failures };
}

/* ---------- the header block ---------- */
const pad = (s, n) => String(s).padEnd(n);
function headerBlock(m) {
  const out = [BEGIN];
  out.push('  Lines, darkest first: CIE L* and the contrast of the two-letter code on its disc.');
  for (const l of [...m.lines].sort((a, b) => a.lab[0] - b.lab[0])) {
    const casing = ['light', 'dark']
      .filter((t) => l.cases[t].rgb)
      .map((t) => `cased on ${t} ${hexOut(l.cases[t].rgb)}`)
      .join(', ');
    out.push(
      `    ${pad(l.name, 7)} --l-${l.k}  ${l.hex}  L* ${l.lab[0].toFixed(1).padStart(4)}` +
        `  code ${hexOut(l.ink)} ${l.code.toFixed(2).padStart(5)}:1${casing ? '  ' + casing : ''}`,
    );
  }
  out.push(
    `  Closest lightness between any two lines: ${m.closest.gap.toFixed(1)} L* ` +
      `(${m.closest.a.name}, ${m.closest.b.name}).`,
  );
  out.push(
    '  Closest CIEDE2000 between any two lines under simulated colour vision deficiency',
    '  (Machado, Oliveira and Fernandes 2009, full severity):',
  );
  for (const [kind, w] of Object.entries(m.cvd))
    out.push(`    ${pad(kind, 12)} ${w.d.toFixed(1).padStart(4)}  (${w.a.name}, ${w.b.name})`);
  for (const t of ['light', 'dark']) {
    const low = m.text.filter((x) => x.theme === t).sort((a, b) => a.ratio - b.ratio)[0];
    out.push(
      `  Lowest text contrast, ${t}: ${low.ratio.toFixed(2)}:1 (--${low.fg} on --${low.ground}).`,
    );
  }
  out.push(END);
  return out.join('\n');
}
function withHeader(css, block) {
  const start = css.indexOf(BEGIN),
    end = css.indexOf(END);
  if (start < 0 || end < start)
    throw new Error('style.css: the header comment has no measured block to compare');
  return css.slice(0, start) + block + css.slice(end + END.length);
}

/* The colour maths, against published reference values, before it is trusted with the palette. */
function selfTest() {
  const near = (got, want, tol, what) => {
    if (Math.abs(got - want) > tol)
      throw new Error(`self-test: ${what} is ${got}, expected ${want}`);
  };
  // Sharma, Wu and Dalal (2005), CIEDE2000 test data, pairs 1, 7, 17, 25 and 34.
  for (const [a, b, want] of [
    [[50, 2.6772, -79.7751], [50, 0, -82.7485], 2.0425],
    [[50, 0, 0], [50, -1, 2], 2.3669],
    [[50, 2.5, 0], [73, 25, -18], 27.1492],
    [[60.2574, -34.0099, 36.2677], [60.4626, -34.1751, 39.4387], 1.2644],
    [[2.0776, 0.0795, -1.135], [0.9033, -0.0636, -0.5514], 0.9082],
  ])
    near(deltaE2000(a, b), want, 1e-4, `CIEDE2000 ${a} / ${b}`);
  // WCAG 2 contrast: black on white is 21:1; #767676 on white is the familiar 4.54:1.
  near(contrast([0, 0, 0], [1, 1, 1]), 21, 1e-9, 'contrast of black on white');
  near(contrast(parseHex('#767676'), [1, 1, 1]), 4.54, 0.005, 'contrast of #767676 on white');
  // CIE L*: white is 100, black 0, and the sRGB mid-grey #777777 is 50.0.
  near(lab([1, 1, 1])[0], 100, 0.01, 'L* of white');
  near(lab([0, 0, 0])[0], 0, 1e-9, 'L* of black');
  near(lab(parseHex('#777777'))[0], 50.0, 0.1, 'L* of #777777');
  // Each Machado matrix keeps white white and greys grey.
  for (const kind of Object.keys(MACHADO)) {
    near(simulatedLab([1, 1, 1], kind)[0], 100, 0.5, `${kind} of white`);
    const grey = simulatedLab(parseHex('#777777'), kind);
    near(Math.hypot(grey[1], grey[2]), 0, 1, `${kind} chroma of grey`);
  }
}

function main() {
  selfTest();
  const css = readFileSync(CSS_PATH, 'utf8'),
    app = readFileSync(APP_PATH, 'utf8');
  const m = measure(css, app);
  const block = headerBlock(m);
  if (process.argv.includes('--write')) {
    writeFileSync(CSS_PATH, withHeader(css, block));
    console.log('style.css: header block rewritten');
  } else if (withHeader(css, block) !== css) {
    m.failures.push(
      'style.css: the measured block in the header comment is stale; run npm run palette -- --write',
    );
  }
  console.log(block.split('\n').slice(1, -1).join('\n'));
  if (m.failures.length) {
    console.error('\nPalette check failed:\n- ' + m.failures.join('\n- '));
    process.exit(1);
  }
  console.log(
    `\nPalette check passed: ${m.lines.length} lines, ${m.text.length} text pairs measured.`,
  );
}

if (require.main === module) main();
module.exports = { contrast, lab, simulatedLab, deltaE2000, parseHex, measure };
