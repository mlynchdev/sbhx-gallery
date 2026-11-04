// Browser-side image preprocessing: decode once, make each variant straight
// from the decoded original, encode to WebP (or JPEG where the browser can't
// encode WebP), and report the real MIME type of every Blob.
//
// Loaded as a plain <script> (window.ImagePrep) and by Node's test runner
// (module.exports). The pure helpers at the top have no DOM dependencies.
(function (root) {
  'use strict';

  // ----- geometry -----

  // Largest size that fits in maxEdge x maxEdge with the same aspect ratio.
  // Never upscales.
  function fitWithin(width, height, maxEdge) {
    var scale = Math.min(1, maxEdge / Math.max(width, height));
    return {
      width: Math.max(1, Math.round(width * scale)),
      height: Math.max(1, Math.round(height * scale)),
    };
  }


  // Streaming area-average (box filter) downscaler. Feed it source rows top to
  // bottom (RGBA bytes); every destination pixel becomes the exact average of
  // the source area it covers, with fractional edge weights. Unlike canvas
  // bilinear scaling, which samples a few pixels per output pixel at
  // reductions under 2x (a 12 MP phone photo to 2048 px is 1.97x), this
  // can't turn fine machining texture into moire. Colour is weighted by alpha
  // so transparent edges don't darken. Memory is one output buffer plus two
  // accumulator rows, whatever the source size.
  function AreaResampler(srcW, srcH, dstW, dstH) {
    if (dstW > srcW || dstH > srcH) throw new Error('AreaResampler only downscales.');
    this.srcW = srcW; this.srcH = srcH; this.dstW = dstW; this.dstH = dstH;
    var sx = srcW / dstW;
    this.sy = srcH / dstH;
    // Horizontal taps: for each destination x, the source pixels it covers.
    var starts = new Int32Array(dstW), counts = new Int32Array(dstW), weights = [];
    for (var x = 0; x < dstW; x++) {
      var a = x * sx, b = (x + 1) * sx;
      var first = Math.floor(a), last = Math.min(srcW - 1, Math.ceil(b) - 1);
      starts[x] = first;
      counts[x] = last - first + 1;
      for (var i = first; i <= last; i++) weights.push((Math.min(b, i + 1) - Math.max(a, i)) / sx);
    }
    this.hStart = starts; this.hCount = counts; this.hWeight = Float32Array.from(weights);
    this.row = new Float32Array(dstW * 4);   // current source row, resampled horizontally
    this.acc = new Float32Array(dstW * 4);   // destination row being accumulated
    this.out = new Uint8ClampedArray(dstW * dstH * 4);
    this.y = 0;      // next source row index
    this.dstRow = 0; // destination row being accumulated
  }

  AreaResampler.prototype.pushRow = function (rgba, offset) {
    var o = offset || 0, row = this.row, hs = this.hStart, hc = this.hCount, hw = this.hWeight;
    var w = 0;
    for (var x = 0, n = this.dstW; x < n; x++) {
      var r = 0, g = 0, b = 0, al = 0;
      for (var k = 0, p = o + hs[x] * 4, c = hc[x]; k < c; k++, p += 4) {
        var wt = hw[w++], aw = rgba[p + 3] * wt;
        r += rgba[p] * aw; g += rgba[p + 1] * aw; b += rgba[p + 2] * aw; al += aw;
      }
      var q = x * 4;
      row[q] = r; row[q + 1] = g; row[q + 2] = b; row[q + 3] = al;
    }
    // Vertically, source row y covers [y, y+1). Split it between the
    // destination rows whose spans it overlaps.
    var y0 = this.y, y1 = y0 + 1;
    while (this.dstRow < this.dstH) {
      var spanEnd = (this.dstRow + 1) * this.sy;
      var part = (Math.min(y1, spanEnd) - Math.max(y0, this.dstRow * this.sy)) / this.sy;
      if (part > 0) for (var i = 0; i < row.length; i++) this.acc[i] += row[i] * part;
      if (spanEnd > y1 + 1e-9) break;
      this._flush();
    }
    this.y = y1;
  };

  AreaResampler.prototype._flush = function () {
    var acc = this.acc, out = this.out, base = this.dstRow * this.dstW * 4;
    for (var q = 0; q < acc.length; q += 4) {
      var al = acc[q + 3];
      if (al > 0) {
        out[base + q] = acc[q] / al;
        out[base + q + 1] = acc[q + 1] / al;
        out[base + q + 2] = acc[q + 2] / al;
      }
      out[base + q + 3] = al;
    }
    acc.fill(0);
    this.dstRow += 1;
  };

  AreaResampler.prototype.result = function () {
    if (this.y !== this.srcH) throw new Error('AreaResampler: ' + this.y + ' of ' + this.srcH + ' rows pushed.');
    return { width: this.dstW, height: this.dstH, data: this.out };
  };

  // A variant that came out at the original's full size (nothing to shrink)
  // and is no smaller than the original is a worse copy of it: point the
  // variant at the original instead of storing a second, bigger file.
  function shouldReuseOriginal(variant, source, original, reusableTypes) {
    return variant.width === source.width && variant.height === source.height &&
      reusableTypes.indexOf(original.content_type) !== -1 && variant.size >= original.size;
  }

  // ----- format sniffing -----

  function ascii(bytes, start, len) {
    return String.fromCharCode.apply(null, bytes.subarray(start, start + len));
  }

  // MIME type from the file's first bytes. file.type comes from the file
  // extension (or is empty), so it can't be trusted to label the upload.
  function sniffImageType(bytes) {
    if (bytes.length < 12) return null;
    if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg';
    if (bytes[0] === 0x89 && ascii(bytes, 1, 3) === 'PNG') return 'image/png';
    if (ascii(bytes, 0, 4) === 'GIF8') return 'image/gif';
    if (ascii(bytes, 0, 4) === 'RIFF' && ascii(bytes, 8, 4) === 'WEBP') return 'image/webp';
    if (ascii(bytes, 4, 4) === 'ftyp') {
      var boxLen = (bytes[0] << 24 | bytes[1] << 16 | bytes[2] << 8 | bytes[3]) >>> 0;
      var brands = [ascii(bytes, 8, 4)];
      for (var i = 16; i + 4 <= Math.min(boxLen, bytes.length); i += 4) brands.push(ascii(bytes, i, 4));
      if (brands.indexOf('avif') !== -1 || brands.indexOf('avis') !== -1) return 'image/avif';
      var heif = ['heic', 'heix', 'hevc', 'hevx', 'heim', 'heis', 'mif1', 'msf1'];
      for (var b = 0; b < brands.length; b++) if (heif.indexOf(brands[b]) !== -1) return 'image/heic';
    }
    return null;
  }

  function gifFrameCount(bytes, stopAt) {
    var i = 13;
    var packed = bytes[10];
    if (packed & 0x80) i += 3 * (1 << ((packed & 0x07) + 1));
    var frames = 0;
    var skipSubBlocks = function () {
      while (i < bytes.length && bytes[i] !== 0) i += bytes[i] + 1;
      i += 1;
    };
    while (i < bytes.length && frames < stopAt) {
      var block = bytes[i];
      if (block === 0x2c) {
        frames += 1;
        var local = bytes[i + 9];
        i += 10;
        if (local & 0x80) i += 3 * (1 << ((local & 0x07) + 1));
        i += 1; // LZW minimum code size
        skipSubBlocks();
      } else if (block === 0x21) {
        i += 2;
        skipSubBlocks();
      } else {
        break; // 0x3b trailer or corrupt data
      }
    }
    return frames;
  }

  function pngIsAnimated(bytes) {
    var i = 8;
    while (i + 8 <= bytes.length) {
      var len = (bytes[i] << 24 | bytes[i + 1] << 16 | bytes[i + 2] << 8 | bytes[i + 3]) >>> 0;
      var type = ascii(bytes, i + 4, 4);
      if (type === 'acTL') return true;
      if (type === 'IDAT') return false;
      i += 12 + len;
    }
    return false;
  }

  // Animated GIF, APNG, animated WebP and AVIF sequences.
  function isAnimated(bytes, mime) {
    if (mime === 'image/gif') return gifFrameCount(bytes, 2) > 1;
    if (mime === 'image/png') return pngIsAnimated(bytes);
    if (mime === 'image/webp') return ascii(bytes, 12, 4) === 'VP8X' && (bytes[20] & 0x02) !== 0;
    if (mime === 'image/avif') return ascii(bytes, 8, 4) === 'avis';
    return false;
  }

  // ----- encoding -----

  var preferredUnsupported = false; // learned once per page load

  // encode(type, quality) -> Promise<Blob|null>. Canvas silently returns PNG
  // when it can't encode the requested type (Safari and WebP), so the Blob's
  // own .type decides what we label it, never the type we asked for.
  function encodeWithFallback(encode, quality, preferred, fallback, allowed) {
    var tryType = function (type) {
      return encode(type, quality).then(function (blob) {
        if (!blob) throw new Error('The browser could not encode the image.');
        return blob;
      });
    };
    var useFallback = function () {
      return tryType(fallback).then(function (blob) {
        if (allowed.indexOf(blob.type) === -1) {
          throw new Error('This browser can\'t encode ' + preferred + ' or ' + fallback + ' (got ' + (blob.type || 'unknown') + ').');
        }
        return blob;
      });
    };
    if (preferredUnsupported) return useFallback();
    return tryType(preferred).then(function (blob) {
      if (blob.type === preferred) return blob;
      preferredUnsupported = true;
      return useFallback();
    });
  }

  function resetEncoderCache() { preferredUnsupported = false; }

  // ----- browser-only helpers -----

  function releaseCanvas(canvas) {
    canvas.width = 0;
    canvas.height = 0;
  }

  function makeCanvas(width, height) {
    var c = document.createElement('canvas');
    c.width = width;
    c.height = height;
    return c;
  }

  // Decode once at full size. createImageBitmap with imageOrientation
  // 'from-image' applies EXIF orientation (a portrait phone photo stored
  // sideways with Orientation=6 comes out upright, and width/height are the
  // rotated size) and decodes off the main thread even in a background tab,
  // where Chromium defers <img>.decode() indefinitely. <img> is the fallback
  // for browsers that reject the options; it also applies EXIF orientation
  // when drawn to a canvas.
  function decodeImage(file) {
    var viaImg = function () {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.decoding = 'async';
      img.src = url;
      return img.decode().then(function () {
        return {
          source: img,
          width: img.naturalWidth,
          height: img.naturalHeight,
          release: function () { img.src = ''; URL.revokeObjectURL(url); },
        };
      }, function (err) {
        URL.revokeObjectURL(url);
        throw err;
      });
    };
    if (typeof createImageBitmap !== 'function') return viaImg();
    return createImageBitmap(file, { imageOrientation: 'from-image' }).then(function (bmp) {
      return { source: bmp, width: bmp.width, height: bmp.height, release: function () { bmp.close(); } };
    }, viaImg);
  }

  // A macrotask that timer throttling in background tabs doesn't delay.
  function yieldToBrowser() {
    return new Promise(function (resolve) {
      var ch = new MessageChannel();
      ch.port1.onmessage = function () { ch.port1.close(); resolve(); };
      ch.port2.postMessage(0);
    });
  }

  // Read the decoded original in horizontal strips (about 4 MP each, well
  // under iOS Safari's canvas limit) and feed every row to each resampler.
  // All variants come straight from the original's pixels in one pass.
  function resampleAll(decoded, sizes, onProgress) {
    var W = decoded.width, H = decoded.height;
    var resamplers = sizes.map(function (t) { return new AreaResampler(W, H, t.width, t.height); });
    var stripRows = Math.max(1, Math.min(H, Math.floor(4e6 / W)));
    var strip = makeCanvas(W, stripRows);
    var ctx = strip.getContext('2d', { willReadFrequently: true });
    var y = 0;
    var step = function () {
      if (y >= H) {
        releaseCanvas(strip);
        return Promise.resolve(resamplers.map(function (r) { return r.result(); }));
      }
      var rows = Math.min(stripRows, H - y);
      ctx.clearRect(0, 0, W, rows);
      ctx.drawImage(decoded.source, 0, y, W, rows, 0, 0, W, rows);
      var data = ctx.getImageData(0, 0, W, rows).data;
      for (var r = 0; r < rows; r++) {
        for (var i = 0; i < resamplers.length; i++) resamplers[i].pushRow(data, r * W * 4);
      }
      y += rows;
      onProgress(y / H);
      return yieldToBrowser().then(step);
    };
    return step().catch(function (err) { releaseCanvas(strip); throw err; });
  }

  function imageDataCanvas(img) {
    var canvas = makeCanvas(img.width, img.height);
    canvas.getContext('2d').putImageData(new ImageData(img.data, img.width, img.height), 0, 0);
    return canvas;
  }

  function canvasEncoder(canvas) {
    return function (type, quality) {
      var src = canvas;
      if (type === 'image/jpeg') {
        // JPEG has no alpha: put transparent pixels on white, not black.
        src = makeCanvas(canvas.width, canvas.height);
        var ctx = src.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, src.width, src.height);
        ctx.drawImage(canvas, 0, 0);
      }
      return new Promise(function (resolve) {
        src.toBlob(function (blob) {
          if (src !== canvas) releaseCanvas(src);
          resolve(blob);
        }, type, quality);
      });
    };
  }

  var HEIC_HELP = 'This browser can\'t read HEIC/HEIF photos. Export the photo as JPEG and upload that, or use ' +
    'Safari. On an iPhone, tap the upload area and pick from Photos; iOS converts HEIC to JPEG for you.';

  // Returns { original, variants[], animated, width, height }. One full-size
  // decode, one strip canvas while resizing, then one canvas per encode.
  function prepareImage(file, config, onStatus) {
    var status = onStatus || function () {};
    var original, decoded;
    status('Reading ' + file.name + '...');
    var type;
    return file.slice(0, 65536).arrayBuffer().then(function (head) {
      type = sniffImageType(new Uint8Array(head));
      if (!type) throw new Error('This file isn\'t a recognized image format. Use JPEG, PNG, WebP, GIF or HEIC.');
      if (config.originalTypes.indexOf(type) === -1) throw new Error('Unsupported image format (' + type + ').');
      if (file.size > config.maxOriginalBytes) throw new Error('This file is over the upload limit.');
      // GIF frames can be anywhere in the file; the other formats flag
      // animation in their header.
      return type === 'image/gif' ? file.arrayBuffer() : head;
    }).then(function (buf) {
      original = { role: 'original', blob: file, content_type: type, size: file.size };
      var animated = isAnimated(new Uint8Array(buf), type);
      status('Decoding image...');
      return decodeImage(file).then(function (d) {
        decoded = d;
        return { animated: animated, width: d.width, height: d.height };
      }, function () {
        throw new Error(type === 'image/heic' ? HEIC_HELP : 'The browser couldn\'t decode this image. It may be corrupt.');
      });
    }).then(function (info) {
      var roles = Object.keys(config.variants);
      // Smallest first: the first encode also tells us whether WebP works,
      // and it's cheaper to learn that on a thumbnail.
      roles.sort(function (a, b) { return config.variants[a].max_edge - config.variants[b].max_edge; });
      var sizes = roles.map(function (role) {
        return fitWithin(decoded.width, decoded.height, config.variants[role].max_edge);
      });
      return resampleAll(decoded, sizes, function (frac) {
        status('Resizing... ' + Math.round(frac * 100) + '%');
      }).then(function (images) {
        decoded.release(); // the full-size decode isn't needed for encoding
        decoded = null;
        var variants = [];
        return images.reduce(function (chain, img, idx) {
          return chain.then(function () {
            var role = roles[idx], settings = config.variants[role];
            status('Encoding ' + role + ' (' + (idx + 1) + '/' + roles.length + ')...');
            var canvas = imageDataCanvas(img);
            images[idx] = null;
            return encodeWithFallback(canvasEncoder(canvas), settings.quality, config.preferredType,
              config.fallbackType, config.variantTypes).then(function (blob) {
              releaseCanvas(canvas);
              var v = { role: role, blob: blob, content_type: blob.type, size: blob.size,
                width: img.width, height: img.height };
              if (shouldReuseOriginal(v, info, original, config.reusableOriginalTypes || [])) {
                v = { role: role, reuse_original: true, blob: null, content_type: original.content_type,
                  size: original.size, width: img.width, height: img.height, encodedSize: blob.size };
              } else if (blob.size > config.maxVariantBytes) {
                throw new Error('The ' + role + ' image came out too large.');
              }
              variants.push(v);
            }, function (err) { releaseCanvas(canvas); throw err; });
          });
        }, Promise.resolve()).then(function () {
          return { original: original, variants: variants, animated: info.animated,
            width: info.width, height: info.height };
        });
      });
    }).finally(function () {
      if (decoded) decoded.release();
    });
  }

  var api = {
    fitWithin: fitWithin,
    AreaResampler: AreaResampler,
    shouldReuseOriginal: shouldReuseOriginal,
    sniffImageType: sniffImageType,
    isAnimated: isAnimated,
    encodeWithFallback: encodeWithFallback,
    resetEncoderCache: resetEncoderCache,
    prepareImage: prepareImage,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ImagePrep = api;
})(this);
