TypeScript frontend sources live in `app/ts/`.

Build (from repo root or `app` dir):

```bash
# from repo root
cd app
npm install
npm run build
```

This will bundle the frontend and place the result in `api/static/dist/bundle.js` which your FASTAPI templates should load via `/static/dist/bundle.js`.

Notes:
- Keep original JS files in `api/static/js/` for backward compatibility until you update templates.
- After verification you can remove `api/static/js/*` and the `api/static/ts/` copies.
