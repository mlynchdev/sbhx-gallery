# SBHX Shop Floor

A Flask app where SBHX members sign in with Authentik, upload photos of parts they've machined, and get a public gallery at `/u/<username>`.

- **Auth:** Authentik over OpenID Connect (Authlib)
- **Photos:** Cloudflare R2. The browser uploads straight to the bucket with a presigned PUT URL.
- **Metadata:** Cloudflare D1, called through its HTTP API
- **Design:** the SBHX landing page design system (`app/static/css/styles.css`), plus `app.css` for the gallery pages

## How an upload works

The browser shrinks each photo before uploading it, so galleries load small files while the full-resolution original is kept.

1. **Prepare (browser, `static/js/imageprep.js`).**
   - It sniffs the file's real format from its first bytes; the browser's `file.type` is only a guess from the extension.
   - It decodes the image once with `createImageBitmap(..., {imageOrientation: 'from-image'})`, which applies EXIF rotation, so portrait phone photos come out upright.
   - It builds both variants from the original's pixels in one pass, using an area-average (box) downscaler. That pass reads about 4 MP strips, so memory stays bounded.
   - It never upscales and always keeps the aspect ratio.
   - It encodes each variant:

     | Variant | Longest edge | Quality | Used for |
     |---|---|---|---|
     | `thumb` | 640 px | 0.85 | gallery cards |
     | `detail` | 2048 px | 0.90 | photo page |

     Settings live in `IMAGE_VARIANTS` and can be overridden with `THUMB_MAX_EDGE`, `THUMB_QUALITY`, `DETAIL_MAX_EDGE` and `DETAIL_QUALITY`.
2. **Presign (`POST /api/uploads`).** The browser sends each file's role, actual Blob type, byte size and, for variants, its pixel size. Flask checks them against the limits and picks the keys: `users/<user_id>/<upload_id>/{original,thumb,detail}.<ext>`. It then presigns one PUT per file with that exact `Content-Type`, and returns a signed upload token listing what it authorized.
3. **Upload.** The browser PUTs each raw Blob straight to R2, one at a time, with up to 3 attempts for network errors and 5xx responses. R2 credentials stay on the server.
4. **Publish (`POST /api/photos`).** For every object, Flask:
   - HEADs it to confirm it exists, has the signed type and is within the size limit;
   - reads its first 64 KB to confirm the bytes really are that format and the header's pixel size matches what was declared;
   - checks that the thumb is the detail scaled down.

   Only then does it write the `photos` row and its `photo_variants` rows. The check reads headers, not pixels, so it can't detect a file that's truncated or corrupt further in.
5. **Failure.** If any PUT fails, the browser calls `POST /api/uploads/abort`, which deletes whatever arrived. A rejected publish deletes everything. Saving is idempotent, so retrying after a lost response returns the same photo. A closed tab leaves objects with no row, which `flask cleanup-uploads` removes (below).

**What's stored.** The original is kept exactly as uploaded, and **Download original** serves it. The table below shows sizes from testing:

| Original | Thumb | Detail | Detail as share of original |
|---|---|---|---|
| 12 MP JPEG, 4.9 MB | 28 KB WebP | 0.9 MB WebP | ~19% |
| 48 MP JPEG, 17.8 MB | 15 KB WebP | 0.5 MB WebP | ~3% |

Each photo therefore costs 103–120% of its original's storage, and the uploader sends that much. In return, everyone viewing a gallery downloads under 1% of the original per card. The original is also there to re-generate variants later, for example with a better encoder.

**Small originals.** If a photo already fits the detail size (2048 px), its "detail" would only be a re-encode. When that re-encode comes out no smaller than the original, the browser skips it: the detail row points at the original's key and nothing extra is uploaded. A 1500×2000 WebP went from 3 stored files (936 KB) to 2 (465 KB). This applies only to JPEG, PNG and WebP originals; a GIF would animate on the photo page, and not every browser shows HEIC/AVIF. The server checks the original's real header size against the declared size, allowing width and height to swap for EXIF-rotated JPEGs.

**Older photos** (uploaded before variants existed) have no `photo_variants` rows. Pages fall back to the original for them, so no migration of existing files is needed.

### Formats and browsers

- **WebP vs. JPEG.** Chromium and Firefox encode WebP. Safari, including iOS Safari as tested on iOS 18.3, can't: `canvas.toBlob('image/webp')` silently returns PNG. The code labels each Blob by its actual `blob.type`. When the preferred type doesn't come back, it switches to JPEG at the same quality and shows a notice; it never sends PNG bytes labelled as WebP. The server re-checks the bytes either way.
- **Why JPEG rather than a WebAssembly WebP encoder.** This project has no JS build step. A maintained WASM encoder (for example `@jsquash/webp`, from libwebp) would add a vendored ~300 KB+ `.wasm` file to keep updated, slow the first upload on phones, and add memory pressure on iOS. Canvas JPEG is built into every browser, and at 0.90 the detail image keeps fine detail. It came out 30–50% larger than WebP in testing. Revisit WASM if Safari's share of uploads makes that storage matter.
- **HEIC/HEIF.** The file picker doesn't list HEIC, so iOS converts photos to JPEG as they're picked. A HEIC dropped or picked on a desktop is decoded if the browser can (Safari); otherwise the user gets an explicit "export as JPEG" message.
- **Animated GIF/WebP/APNG.** Variants show the first frame, a notice says so, and the original keeps the animation for Download.
- **Transparency.** WebP variants keep alpha. The JPEG fallback puts transparent areas on white.

