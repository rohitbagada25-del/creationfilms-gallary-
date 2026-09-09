# Creation Films v12 Simple Setup

## Render
Keep the existing Telegram/database environment variables and add:
- MEDIA_WORKER_URL=https://creationfilmsmedia.rohitbagada25.workers.dev
- MEDIA_MEDIA_SECRET=<the same secret configured in the Worker>

Start command:
`gunicorn server:app`

## Cloudflare Worker
Deploy `cloudflare-worker/src_index.js` to the existing `creationfilmsmedia` Worker.
Set these Worker secrets:
- TELEGRAM_BOT_TOKEN = existing bot token
- MEDIA_MEDIA_SECRET = same secret as Render

The Worker serves thumb/preview/raw media through Telegram Bot API and caches successful responses.

## Important limitation
Telegram Bot API's `getFile` download path has a 20 MB limit. Therefore this version deliberately does NOT pretend to support >20 MB original downloads through Cloudflare. For >20 MB originals, the existing Render fallback remains available. This is the safe simple architecture.
