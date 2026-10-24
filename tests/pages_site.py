"""Build and serve the site exactly as GitHub Pages publishes it, for browser tests.

The release step fills https://wjwhitmire.github.io/reg-watch/, wjwhitmire and https://github.com/wjwhitmire/reg-watch in every
tracked file, then Pages serves the published folder under /<repo>/. Testing
the unfilled dev build at / once let a broken release through, so the browser
tests run against this build instead.

Third-party scripts: if PAGES_TEST_VENDOR names a folder, a request for a
pinned CDN file is answered from the file of the same name in that folder
(the page still checks its integrity hash). Otherwise the request goes to the
network, and tests that need the library skip when it cannot load.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import os
import pathlib
import shutil
import subprocess
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
USER = "wjwhitmire"
PLACEHOLDERS = ("https://wjwhitmire.github.io/reg-watch/", "wjwhitmire", "https://github.com/wjwhitmire/reg-watch")


def tracked_files(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True).stdout
        names = [n for n in out.decode().split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(root)) for p in root.rglob("*")
                 if p.is_file() and not ({".git", "node_modules", "__pycache__", ".venv"} & set(p.parts))]
    return [root / n for n in names if (root / n).is_file()]


def fill(text: str, name: str) -> str:
    return (text.replace("https://wjwhitmire.github.io/reg-watch/", f"https://{USER}.github.io/{name}/")
            .replace("wjwhitmire", USER)
            .replace("https://github.com/wjwhitmire/reg-watch", f"https://github.com/{USER}/{name}"))


def release_tree(dest: pathlib.Path, name: str, root: pathlib.Path = ROOT) -> pathlib.Path:
    """Copy the tracked files to dest and apply the release placeholder fill."""
    for src in tracked_files(root):
        out = dest / src.relative_to(root)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            out.write_bytes(data)
            continue
        out.write_text(fill(text, name) if any(p in text for p in PLACEHOLDERS) else text, encoding="utf-8")
    return dest


def pages_root(tree: pathlib.Path) -> pathlib.Path:
    """The folder Pages serves when publishing from the main branch /docs folder."""
    return tree / "docs"


class _Handler(http.server.SimpleHTTPRequestHandler):
    prefix = "/"

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith(self.prefix):
            return str(pathlib.Path(self.directory) / "__outside_the_site__")
        return super().translate_path("/" + path[len(self.prefix):])

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def serve(site: pathlib.Path, name: str):
    handler = functools.partial(type("H", (_Handler,), {"prefix": f"/{name}/"}), directory=str(site))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/{name}/"
    finally:
        srv.shutdown()
        srv.server_close()


def install_routes(context, origin: str, errors: list) -> None:
    """Same-origin requests pass; fonts get an empty stylesheet; pinned CDN files may come from a local folder."""
    vendor = os.environ.get("PAGES_TEST_VENDOR")

    def handle(route):
        url = route.request.url
        if url.startswith(origin) or url.startswith(("blob:", "data:")):
            return route.continue_()
        if "fonts.googleapis.com" in url or "fonts.gstatic.com" in url:
            return route.fulfill(status=200, body="", headers={"content-type": "text/css"})
        if url.startswith(("https://cdnjs.cloudflare.com/", "https://cdn.jsdelivr.net/")):
            if vendor:
                local = pathlib.Path(vendor) / url.rsplit("/", 1)[1]
                if local.is_file():
                    return route.fulfill(status=200, path=str(local), headers={
                        "content-type": "application/javascript", "access-control-allow-origin": "*"})
            return route.continue_()
        errors.append("unexpected request " + url)
        return route.abort()

    context.route("**/*", handle)


def watch(page, errors: list) -> None:
    page.on("pageerror", lambda e: errors.append(f"pageerror {e}"))
    page.on("console", lambda m: errors.append(f"console {m.text}") if m.type == "error" and "ERR_" not in m.text else None)
    page.add_init_script("window.__csp=[];document.addEventListener('securitypolicyviolation',e=>window.__csp.push(e.violatedDirective+' '+e.blockedURI));")


def copy_tree(src: pathlib.Path, dest: pathlib.Path) -> None:
    shutil.copytree(src, dest, dirs_exist_ok=True)
