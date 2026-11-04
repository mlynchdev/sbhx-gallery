// Direct-to-R2 upload: get a presigned URL, PUT the file, then save metadata.
(function () {
  'use strict';

  var form = document.getElementById('uploadForm');
  if (!form) return;

  var csrf = document.querySelector('meta[name="csrf-token"]').content;
  var input = document.getElementById('fileInput');
  var dropzone = document.getElementById('dropzone');
  var preview = document.getElementById('preview');
  var prompt = document.getElementById('dropzonePrompt');
  var progress = document.getElementById('progress');
  var bar = document.getElementById('progressBar');
  var errorBox = document.getElementById('formError');
  var submit = document.getElementById('submitBtn');
  var maxBytes = Number(form.dataset.maxBytes);
  var allowed = form.dataset.allowed.split(',');

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.hidden = !msg;
  }

  function validate(file) {
    if (!file) return 'Choose an image to upload.';
    if (allowed.indexOf(file.type) === -1) return 'Unsupported file type. Use JPEG, PNG, WebP or GIF.';
    if (file.size > maxBytes) return 'That file is too large.';
    return '';
  }

  function showPreview(file) {
    showError(validate(file));
    if (!file) return;
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
    prompt.hidden = true;
  }

  input.addEventListener('change', function () { showPreview(input.files[0]); });

  ['dragenter', 'dragover'].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.add('dragover'); });
  });
  ['dragleave', 'drop'].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.remove('dragover'); });
  });
  dropzone.addEventListener('drop', function (e) {
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      showPreview(input.files[0]);
    }
  });

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error(data.error || 'Request failed (' + res.status + ').');
        return data;
      });
    });
  }

  // XHR rather than fetch so we get upload progress events.
  function putFile(url, file, contentType) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('PUT', url);
      xhr.setRequestHeader('Content-Type', contentType);
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) bar.style.width = (100 * e.loaded / e.total).toFixed(1) + '%';
      };
      xhr.onload = function () {
        xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error('Upload failed (' + xhr.status + ').'));
      };
      xhr.onerror = function () { reject(new Error('Upload failed. Check the bucket CORS settings.')); };
      xhr.send(file);
    });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var file = input.files[0];
    var err = validate(file);
    if (err) return showError(err);
    showError('');

    submit.disabled = true;
    submit.textContent = 'Uploading...';
    progress.hidden = false;
    bar.style.width = '0';

    var key;
    postJSON(form.dataset.presignUrl, { content_type: file.type, size: file.size })
      .then(function (up) {
        key = up.key;
        return putFile(up.upload_url, file, up.content_type);
      })
      .then(function () {
        submit.textContent = 'Saving...';
        return postJSON(form.dataset.saveUrl, {
          key: key,
          title: form.elements.title.value,
          description: form.elements.description.value,
        });
      })
      .then(function (photo) { window.location = photo.url; })
      .catch(function (error) {
        showError(error.message);
        submit.disabled = false;
        submit.textContent = 'Upload';
        progress.hidden = true;
      });
  });
})();
