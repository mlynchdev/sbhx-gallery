// Upload page: preprocess in the browser, presign, PUT each Blob straight to
// storage, then ask Flask to verify and publish.
(function () {
  'use strict';

  var form = document.getElementById('uploadForm');
  if (!form || !window.ImagePrep) return;

  var config = JSON.parse(form.dataset.config);
  var csrf = document.querySelector('meta[name="csrf-token"]').content;
  var input = document.getElementById('fileInput');
  var dropzone = document.getElementById('dropzone');
  var preview = document.getElementById('preview');
  var prompt = document.getElementById('dropzonePrompt');
  var statusBox = document.getElementById('uploadStatus');
  var summary = document.getElementById('variantSummary');
  var notice = document.getElementById('uploadNotice');
  var progress = document.getElementById('progress');
  var bar = document.getElementById('progressBar');
  var errorBox = document.getElementById('formError');
  var submit = document.getElementById('submitBtn');

  var LABELS = { thumb: 'Thumbnail', detail: 'Detail', original: 'Original' };
  var PUT_ATTEMPTS = 3;

  var generation = 0;   // bumps on every new file so stale work is ignored
  var preparing = null; // Promise of the current file's prepared result
  var previewUrl = null;
  var busy = false;

  function kb(n) { return n < 1024 * 1024 ? Math.round(n / 1024) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }
  function fmt(type) { return type.split('/')[1].toUpperCase(); }

  function setStatus(msg) { statusBox.textContent = msg; statusBox.hidden = !msg; }
  function showError(msg) { errorBox.textContent = msg; errorBox.hidden = !msg; }
  function showNotice(msg) { notice.textContent = msg; notice.hidden = !msg; }

  function setPreview(blob) {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = blob ? URL.createObjectURL(blob) : null;
    preview.hidden = !blob;
    prompt.hidden = !!blob;
    if (previewUrl) preview.src = previewUrl; else preview.removeAttribute('src');
  }

  function renderSummary(result) {
    summary.innerHTML = '';
    result.variants.concat([result.original]).forEach(function (f) {
      var li = document.createElement('li');
      var dims = f.width ? f.width + ' × ' + f.height + ' · ' : (result.width + ' × ' + result.height + ' · ');
      li.textContent = f.reuse_original
        ? LABELS[f.role] + ': ' + dims + 'uses the original (re-encoding gave ' + kb(f.encodedSize) + ', more than ' + kb(f.size) + ')'
        : LABELS[f.role] + ': ' + dims + fmt(f.content_type) + ' · ' + kb(f.size);
      if (f.role === 'original' && !keepsOriginal(result)) li.textContent += ' · not stored';
      summary.appendChild(li);
    });
    summary.hidden = false;
  }

  // The original is uploaded when the site keeps originals, or when it is
  // itself the detail image (small enough already).
  function keepsOriginal(result) {
    return config.retainOriginal || result.variants.some(function (v) { return v.reuse_original; });
  }

  function filesToUpload(result) {
    return keepsOriginal(result) ? result.variants.concat([result.original]) : result.variants.slice();
  }

  function chooseFile(file) {
    var gen = ++generation;
    setPreview(null);
    summary.hidden = true;
    showError('');
    showNotice('');
    if (!file) { preparing = null; setStatus(''); return; }
    preparing = ImagePrep.prepareImage(file, config, function (msg) {
      if (gen === generation) setStatus(msg);
    }).then(function (result) {
      if (gen !== generation) return null;
      var thumb = result.variants.filter(function (v) { return v.role === 'thumb'; })[0];
      setPreview(thumb.blob);
      renderSummary(result);
      var notes = [];
      if (result.animated) notes.push('Animated image: the gallery shows the first frame. The original animation is kept and available through Download.');
      if (result.variants.some(function (v) { return !v.reuse_original && v.content_type !== config.preferredType; })) {
        notes.push('This browser can\'t encode WebP, so the previews were saved as JPEG.');
      }
      showNotice(notes.join(' '));
      setStatus('Ready to upload.');
      return result;
    }, function (err) {
      if (gen !== generation) return null;
      setStatus('');
      showError(err.message);
      input.value = '';
      return null;
    });
  }

  input.addEventListener('change', function () { chooseFile(input.files[0]); });
  ['dragenter', 'dragover'].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.add('dragover'); });
  });
  ['dragleave', 'drop'].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.remove('dragover'); });
  });
  dropzone.addEventListener('drop', function (e) {
    if (busy || !e.dataTransfer.files.length) return;
    input.files = e.dataTransfer.files;
    chooseFile(input.files[0]);
  });

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(data.error || 'The server returned an error (' + res.status + ').');
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // XHR rather than fetch for upload progress. The body is the raw Blob and
  // Content-Type must equal the type the URL was signed for.
  function putOnce(url, blob, contentType, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('PUT', url);
      xhr.setRequestHeader('Content-Type', contentType);
      xhr.upload.onprogress = function (e) { if (e.lengthComputable) onProgress(e.loaded); };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) return resolve();
        var err = new Error('HTTP ' + xhr.status);
        err.status = xhr.status;
        err.retryable = xhr.status >= 500 || xhr.status === 429;
        reject(err);
      };
      xhr.onerror = function () {
        var err = new Error('network error');
        err.retryable = true;
        reject(err);
      };
      xhr.send(blob);
    });
  }

  function putWithRetry(item, onProgress) {
    var attempt = 0;
    var run = function () {
      attempt += 1;
      return putOnce(item.url, item.blob, item.content_type, onProgress).catch(function (err) {
        if (err.retryable && attempt < PUT_ATTEMPTS) {
          setStatus('Retrying ' + LABELS[item.role].toLowerCase() + ' upload (attempt ' + (attempt + 1) + ' of ' + PUT_ATTEMPTS + ')...');
          return sleep(1000 * attempt * attempt).then(run);
        }
        err.role = item.role;
        throw err;
      });
    };
    return run();
  }

  function putFailureMessage(err) {
    var what = 'Uploading the ' + LABELS[err.role].toLowerCase() + ' image failed';
    if (err.status === 403) return what + ': the upload link was refused or expired. Nothing was saved; press Upload to try again.';
    if (!err.status) return what + ' (network error, or the storage bucket\'s CORS settings block this site). Nothing was saved; check your connection and press Upload to try again.';
    return what + ' (' + err.message + '). Nothing was saved; press Upload to try again.';
  }

  function setBusy(on, label) {
    busy = on;
    submit.disabled = on;
    input.disabled = on;
    submit.textContent = label || 'Upload';
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (busy) return;
    showError('');
    var title = form.elements.title.value.trim();
    if (!title) return showError('Add a title before uploading.');
    if (!preparing) return showError('Choose an image to upload.');

    setBusy(true, 'Preparing...');
    var token = null;
    preparing.then(function (result) {
      if (!result) throw new Error(errorBox.textContent || 'Choose an image to upload.');
      setBusy(true, 'Uploading...');
      setStatus('Requesting upload links...');
      var files = filesToUpload(result);
      return postJSON(form.dataset.presignUrl, {
        files: files.map(function (f) {
          var spec = { role: f.role, content_type: f.content_type, size: f.size };
          if (f.width) { spec.width = f.width; spec.height = f.height; }
          if (f.reuse_original) spec.reuse_original = true;
          return spec;
        }),
      }).then(function (session) {
        token = session.upload_token;
        var byRole = {};
        files.forEach(function (f) { byRole[f.role] = f; });
        var items = session.uploads.map(function (u) {
          // Always send exactly the type Flask signed.
          return { role: u.role, url: u.url, content_type: u.content_type, blob: byRole[u.role].blob };
        });
        var total = items.reduce(function (n, it) { return n + it.blob.size; }, 0);
        var done = 0;
        progress.hidden = false;
        bar.style.width = '0';
        // One PUT at a time keeps memory and bandwidth contention low.
        return items.reduce(function (chain, item) {
          return chain.then(function () {
            setStatus('Uploading ' + LABELS[item.role].toLowerCase() + ' (' + kb(item.blob.size) + ')...');
            return putWithRetry(item, function (loaded) {
              bar.style.width = (100 * (done + loaded) / total).toFixed(1) + '%';
            }).then(function () { done += item.blob.size; });
          });
        }, Promise.resolve());
      }).catch(function (err) {
        if (err.role) err.message = putFailureMessage(err);
        throw err;
      });
    }).then(function () {
      setStatus('Verifying and saving...');
      setBusy(true, 'Saving...');
      var body = { upload_token: token, title: title, description: form.elements.description.value };
      // Saving is idempotent, so one retry after a dropped response is safe.
      return postJSON(form.dataset.saveUrl, body).catch(function (err) {
        if (err.status) throw err;
        return sleep(1500).then(function () { return postJSON(form.dataset.saveUrl, body); });
      });
    }).then(function (photo) {
      token = null;
      setPreview(null);
      window.location = photo.url;
    }).catch(function (err) {
      if (token) {
        // Best effort: delete whatever reached storage. The cleanup command
        // catches anything this misses.
        postJSON(form.dataset.abortUrl, { upload_token: token }).catch(function () {});
      }
      showError(err.message);
      setStatus('');
      progress.hidden = true;
      setBusy(false);
    });
  });
})();
