# evidence-foundry-landing

Public landing site for [evidencefoundry.com](https://evidencefoundry.com) —
**Evidence Foundry**, the truth-first research workspace. The product app lives at
[app.evidencefoundry.com](https://app.evidencefoundry.com) (repo: `research-facility`);
this repo is the thin, standalone marketing/explanation page at the apex domain only.

Sibling pattern: `seitiate-landing`, `sakme-landing`, `closedclaw-landing`.

## Design

"Field Ledger" skin — the same design system as the app, so landing and workspace
read as one product. Tokens in `styles.css` mirror
`research-facility/frontend/src/styles/tokens.css`; keep them in sync by hand.
Fonts: Newsreader (display serif) + IBM Plex Sans/Mono, loaded from Google Fonts.

Static only: no build step. The sign-in CTA points at
`app.evidencefoundry.com`; the reviewed-access application form posts JSON to
the Seitiate+ vertical lifecycle endpoint for `evidence-foundry`.

## Deploy — GitHub Pages

1. Settings → Pages → Deploy from branch: `main`, folder `/ (root)`.
2. Custom domain: `evidencefoundry.com` (the `CNAME` file in this repo sets it).
3. Enforce HTTPS once the certificate is issued.

Every push to `main` redeploys. No CI needed.

## DNS (Namecheap, apex)

The apex cannot use a CNAME record. Add either:

- **A records** (host `@`) → `185.199.108.153`, `185.199.109.153`,
  `185.199.110.153`, `185.199.111.153`
  (optionally AAAA → `2606:50c0:8000::153` … `2606:50c0:8003::153`), or
- a Namecheap **ALIAS** record (host `@`) → `gcharris.github.io`.

Optional: CNAME record for host `www` → `gcharris.github.io` (GitHub redirects
www → apex once the custom domain is set).

`app.evidencefoundry.com` is separate — it points at the Cloud Run app deploy and
is not managed by this repo.
