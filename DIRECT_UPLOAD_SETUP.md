# Creation Films — Cloudflare Direct Upload

This build changes the photo upload path to:

**Phone/PC → Cloudflare Worker → Telegram**

Render only issues a short-lived upload ticket and saves lightweight gallery metadata. The original photo bytes no longer pass through Render during upload.

## Cloudflare Worker secrets

Keep the existing secrets:

- `TELEGRAM_BOT_TOKEN` = the same BotFather token already used by the gallery
- `MEDIA_MEDIA_SECRET` = the same secret already used by Render's `MEDIA_MEDIA_SECRET`

Add one new secret:

- `TELEGRAM_CHAT_ID` = the exact same value already used as Render's `CHANNEL_ID`

Do not put any of these values in frontend code.

## Deploy Worker

Open the existing `creationfilmsmedia` Worker, replace its code with `cloudflare-worker/src_index.js`, save/deploy it, and keep the existing Worker URL.

The Worker now supports both:

- `/media/thumb/...`, `/media/preview/...`, `/media/raw/...` for delivery
- `/upload` for direct Telegram uploads

## Render environment variables

The app still needs:

- `BOT_TOKEN`
- `CHANNEL_ID`
- `ADMIN_KEY`
- `SECRET_KEY`
- `MEDIA_WORKER_URL=https://creationfilmsmedia.rohitbagada25.workers.dev`
- `MEDIA_MEDIA_SECRET` = exactly the same secret as the Worker

## Important limit

Direct photo upload is limited to 50 MB because Telegram Bot API `sendDocument` is limited to 50 MB. The browser creates the 1280px thumbnail and 2000px preview before sending them through Cloudflare.

This removes the large photo upload bytes from Render outbound bandwidth.

Byte-level chunk resume is not used in this direct mode because Telegram Bot API does not provide a resumable chunk-upload API. If a connection breaks, the affected photo gets a Retry action; already completed photos are not re-uploaded by the normal queue.
