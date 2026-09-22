"""Lightweight HTTP file manager deployed inside VPS instances (stdlib only)."""

from __future__ import annotations

# Python source written to /tmp/vex-fm.py on the instance.
FILE_MANAGER_PY = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import shutil
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = ""
ROOT = Path("/")
MAX_EDIT = 2 * 1024 * 1024

CSS = """
:root { --bg:#0b1020; --card:#141b33; --fg:#e8ecff; --muted:#8b93b8;
  --acc:#3dd6c6; --danger:#ff5c7a; --ok:#3dff9a; }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;
  background:linear-gradient(160deg,#070a16,#0b1020 40%,#0d1430); color:var(--fg); }
a { color:var(--acc); text-decoration:none; }
a:hover { text-decoration:underline; }
.wrap { max-width:1100px; margin:18px auto; padding:0 14px 40px; }
header { display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between;
  background:var(--card); border:1px solid #243056; border-radius:14px; padding:14px 16px; }
header h1 { margin:0; font-size:18px; font-weight:700; letter-spacing:.3px; }
header .tag { color:var(--muted); font-size:12px; }
.bar { display:flex; flex-wrap:wrap; gap:8px; margin:14px 0; }
.bar form, .bar input, .bar button, .bar select {
  background:#0e1530; color:var(--fg); border:1px solid #2a365c; border-radius:10px;
  padding:8px 10px; font:inherit;
}
.bar button, .btn {
  background:linear-gradient(180deg,#1f7a70,#17615a); border:none; cursor:pointer;
  color:#eafffc; font-weight:600; padding:8px 12px; border-radius:10px;
}
.btn.danger { background:linear-gradient(180deg,#a12645,#7c1b34); }
.btn.ghost { background:#182244; border:1px solid #2a365c; }
.crumbs { margin:8px 0 14px; color:var(--muted); word-break:break-all; }
.crumbs a { color:var(--acc); }
table { width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid #243056; border-radius:14px; overflow:hidden; }
th, td { padding:10px 12px; border-bottom:1px solid #1d2748; text-align:left; }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; }
tr:last-child td { border-bottom:none; }
tr:hover td { background:#182044; }
.mono { font-family:ui-monospace,Consolas,monospace; font-size:13px; }
.right { text-align:right; color:var(--muted); }
.actions { display:flex; gap:6px; flex-wrap:wrap; }
.flash { margin:10px 0; padding:10px 12px; border-radius:10px; background:#123; border:1px solid #2a365c; }
.flash.ok { border-color:#1d5; }
.flash.err { border-color:#f55; color:#fbb; }
.editor textarea {
  width:100%; min-height:420px; background:#0a0f22; color:#e8ecff;
  border:1px solid #2a365c; border-radius:12px; padding:12px;
  font:13px/1.4 ui-monospace,Consolas,monospace; resize:vertical;
}
.card { background:var(--card); border:1px solid #243056; border-radius:14px; padding:16px; }
footer { margin-top:18px; color:var(--muted); font-size:12px; }
.hide { display:none; }
"""


def _ok(req: "Handler") -> None:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
    if req.headers.get("X-FM-Token") == TOKEN:
        return
    if q.get("token", [""])[0] == TOKEN:
        return
    cookie = req.headers.get("Cookie", "")
    if f"fm_token={TOKEN}" in cookie:
        return
    req.send_response(401)
    req.send_header("Content-Type", "text/html; charset=utf-8")
    req.end_headers()
    req.wfile.write(
        (
            "<html><body style='font-family:system-ui;background:#0b1020;color:#e8ecff;"
            "padding:40px'><h2>Unauthorized</h2>"
            f"<p>Add token: <code>/?token={TOKEN}</code></p></body></html>"
        ).encode()
    )
    raise PermissionError


def _safe(path: str) -> Path:
    raw = urllib.parse.unquote(path or "/")
    p = (ROOT / raw.lstrip("/")).resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        p = ROOT.resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        raise PermissionError("outside root")
    return p


