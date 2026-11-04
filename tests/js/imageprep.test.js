// Run with: node --test tests/js/
const test = require('node:test');
const assert = require('node:assert/strict');
const P = require('../../app/static/js/imageprep.js');

test('fitWithin preserves aspect ratio', () => {
  assert.deepEqual(P.fitWithin(4032, 3024, 2048), { width: 2048, height: 1536 });
  assert.deepEqual(P.fitWithin(3024, 4032, 640), { width: 480, height: 640 });
  assert.deepEqual(P.fitWithin(8000, 1000, 640), { width: 640, height: 80 });
  const r = P.fitWithin(5183, 3456, 2048);
  assert.ok(Math.abs(r.width / r.height - 5183 / 3456) < 0.001);
});

test('fitWithin never upscales', () => {
  assert.deepEqual(P.fitWithin(500, 300, 640), { width: 500, height: 300 });
  assert.deepEqual(P.fitWithin(640, 640, 640), { width: 640, height: 640 });
  assert.deepEqual(P.fitWithin(1, 5000, 640), { width: 1, height: 640 });
});

const bytes = (...parts) => Uint8Array.from(parts.flatMap((p) =>
  typeof p === 'string' ? [...p].map((c) => c.charCodeAt(0)) : p));
const pad = (arr, n = 32) => { const out = new Uint8Array(Math.max(n, arr.length)); out.set(arr); return out; };

test('sniffImageType reads magic bytes, not file names', () => {
  assert.equal(P.sniffImageType(pad(bytes([0xff, 0xd8, 0xff, 0xe0]))), 'image/jpeg');
  assert.equal(P.sniffImageType(pad(bytes([0x89], 'PNG\r\n', [0x1a, 0x0a]))), 'image/png');
  assert.equal(P.sniffImageType(pad(bytes('GIF89a'))), 'image/gif');
  assert.equal(P.sniffImageType(pad(bytes('RIFF', [0, 0, 0, 0], 'WEBPVP8 '))), 'image/webp');
  assert.equal(P.sniffImageType(pad(bytes([0, 0, 0, 24], 'ftypheic', [0, 0, 0, 0], 'mif1heic'))), 'image/heic');
  assert.equal(P.sniffImageType(pad(bytes([0, 0, 0, 24], 'ftypavif', [0, 0, 0, 0], 'mif1miaf'))), 'image/avif');
  assert.equal(P.sniffImageType(pad(bytes('<!doctype html>'))), null);
});

function gif(frames) {
  const frame = [0x21, 0xf9, 0x04, 0, 0, 0, 0, 0, 0x2c, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0x02, 0x02, 0x44, 0x01, 0x00];
  return bytes('GIF89a', [1, 0, 1, 0, 0, 0, 0], ...Array(frames).fill(frame), [0x3b]);
}

test('isAnimated detects multi-frame GIFs only', () => {
  assert.equal(P.isAnimated(gif(1), 'image/gif'), false);
  assert.equal(P.isAnimated(gif(3), 'image/gif'), true);
});

test('isAnimated detects APNG and animated WebP', () => {
  const chunk = (type, len = 0) => [[0, 0, 0, len], type, Array(len + 4).fill(0)];
  const png = (...chunks) => bytes([0x89], 'PNG\r\n', [0x1a, 0x0a], ...chunks.flat());
  assert.equal(P.isAnimated(png(chunk('IHDR', 13), chunk('acTL', 8), chunk('IDAT')), 'image/png'), true);
  assert.equal(P.isAnimated(png(chunk('IHDR', 13), chunk('IDAT')), 'image/png'), false);

  const webp = (flags) => pad(bytes('RIFF', [0, 0, 0, 0], 'WEBPVP8X', [10, 0, 0, 0, flags]));
  assert.equal(P.isAnimated(webp(0x02), 'image/webp'), true);
  assert.equal(P.isAnimated(webp(0x10), 'image/webp'), false);  // alpha only
});

// A fake canvas encoder: returns Blob-like objects of whatever type the
// "browser" supports, the way toBlob does.
function fakeEncoder(supported) {
  const calls = [];
  const encode = (type, quality) => {
    calls.push([type, quality]);
    return Promise.resolve({ type: supported.includes(type) ? type : 'image/png', size: 100 });
  };
  return { encode, calls };
}
const ALLOWED = ['image/webp', 'image/jpeg'];

test('encodeWithFallback keeps WebP when the browser encodes it', async () => {
  P.resetEncoderCache();
  const { encode, calls } = fakeEncoder(['image/webp', 'image/jpeg']);
  const blob = await P.encodeWithFallback(encode, 0.85, 'image/webp', 'image/jpeg', ALLOWED);
  assert.equal(blob.type, 'image/webp');
  assert.deepEqual(calls, [['image/webp', 0.85]]);
});

