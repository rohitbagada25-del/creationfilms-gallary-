# Creation Films Gallery — v11 fixed upload

Updates:
- Reliable 1 MB resumable photo chunks for compatibility with common hosting/proxy limits.
- Adaptive photo upload concurrency: starts at 2 and ramps up to 6 when throughput remains healthy; backs off on errors.
- Per-photo resumable retries and progress/speed display.
- Client gallery shows a loading state while the photo list is fetched.
- Hero/cover photo now shows a loading indicator until the image actually loads.
- Local thumbnail/preview caching remains enabled for faster gallery rendering.