def _human(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


def _page(title: str, body: str, flash: str = "", flash_cls: str = "") -> bytes:
    flash_html = f'<div class="flash {flash_cls}">{html.escape(flash)}</div>' if flash else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <div><h1>VexDeploy File Manager</h1><div class="tag">upload · download · edit · delete</div></div>
  <div class="tag">token-protected</div>
</header>
{flash_html}
{body}
<footer>Bound to 127.0.0.1 · exposed via localhost.run · do not share the full URL with token</footer>
</div></body></html>"""
    return doc.encode("utf-8", errors="replace")


class Handler(BaseHTTPRequestHandler):
    server_version = "VexFM/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter
        pass

    def _redirect(self, loc: str) -> None:
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def _html(self, data: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _qs(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def do_GET(self) -> None:
        try:
            _ok(self)
        except PermissionError:
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/list"):
            self._list(qs)
            return
        if path == "/edit":
            self._edit_get(qs)
            return
        if path in ("/download", "/file"):
            self._download(qs)
            return
        if path == "/logout":
            self.send_response(200)
            self.send_header("Set-Cookie", "fm_token=; Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(b"logged out")
            return
        self._html(_page("Not found", "<div class='card'>Not found</div>"), 404)

    def do_POST(self) -> None:
        try:
            _ok(self)
        except PermissionError:
            return
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")

        if parsed.path == "/upload":
            self._upload(body, ctype, qs_from_body=False)
            return
        if parsed.path == "/delete":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            target = form.get("path", [""])[0]
            try:
                p = _safe(target)
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists() or p.is_symlink():
                    p.unlink()
                else:
                    raise FileNotFoundError(target)
                self._redirect("/?token=" + TOKEN + "&ok=" + urllib.parse.quote("Deleted " + target))
            except Exception as exc:
                self._html(_page("Delete", "<div class='card'>err</div>", str(exc), "err"))
            return
        if parsed.path == "/mkdir":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            parent = form.get("path", ["/"])[0]
            name = form.get("name", [""])[0].strip()
            try:
                if not name or "/" in name or name in (".", ".."):
                    raise ValueError("invalid name")
                (_safe(parent) / name).mkdir(exist_ok=False)
                self._redirect("/?token=" + TOKEN + "&path=" + urllib.parse.quote(parent) + "&ok=" + urllib.parse.quote("Created " + name))
            except Exception as exc:
                self._html(_page("Mkdir", "<div class='card'>err</div>", str(exc), "err"))
            return
        if parsed.path == "/save":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            target = form.get("path", [""])[0]
            content = form.get("content", [""])[0]
            try:
                p = _safe(target)
                p.write_text(content, encoding="utf-8", errors="replace")
                parent = str(p.parent.relative_to(ROOT) or "/")
                if parent == ".":
                    parent = "/"
                self._redirect("/?token=" + TOKEN + "&path=" + urllib.parse.quote("/" + parent.strip("/") + ("/" if parent.strip("/") else "")) + "&ok=" + urllib.parse.quote("Saved " + p.name))
            except Exception as exc:
                self._html(_page("Save", "<div class='card'>err</div>", str(exc), "err"))
            return
        self._html(_page("Bad", "<div class='card'>bad request</div>"), 400)

    def _list(self, qs: dict) -> None:
        rel = qs.get("path", ["/"])[0]
        flash = (qs.get("ok") or qs.get("err") or [""])[0]
        fcls = "ok" if qs.get("ok") else ("err" if qs.get("err") else "")
        try:
            cur = _safe(rel)
            if not cur.exists():
                cur = ROOT
            if cur.is_file():
                # redirect to edit/download
                parent = "/" + str(cur.parent.relative_to(ROOT)).strip("/")
                if parent == "/.":
                    parent = "/"
                self._redirect("/edit?token=" + TOKEN + "&path=" + urllib.parse.quote(str(cur.relative_to(ROOT))))
                return
        except Exception as exc:
            self._html(_page("List", "<div class='card'>bad path</div>", str(exc), "err"))
            return

        rel_disp = "/" + str(cur.relative_to(ROOT)).strip("/")
        if rel_disp.startswith("/."):
            rel_disp = rel_disp
        crumbs = ["<a href='/?token=" + TOKEN + "'>/</a>"]
        acc = ""
        parts = [p for p in rel_disp.strip("/").split("/") if p]
        for part in parts:
            acc += part + "/"
            crumbs.append(
                f"<a href='/?token={TOKEN}&path={urllib.parse.quote('/' + acc)}'>{html.escape(part)}</a>"
            )
        crumb_html = " ".join(crumbs)

        rows = []
        try:
            entries = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as exc:
            self._html(_page("List", "<div class='card'>perm</div>", str(exc), "err"))
            return
        for ent in entries:
            name = ent.name
            if ent.is_dir():
                href = f"/?token={TOKEN}&path={urllib.parse.quote((rel_disp.rstrip('/') + '/' + name))}"
                rows.append(
                    f"<tr><td><a class='mono' href='{href}'>📁 {html.escape(name)}/</a></td>"
                    f"<td class='right'>dir</td><td class='actions'>"
                    f"<form method='post' action='/delete' onsubmit=\"return confirm('Delete {html.escape(name)}?')\">"
                    f"<input type='hidden' name='path' value='{html.escape(str(ent))}'>"
                    f"<button class='btn danger' type='submit'>Delete</button></form></td></tr>"
                )
            else:
                try:
                    size = _human(ent.stat().st_size)
                except OSError:
                    size = "?"
                edit = f"/edit?token={TOKEN}&path={urllib.parse.quote(str(ent.relative_to(ROOT)))}"
                dl = f"/download?token={TOKEN}&path={urllib.parse.quote(str(ent.relative_to(ROOT)))}"
                rows.append(
                    f"<tr><td class='mono'><a href='{edit}'>{html.escape(name)}</a></td>"
                    f"<td class='right'>{size}</td><td class='actions'>"
                    f"<a class='btn ghost' href='{edit}'>Edit</a> "
                    f"<a class='btn ghost' href='{dl}'>Download</a> "
                    f"<form method='post' action='/delete' onsubmit=\"return confirm('Delete {html.escape(name)}?')\">"
                    f"<input type='hidden' name='path' value='{html.escape(str(ent))}'>"
                    f"<button class='btn danger' type='submit'>Delete</button></form></td></tr>"
                )

        parent_rel = str(cur.parent.relative_to(ROOT)) if cur != ROOT else ""
        parent_href = f"/?token={TOKEN}" + (
            f"&path={urllib.parse.quote('/' + parent_rel.strip('/'))}" if parent_rel and parent_rel != "." else ""
        )
        up = "" if cur == ROOT else f"<a class='btn ghost' href='{parent_href}'>⬆ Up</a>"

        body = f"""
<div class="crumbs">{crumb_html}</div>
<div class="bar">
  {up}
  <form method="post" action="/mkdir">
    <input type="hidden" name="path" value="{html.escape(str(cur))}">
    <input name="name" placeholder="new folder" required>
    <button type="submit">Mkdir</button>
  </form>
  <form method="post" action="/upload" enctype="multipart/form-data">
    <input type="hidden" name="path" value="{html.escape(str(cur))}">
    <input type="file" name="file" required multiple>
    <button type="submit">Upload</button>
  </form>
  <form method="get" action="/">
    <input type="hidden" name="token" value="{TOKEN}">
    <input name="path" placeholder="/etc" value="{html.escape(rel_disp)}">
    <button type="submit">Go</button>
  </form>
</div>
<table><thead><tr><th>Name</th><th class="right">Size</th><th>Actions</th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan=3>Empty</td></tr>"}</tbody></table>
"""
        self._html(_page(f"Files {rel_disp}", body, flash, fcls))

    def _edit_get(self, qs: dict) -> None:
        try:
            p = _safe(qs.get("path", [""])[0])
            if not p.is_file():
                raise FileNotFoundError("not a file")
            if p.stat().st_size > MAX_EDIT:
                raise ValueError("file too large to edit in browser")
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self._html(_page("Edit", "<div class='card'>error</div>", str(exc), "err"))
            return
        rel = str(p.relative_to(ROOT))
        parent = "/" + str(p.parent.relative_to(ROOT)).strip("/")
        if parent == "/.":
            parent = "/"
        back = f"/?token={TOKEN}" + (
            f"&path={urllib.parse.quote(parent if parent != '/' else '/')}" if p.parent != ROOT else ""
        )
        if p.parent == ROOT:
            back = f"/?token={TOKEN}"
        else:
            back = f"/?token={TOKEN}&path={urllib.parse.quote('/' + str(p.parent.relative_to(ROOT)).strip('/'))}"
        body = f"""
<div class="crumbs"><a href="{back}">← Back</a> · <span class="mono">{html.escape(rel)}</span></div>
<div class="card editor">
  <form method="post" action="/save">
    <input type="hidden" name="path" value="{html.escape(rel)}">
    <textarea name="content" spellcheck="false">{html.escape(text)}</textarea>
    <div class="bar" style="margin-top:12px">
      <button type="submit">Save</button>
      <a class="btn ghost" href="/download?token={TOKEN}&path={urllib.parse.quote(rel)}">Download</a>
      <a class="btn ghost" href="{back}">Cancel</a>
    </div>
  </form>
</div>"""
        self._html(_page(f"Edit {p.name}", body))

    def _download(self, qs: dict) -> None:
        try:
            p = _safe(qs.get("path", [""])[0])
            if not p.is_file():
                raise FileNotFoundError("not a file")
            data = p.read_bytes()
        except Exception as exc:
            self._html(_page("Download", "<div class='card'>err</div>", str(exc), "err"))
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _upload(self, body: bytes, ctype: str, qs_from_body: bool) -> None:
        # parse multipart manually (no external deps)
        boundary = ""
        for part in ctype.split(";"):
            part = part.strip()
            if part.lower().startswith("boundary="):
                boundary = part.split("=", 1)[1].strip().strip('"')
        form_path = "/"
        saved = []
        if boundary:
            b = b"--" + boundary.encode()
            chunks = body.split(b)
            for chunk in chunks:
                if b"\r\n\r\n" not in chunk:
                    continue
                head, _, data = chunk.partition(b"\r\n\r\n")
                data = data[:-2] if data.endswith(b"\r\n") else data
                head_l = head.decode("utf-8", "replace")
                if 'name="path"' in head_l:
                    form_path = data.decode("utf-8", "replace").strip() or "/"
                if 'name="file"' in head_l and "filename=" in head_l:
                    fname = head_l.split("filename=", 1)[1].split("\r\n", 1)[0].strip().strip('"')
                    if not fname:
                        continue
                    dest_dir = _safe(form_path)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / Path(fname).name
                    dest.write_bytes(data)
                    saved.append(dest.name)
        if not saved:
            self._html(_page("Upload", "<div class='card'>no files</div>", "No file received", "err"))
            return
        parent = "/" + str(_safe(form_path).relative_to(ROOT)).strip("/")
        loc = f"/?token={TOKEN}"
        if parent and parent != "/":
            loc += f"&path={urllib.parse.quote('/' + parent)}"
        loc += "&ok=" + urllib.parse.quote("Uploaded " + ", ".join(saved))
        self._redirect(loc)


def main() -> None:
    global TOKEN, ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", required=True)
    ap.add_argument("--root", default="/")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    TOKEN = args.token
    ROOT = Path(args.root).resolve()
    if not ROOT.is_dir():
        ROOT = Path("/")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"VexFM listening on {args.host}:{args.port} root={ROOT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
'''


def build_start_script(token: str, port: int = 8765) -> str:
    """Free non-auth localhost.run tunnel: ssh -R 80:localhost:PORT localhost.run"""
    import base64

    fm_b64 = base64.b64encode(FILE_MANAGER_PY.encode("utf-8")).decode("ascii")
    p = int(port)
    t = token
    return f"""set +e
export DEBIAN_FRONTEND=noninteractive
if ! command -v python3 >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq python3 openssh-client curl ca-certificates >/dev/null 2>&1 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 openssh-client curl ca-certificates >/dev/null 2>&1 || true
  fi
fi
command -v python3 >/dev/null 2>&1 || {{ echo NO_PYTHON; exit 2; }}
if [ -f /tmp/vex-fm.pid ]; then kill "$(cat /tmp/vex-fm.pid)" >/dev/null 2>&1 || true; fi
if [ -f /tmp/vex-tunnel.pid ]; then kill "$(cat /tmp/vex-tunnel.pid)" >/dev/null 2>&1 || true; fi
rm -f /tmp/vex-fm.log /tmp/vex-fm.pid /tmp/vex-tunnel.log /tmp/vex-tunnel.pid
echo {fm_b64} | base64 -d > /tmp/vex-fm.py || {{ echo FM_WRITE_FAIL; exit 3; }}
setsid python3 /tmp/vex-fm.py --host 127.0.0.1 --port {p} --token {t} --root / >/tmp/vex-fm.log 2>&1 &
echo $! > /tmp/vex-fm.pid
sleep 1
if ! kill -0 "$(cat /tmp/vex-fm.pid)" 2>/dev/null; then
  echo FM_START_FAIL
  cat /tmp/vex-fm.log 2>/dev/null || true
  exit 4
fi
command -v ssh >/dev/null 2>&1 || {{
  command -v apt-get >/dev/null 2>&1 && apt-get install -y -qq openssh-client >/dev/null 2>&1 || true
}}
printf '#!/bin/sh\\necho\\n' > /tmp/vex-askpass.sh
chmod +x /tmp/vex-askpass.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/vex-askpass.sh SSH_ASKPASS_REQUIRE=force
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -o NumberOfPasswordPrompts=1"
start_tunnel() {{
  HOST="$1"
  setsid ssh $SSH_OPTS -R 80:127.0.0.1:{p} "$HOST" </dev/null >/tmp/vex-tunnel.log 2>&1 &
  echo $! > /tmp/vex-tunnel.pid
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    URL=$(grep -Eio 'https://[A-Za-z0-9._-]+\\.localhost\\.run' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
    if [ -z "$URL" ]; then
      HOSTLINE=$(grep -Eio '[A-Za-z0-9._-]+\\.localhost\\.run' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
      if [ -n "$HOSTLINE" ]; then URL="https://$HOSTLINE"; fi
    fi
    if [ -n "$URL" ]; then return 0; fi
    if [ -f /tmp/vex-tunnel.pid ]; then
      PID=$(cat /tmp/vex-tunnel.pid 2>/dev/null)
      if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then return 1; fi
    fi
    sleep 1
  done
  return 1
}}
URL=""
for HOST in localhost.run nokey@localhost.run; do
  if [ -f /tmp/vex-tunnel.pid ]; then kill "$(cat /tmp/vex-tunnel.pid)" >/dev/null 2>&1 || true; fi
  if start_tunnel "$HOST"; then
    break
  fi
done
if [ -z "$URL" ]; then
  URL=$(grep -Eio 'https://[A-Za-z0-9._-]+\\.localhost\\.run' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
  if [ -z "$URL" ]; then
    HOSTLINE=$(grep -Eio '[A-Za-z0-9._-]+\\.localhost\\.run' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
    if [ -n "$HOSTLINE" ]; then URL="https://$HOSTLINE"; fi
  fi
fi
echo "TOKEN={t}"
echo "PORT={p}"
echo "URL=${{URL:-}}"
if [ -n "$URL" ]; then
  echo FM_OK
else
  echo FM_TUNNEL_FAIL
  tail -n 40 /tmp/vex-tunnel.log 2>/dev/null || true
  exit 5
fi
"""


def build_stop_script() -> str:
    return (
        "set +e; "
        "if [ -f /tmp/vex-fm.pid ]; then kill \"$(cat /tmp/vex-fm.pid)\" >/dev/null 2>&1 || true; "
        "  rm -f /tmp/vex-fm.pid; fi; "
        "if [ -f /tmp/vex-tunnel.pid ]; then kill \"$(cat /tmp/vex-tunnel.pid)\" >/dev/null 2>&1 || true; "
        "  rm -f /tmp/vex-tunnel.pid; fi; "
        "rm -f /tmp/vex-tunnel.log; "
        "echo FM_STOPPED"
    )
