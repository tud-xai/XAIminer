# Vendored front-end libraries

The three libraries the pages need, served from this repository instead of a CDN. Reason: the
public demo would otherwise transmit every visitor's IP address to the CDN operator, which the
privacy policy then has to declare.

This does **not** introduce a build step — the files are used exactly as downloaded, referenced
with `url_for('static', filename='vendor/…')`.

| Directory | Version | Source |
| --- | --- | --- |
| `bootstrap-5.3.3/` | 5.3.3 | `https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/{css/bootstrap.min.css,js/bootstrap.bundle.min.js}` |
| `bootstrap-icons-1.11.3/` | 1.11.3 | `https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css` plus `font/fonts/bootstrap-icons.woff{,2}` |
| `htmx-2.0.4/` | 2.0.4 | `https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js` |

Two edits were made to the downloaded files, nothing else:

- the trailing `sourceMappingURL` comments in the two Bootstrap files were removed (the `.map`
  files are not vendored, and the reference would only 404 in a browser's developer tools),
- nothing was touched in the icon CSS: its `url("fonts/…")` references are relative and already
  match the `fonts/` subdirectory here.

## Upgrading

Download the new files into a directory named after the new version, point every page
skeleton that loads them (`templates/base.html` and any standalone page) at it,
delete the old directory, and update this table. The version is part of the path on purpose: it
is what makes a browser fetch the new file instead of a cached old one.