test('encodeWithFallback never labels PNG output as WebP', async () => {
  P.resetEncoderCache();
  const { encode, calls } = fakeEncoder(['image/jpeg']); // Safari
  const blob = await P.encodeWithFallback(encode, 0.9, 'image/webp', 'image/jpeg', ALLOWED);
  assert.equal(blob.type, 'image/jpeg');
  assert.deepEqual(calls.map((c) => c[0]), ['image/webp', 'image/jpeg']);

  // Learned once: later variants go straight to JPEG.
  calls.length = 0;
  await P.encodeWithFallback(encode, 0.9, 'image/webp', 'image/jpeg', ALLOWED);
  assert.deepEqual(calls.map((c) => c[0]), ['image/jpeg']);
});

test('encodeWithFallback fails loudly if neither format is produced', async () => {
  P.resetEncoderCache();
  const { encode } = fakeEncoder([]);
  await assert.rejects(
    P.encodeWithFallback(encode, 0.9, 'image/webp', 'image/jpeg', ALLOWED),
    /can't encode image\/webp or image\/jpeg \(got image\/png\)/,
  );
});

test('encodeWithFallback rejects a null blob', async () => {
  P.resetEncoderCache();
  await assert.rejects(
    P.encodeWithFallback(() => Promise.resolve(null), 0.9, 'image/webp', 'image/jpeg', ALLOWED),
    /could not encode/,
  );
});

// ----- AreaResampler -----

function resample(src, srcW, srcH, dstW, dstH) {
  const r = new P.AreaResampler(srcW, srcH, dstW, dstH);
  for (let y = 0; y < srcH; y++) r.pushRow(src, y * srcW * 4);
  return r.result();
}

function image(w, h, fn) {
  const d = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) d.set(fn(x, y), (y * w + x) * 4);
  return d;
}

const channel = (img, c = 0) => Array.from({ length: img.width * img.height }, (_, i) => img.data[i * 4 + c]);
const std = (xs) => { const m = xs.reduce((a, b) => a + b) / xs.length; return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / xs.length); };

test('AreaResampler keeps a flat image flat and outputs the target size', () => {
  const out = resample(image(97, 61, () => [10, 200, 30, 255]), 97, 61, 40, 25);
  assert.equal(out.width, 40);
  assert.equal(out.height, 25);
  assert.equal(out.data.length, 40 * 25 * 4);
  assert.ok(channel(out, 0).every((v) => v === 10));
  assert.ok(channel(out, 1).every((v) => v === 200));
});

test('AreaResampler averages detail finer than the output can show', () => {
  // 1 px black/white lines, reduced 1.97x (a 12 MP photo to 2048 px).
  const W = 4032, H = 40;
  const src = image(W, H, (x) => (x % 2 ? [255, 255, 255, 255] : [0, 0, 0, 255]));
  const out = resample(src, W, H, 2048, 20);
  // Bilinear sampling would alias this into visible bands; area averaging
  // leaves it essentially flat grey.
  assert.ok(std(channel(out)) < 3, `std ${std(channel(out))}`);
  assert.ok(Math.abs(channel(out)[100] - 127.5) < 3);
});

test('AreaResampler keeps detail the output can resolve', () => {
  // 8 px period stripes reduced 2x become 4 px stripes, still full contrast.
  const src = image(64, 4, (x) => (x % 8 < 4 ? [255, 255, 255, 255] : [0, 0, 0, 255]));
  const row = channel(resample(src, 64, 4, 32, 2)).slice(0, 8);
  assert.deepEqual(row, [255, 255, 0, 0, 255, 255, 0, 0]);
});

test('AreaResampler uses fractional weights for non-integer ratios', () => {
  // 3 -> 2 px: each output covers 1.5 source pixels.
  const src = image(3, 1, (x) => [[0, 90, 180][x], 0, 0, 255]);
  assert.deepEqual(channel(resample(src, 3, 1, 2, 1)), [30, 150]);
});

test('AreaResampler weights colour by alpha', () => {
  // Opaque red next to transparent black must stay red, not turn dark.
  const src = image(2, 1, (x) => (x === 0 ? [255, 0, 0, 255] : [0, 0, 0, 0]));
  const out = resample(src, 2, 1, 1, 1);
  assert.deepEqual(Array.from(out.data), [255, 0, 0, 128]);
});

test('AreaResampler refuses to upscale or finish early', () => {
  assert.throws(() => new P.AreaResampler(10, 10, 20, 10), /only downscales/);
  const r = new P.AreaResampler(4, 4, 2, 2);
  r.pushRow(new Uint8ClampedArray(16));
  assert.throws(() => r.result(), /1 of 4 rows/);
});

test('shouldReuseOriginal only when nothing was shrunk and re-encoding grew the file', () => {
  const source = { width: 1500, height: 2000 };
  const original = { content_type: 'image/webp', size: 383000 };
  const types = ['image/jpeg', 'image/png', 'image/webp'];
  const v = (w, h, size) => ({ width: w, height: h, size });
  assert.equal(P.shouldReuseOriginal(v(1500, 2000, 460000), source, original, types), true);
  assert.equal(P.shouldReuseOriginal(v(1500, 2000, 300000), source, original, types), false); // re-encode smaller
  assert.equal(P.shouldReuseOriginal(v(480, 640, 500000), source, original, types), false);   // was resized
  assert.equal(P.shouldReuseOriginal(v(1500, 2000, 460000), source, { content_type: 'image/gif', size: 1 }, types), false);
});
