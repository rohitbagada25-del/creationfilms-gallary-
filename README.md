# Creation Films Client Gallery

A Flask-based private photo gallery with a Cloudflare Worker direct-upload path and Telegram-backed media/database backup. This build keeps the existing photo upload/gallery routes and adds a premium admin dashboard layout plus YouTube film links per gallery.

## Included admin sections
- Dashboard with gallery overview and create-gallery form
- Clients list (derived from existing galleries)
- Uploads overview linking to each gallery's existing uploader
- Studio/gallery branding settings stored in the existing JSON database
- Per-gallery YouTube film management: add a title + YouTube URL, or remove a film

## YouTube films
1. Upload the finished film to YouTube.
2. Choose **Unlisted** if it should not appear in normal YouTube search. Unlisted is not private: anyone with the link can watch or share it.
3. In Admin → open a gallery → YouTube Wedding Films, paste the link and title.
4. The film appears embedded in the client gallery and streams from YouTube, not from the Cloudflare Worker.

## Deployment
- Deploy the whole project to the same Render service/repository as the current app.
- Keep all existing Render environment variables and the Cloudflare Worker configuration unchanged.
- Do not replace the live deployment until a preview/staging deployment has been tested.
- Keep a backup of the existing database/Telegram backup before deploying.

## Important
The Python source passed `py_compile` in the build environment. A live integration test against your Render environment, Cloudflare Worker, and Telegram credentials was not possible here. Test login, existing photo upload/download, password protection, favorites, deletion, and YouTube embeds in a staging deployment before replacing the live site.
