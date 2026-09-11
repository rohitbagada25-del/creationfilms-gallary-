# Creation Films v16 — Render bandwidth fix

## Photo uploads
Photo originals, thumbnails and previews use:

Phone/browser -> Cloudflare Worker -> Telegram

The Render service does not proxy the photo bytes to Telegram.

## Database backup
The gallery JSON database is still backed up to Telegram, but the backup is now sent through the Cloudflare Worker. Render only sends the small JSON payload to the Worker.

Rapid saves are coalesced into one delayed backup so a batch of photo registrations does not create dozens of duplicate Telegram backup uploads.

## Render metrics
Existing bandwidth already consumed on Render will not disappear or reset. After deploying v16, test with a few new photos and watch the *new* Service-Initiated usage. It should remain very small for photo uploads.

Other features such as legacy video upload/legacy photo endpoints can still use Render -> Telegram if invoked; the normal v16 photo uploader does not.
