# Security

## Data handling

- No keys, tokens or credentials are needed or stored. The Federal Register API is public. The collect workflow uses only the built-in `GITHUB_TOKEN`, with `contents: write` granted to that one job; CI and Pages workflows are read-only for repository contents.
- The collector requests only URLs under `https://www.federalregister.gov/api/v1/`. A `next_page_url` pointing anywhere else is ignored and logged, and an HTTP redirect that leaves that prefix is refused.
- Everything from the API is treated as untrusted. Text is stripped of control characters and GPO markup and length-capped. Links are kept only if they are https URLs on federalregister.gov or govinfo.gov made of printable ASCII without quotes or angle brackets. Malformed records are skipped and logged.
- Atom feeds are built with `xml.etree`, so every value is XML-escaped by the serializer.
- The standalone page (`web/app.html`) makes no network calls. No fetch, XHR, WebSocket, beacons or analytics. The only external resource is the Google Fonts stylesheet. Its data is an embedded JSON snapshot.
- The GitHub Pages build (`docs/index.html`) makes one request: `data/documents.json` from its own origin. A Content-Security-Policy allows scripts only by SHA-256 hash and connections only to the same origin.
- The page renders every API value with `textContent` and `createElement`, never `innerHTML`. There is no `eval`, no `new Function`, no inline event handlers and no browser storage. Links open with `rel="noopener noreferrer"`.
- Nothing you type into the page is uploaded or stored. CSV export goes to your clipboard only, with formula-injection protection.
- The Python package has no runtime dependencies, uses no `pickle`, `eval` or shell calls, and writes only inside the data directory it is given.

## Reporting a vulnerability

Open a private security advisory on this repository (Security tab, "Report a vulnerability"). Include steps to reproduce and the affected file. Please do not open a public issue for a security problem.
