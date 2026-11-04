# SBHX Shop Floor

A Flask app where SBHX members sign in with Authentik, upload photos of parts they've machined, and get a public gallery at `/u/<username>`.

- **Auth:** Authentik over OpenID Connect (Authlib)
- **Photos:** Cloudflare R2. The browser uploads straight to the bucket with a presigned PUT URL.
- **Metadata:** Cloudflare D1, called through its HTTP API
- **Design:** the SBHX landing page design system (`app/static/css/styles.css`), plus `app.css` for the gallery pages

## How an upload works

1. The browser calls `POST /api/uploads` with the file's type and size. The server checks both and returns a presigned PUT URL for `users/<user_id>/<uuid>.<ext>`.
2. The browser PUTs the file directly to R2, so the image never passes through Flask.
3. The browser calls `POST /api/photos` with the key, title and description. The server checks that the key belongs to the current user, HEADs the object in R2 to confirm it exists and that its size and type are allowed, and then inserts the row into D1.

The `photos` table stores the R2 `object_key`, not a full URL. View and download URLs are built when the page renders, so moving the bucket to a new domain doesn't break old rows. The Download button uses a presigned GET with `Content-Disposition: attachment`.

## Local development

You don't need Cloudflare or Authentik to run it locally: it uses a SQLite file, a local folder that imitates R2's presigned PUTs, and a dev-only sign-in form.

```bash
cp .env.example .env          # defaults are local-only; set SECRET_KEY
uv sync
uv run flask --app app init-db
uv run flask --app app run --port 5050
uv run pytest
```

The dev sign-in (`/auth/dev-login`) works only when both `DEV_LOGIN=1` and Flask debug are on.

## Production setup

### 1. D1

```bash
wrangler d1 create photo-share                  # paste the id into wrangler.toml
wrangler d1 migrations apply photo-share --remote
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
- If someone abandons an upload between steps 2 and 3, the object stays in the bucket with no row. An R2 lifecycle rule can clean these up, or you can live with them.

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
| `/photos/<id>` | public | Photo, metadata, download link |
| `/upload` | signed in | Upload form |
| `/photos/<id>/edit` | owner | Edit title and description, or delete |
| `/settings` | signed in | Display name and bio |
