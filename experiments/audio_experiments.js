// Reproducible experiments for the AL/1 audio codec. Run: node experiments/audio_experiments.js
const fs = require('fs');
const html = fs.readFileSync(__dirname + '/../frontend/index.html', 'utf8');
const { enc, dec } = new Function(html.split('//AUDIO')[1] + ';return{enc,dec}')();
let seed = 42; const R = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
const ascii = n => Array.from({ length: n }, () => String.fromCharCode(97 + Math.floor(R() * 26))).join('');
const arabic = n => Array.from({ length: n }, () => String.fromCharCode(0x0621 + Math.floor(R() * 26))).join('');
const gauss = () => Math.sqrt(-2 * Math.log(R() + 1e-12)) * Math.cos(2 * Math.PI * R());
const length = [];
for (const [label, gen, len] of [['ASCII 20', ascii, 20], ['ASCII 50', ascii, 50], ['ASCII 100', ascii, 100], ['ASCII 150', ascii, 150], ['ASCII 200', ascii, 200], ['Arabic 50 chars', arabic, 50]]) {
  let ok = 0, te = 0, td = 0, dur = 0, bytes = 0; const T = 20;
  for (let i = 0; i < T; i++) { const f = 'AL/1|MSG:7F82|TASK:' + gen(len), e = enc(f), d = dec(e.s); ok += (d.ok && d.text === f); te += e.ms; td += d.ms; dur = e.dur; bytes = e.bytes; }
  length.push({ label, bytes, duration_s: +dur.toFixed(2), enc_ms: +(te / T).toFixed(1), dec_ms: +(td / T).toFixed(1), success: `${ok}/${T}` });
}
const f0 = 'AL/1|MSG:7F82|TASK:' + ascii(100), e0 = enc(f0); let p = 0; for (const v of e0.s) p += v * v; const rms = Math.sqrt(p / e0.s.length);
const noise = [];
for (const snr of [30, 20, 10, 5, 0, -5, -10]) {
  const sg = rms / Math.pow(10, snr / 20); let ok = 0; const T = 40;
  for (let t = 0; t < T; t++) { const s = Float32Array.from(e0.s, v => v + sg * gauss()); const d = dec(s); ok += (d.ok && d.text === f0); }
  noise.push({ snr_db: snr, success: ok, trials: T, rate_pct: Math.round(100 * ok / T) });
}
const out = { length, noise }; fs.writeFileSync(__dirname + '/audio_results.json', JSON.stringify(out, null, 1)); console.log(JSON.stringify(out));
