# 27 — Dashboard (Vercel)

Status: **built and deployed** 2026-11-15.

| Item | Value |
|---|---|
| URL | `https://rade-bot.vercel.app` |
| Vercel project | `prj_wPDlA89fF7tjO32fHFbiiNKn0syC` |
| Source | `web/` in this repo, auto-deployed from `main` |
| Backend | `https://rade-bot.onrender.com` (Render) |

## Sign-in

The dashboard is a static page plus serverless functions. There is no user
database; the account lives in the Vercel environment:

| Variable | Meaning |
|---|---|
| `DASHBOARD_EMAIL` | the sign-in address |
| `DASHBOARD_PASSWORD_HASH` | salted SHA-256 of the password (never the clear text) |
| `DASHBOARD_SESSION_SECRET` | HMAC key for the session cookie |
| `DASHBOARD_TOKEN` | optional shared-token backstop for scripted access |

A successful sign-in sets an **httpOnly, SameSite=Lax cookie** valid 12 hours.
The password is never put in `localStorage`, so it is not readable from script.

To change the password, recompute the hash (recipe in `.env.example`) and update
`DASHBOARD_PASSWORD_HASH` in the Vercel project, then redeploy.

## What it can do

Read everything; write exactly one thing — the `execution_paused` switch in
`mirror_control`. It cannot place or close an order, and it never sees the
backend token or the Supabase key: the browser sends the session cookie, the
proxy checks it, and only then calls Render with the backend token.

## Layered protection

1. **Vercel SSO** on the custom domain (`all_except_custom_domains`).
2. **Session cookie** issued by `/api/login`.
3. **Backend auth** on every proxied call.
4. **Supabase RLS** on the tables; the service uses the secret key server-side.

## Tests

`node --test web/test/proxy.test.js` — 15 tests covering the query whitelist,
the trade-id path check, the write body whitelist, and that a forged, expired
or absent session is refused while a valid one is accepted.
