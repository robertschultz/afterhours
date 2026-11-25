# Shared web assets

The two loopback services serve a status page and shared browser modules. Assets are also available through the `/engine/web/` alias. Local data roots may supply approved media without overriding bundled HTML or scripts.

The bundled posters and eight-second MP4 files are synthetic and contain no audio or likeness. Regenerate them with `scripts/generate-demo-media.py` from the engine checkout and FFmpeg available on PATH.

The optional Director SDK is built from `director-sdk.js` using `pnpm build`.
