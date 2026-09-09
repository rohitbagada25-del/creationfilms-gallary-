# Creation Films v14 fixes

This v14 build keeps the Cloudflare direct-upload architecture and fixes the main v12/v14 UI issues:

- Batch upload progress is calculated from original photo bytes only.
- Progress/MB display is monotonic and does not jump backward during retries or when small files finish.
- Thumbnail/preview uploads no longer distort the main percentage.
- Temporary/network/5xx/429 upload errors retry automatically up to 3 attempts.
- Offline uploads pause and resume when the browser reports the connection is back.
- Gallery entry now shows a loading screen until the API data and the first visible thumbnails/hero image have finished loading (or failed), instead of showing an empty/half-loaded gallery.
- Existing Cloudflare media delivery, original download, uncropped grid layout, ordering, favorites, lightbox navigation, and gallery/admin features are retained.
