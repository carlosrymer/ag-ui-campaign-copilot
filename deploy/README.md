# Deploying the replayer

The published site at <https://carlosrymer.github.io/ag-ui-campaign-copilot/> is the
static replayer built from `frontend/`.

## How this repo is currently deployed

The built site is published to the **`gh-pages` branch**, which GitHub Pages serves
directly:

```bash
cd frontend
npm ci
npm run validate        # AG-UI schema conformance on the committed recordings
npm run build           # writes frontend/dist (base path /ag-ui-campaign-copilot/)
touch dist/.nojekyll

# publish dist/ to the gh-pages branch
cd dist
git init && git checkout -b gh-pages
git add -A && git commit -m "Publish replayer"
git remote add origin https://github.com/carlosrymer/ag-ui-campaign-copilot.git
git push -f origin gh-pages
```

## The Actions workflow

`github-pages-workflow.yml` in this directory is the CI equivalent: it validates the
recordings, builds, and publishes via `actions/configure-pages` →
`upload-pages-artifact` → `deploy-pages` on every push to `main`.

It lives here rather than in `.github/workflows/` because the credential I had while
building this repo lacked the `workflow` OAuth scope, so it could not push files into
`.github/workflows/`. To enable it, copy it into place from a machine whose credential
has that scope:

```bash
mkdir -p .github/workflows
cp deploy/github-pages-workflow.yml .github/workflows/deploy.yml
git add .github/workflows/deploy.yml && git commit -m "Enable Pages workflow" && git push
```

Then set **Settings → Pages → Source** to *GitHub Actions*. After that, pushes to `main`
redeploy automatically and the `gh-pages` branch is no longer needed.