## Local development

You don't need Cloudflare or Authentik to run it locally: it uses a SQLite file, a local folder that imitates R2's presigned PUTs, and a dev-only sign-in form.

```bash
cp .env.example .env          # defaults are local-only; set SECRET_KEY
uv sync
uv run flask --app app init-db
uv run flask --app app run --port 5050
uv run pytest                       # Flask, upload API, header sniffing
node --test 'tests/js/*.test.js'    # browser preprocessing helpers
```

`init-db` applies every file in `migrations/`. They're idempotent, so re-running it on an existing database is safe.

The dev sign-in (`/auth/dev-login`) works only when both `DEV_LOGIN=1` and Flask debug are on.

## Production setup

### 1. D1

```bash
wrangler d1 create photo-share                  # paste the id into wrangler.toml
wrangler d1 migrations apply photo-share --remote   # also applies 0002_photo_variants.sql to an existing DB
```

Create a Cloudflare API token with **Account → D1 → Edit**. Then set `DB_BACKEND=d1`, `CF_ACCOUNT_ID`, `D1_DATABASE_ID` and `CF_API_TOKEN`.

Flask is a WSGI app and can't run inside a Cloudflare Worker, so it reaches D1 over the REST API. Each query is an HTTPS round trip, roughly 50–150 ms from outside Cloudflare. A page makes one to three queries, which is fine at this scale.

### 2. R2

```bash
wrangler r2 bucket create sbhx-photos
wrangler r2 bucket cors set sbhx-photos --file r2-cors.json   # edit the origins first
```

- Create an R2 API token with **Object Read & Write** on the bucket. Set `STORAGE_BACKEND=r2`, `R2_BUCKET`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`.
- Galleries are public, so connect a custom domain to the bucket (for example `photos.sbhackerspace.com`) and set `R2_PUBLIC_BASE_URL`. Images then load from Cloudflare's CDN. If you leave it empty, the app signs a GET URL for every image instead.
- **CORS** (`r2-cors.json`) must allow your site's origin, the `PUT` method and the `content-type` header. Browsers send a preflight request for these uploads, so without this every PUT fails with a network error. The app reports that as a possible CORS problem. No other headers are needed: the signature is in the URL query string. Edit `origins` to your real domain before applying it.
- **Abandoned uploads.** Run `uv run flask --app app cleanup-uploads` daily, for example from cron. It deletes objects under `users/` that are older than `ORPHAN_MAX_AGE_HOURS` (default 24) and that no photo or variant row references. Use `--dry-run` to preview. An R2 lifecycle rule can't do this, because it can't tell an orphan from a published photo.
- **Public originals.** A public custom domain serves every key in the bucket, originals included. Their keys are unguessable, but anyone with a key can fetch it. To make originals private, add a Cloudflare WAF rule on that hostname blocking paths matching `*/original.*`. **Download original** would then need presigned GETs from the S3 endpoint, which it already uses.

### 3. Authentik

1. **Applications → Providers → Create → OAuth2/OpenID Provider**
   - Client type: Confidential
   - Redirect URI: `https://<your-host>/auth/callback`
   - Scopes: `openid`, `profile`, `email`
2. **Applications → Create**, slug `photo-share`, using that provider.
3. Set `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, and
   `OIDC_DISCOVERY_URL=https://<authentik-host>/application/o/photo-share/.well-known/openid-configuration`.

When someone signs in for the first time, the app creates their account. The username comes from the `preferred_username` claim, and the app adds `-2`, `-3` and so on if that name is taken. Users are matched on `sub`, so renaming someone in Authentik won't split their account.

### 4. Run

```bash
uv run gunicorn -w 2 -b 0.0.0.0:8000 "app:create_app()"
```

Put it behind HTTPS (a Cloudflare Tunnel works well) and set `SESSION_COOKIE_SECURE=1` and `DEV_LOGIN=0`. `ProxyFix` trusts a single proxy hop, so callback URLs come out as `https://`.

## Routes

| Path | Who | What |
|---|---|---|
| `/` | public | Hero and latest uploads from everyone |
| `/u/<username>` | public | A member's gallery |
| `/photos/<id>` | public | Detail image, metadata, download link for the original |
| `/upload` | signed in | Upload form |
| `/photos/<id>/edit` | owner | Edit title and description, or delete |
| `/settings` | signed in | Display name and bio |
