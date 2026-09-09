# Creation Films Client Gallery

Fast client gallery with Telegram-backed original storage.

## Image pipeline
- Gallery grid: pre-generated **640px JPEG thumbnails**.
- Photo viewer: pre-generated **1440px compressed JPEG preview**.
- Download: the **exact original uploaded file**.
- Thumbnails and previews are created during upload, so client browsing never has to resize an original.

## Upload improvements
- Up to 3 photo uploads run in parallel to avoid the 5-9 second pause between files.
- Each upload automatically retries transient failures up to 3 times and continues with the remaining queue.
- A failed file does not force successful files to upload again.
- Telegram database backups run in the background instead of blocking every photo upload.

## Admin improvements
- Rename a gallery without changing its share URL.
- Select multiple photos and delete them in one action.
- Keep the existing individual delete and cover-photo controls.

## Fresh-start note
This build intentionally does not use the old on-demand thumbnail fallback. Start new galleries/uploads with this version so every photo gets its thumbnail and preview at upload time.
