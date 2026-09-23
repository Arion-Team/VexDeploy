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
:root {
  --bg:#070a14; --bg2:#0b1020; --card:#121a33; --card2:#0e1530;
  --fg:#e8ecff; --muted:#8b93b8; --line:#243056; --line2:#1d2748;
  --acc:#3dd6c6; --acc-dim:rgba(61,214,198,.14); --danger:#ff5c7a;
  --ok:#3dff9a; --warn:#ffc857; --r:14px;
  --shadow:0 12px 32px rgba(0,0,0,.38);
}
* { box-sizing:border-box; }
html { color-scheme:dark; }
body {
  margin:0; min-height:100vh; color:var(--fg);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  background:
    radial-gradient(1100px 520px at 8% -10%, rgba(61,214,198,.13), transparent 55%),
    radial-gradient(900px 480px at 100% 0%, rgba(96,128,255,.11), transparent 50%),
    linear-gradient(180deg,var(--bg),var(--bg2) 40%,#0d1430);
  background-attachment:fixed;
}
a { color:var(--acc); text-decoration:none; }
a:hover { filter:brightness(1.1); }
.shell { max-width:1120px; margin:0 auto; padding:18px 16px 48px; }
.top {
  display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between;
  background:linear-gradient(180deg,rgba(20,28,54,.95),rgba(14,20,40,.95));
  border:1px solid var(--line); border-radius:var(--r);
  padding:14px 16px; box-shadow:var(--shadow);
  backdrop-filter:blur(8px);
}
.brand { display:flex; gap:12px; align-items:center; }
.logo {
  width:40px; height:40px; border-radius:12px; display:grid; place-items:center;
  font-size:18px; font-weight:800; color:#04121a;
  background:linear-gradient(145deg,var(--acc),#5ee0c8 55%,#2a9f94);
  box-shadow:0 6px 18px rgba(61,214,198,.35);
}
.brand-name { font-size:17px; font-weight:750; letter-spacing:.2px; line-height:1.15; }
.brand-sub { color:var(--muted); font-size:12px; letter-spacing:.4px; text-transform:uppercase; }
.top-meta { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.pill {
  font-size:11px; font-weight:650; letter-spacing:.3px; text-transform:uppercase;
  color:#9ff0e6; background:var(--acc-dim); border:1px solid rgba(61,214,198,.35);
  border-radius:999px; padding:5px 10px;
}
.pill.dim { color:var(--muted); background:rgba(255,255,255,.04); border-color:var(--line); text-transform:none; font-weight:550; }
.flash {
  display:flex; gap:10px; align-items:flex-start; margin:14px 0 0; padding:12px 14px;
  border-radius:12px; background:rgba(255,255,255,.04); border:1px solid var(--line);
  box-shadow:0 4px 14px rgba(0,0,0,.2);
}
.flash::before { content:"i"; flex:0 0 auto; width:22px; height:22px; border-radius:999px;
  display:grid; place-items:center; font-weight:800; font-size:12px;
  background:rgba(61,214,198,.18); color:var(--acc); }
.flash.ok { border-color:rgba(61,255,154,.35); }
.flash.ok::before { content:"✓"; background:rgba(61,255,154,.15); color:var(--ok); }
.flash.err { border-color:rgba(255,92,122,.4); color:#ffc0cc; }
.flash.err::before { content:"!"; background:rgba(255,92,122,.18); color:var(--danger); }
.crumbs {
  display:flex; flex-wrap:wrap; align-items:center; gap:6px;
  margin:16px 0 12px; padding:10px 12px; border-radius:12px;
  background:rgba(14,21,48,.8); border:1px solid var(--line); color:var(--muted);
  word-break:break-all;
}
.crumbs a {
  display:inline-flex; align-items:center; padding:3px 9px; border-radius:8px;
  background:rgba(61,214,198,.08); border:1px solid transparent; color:var(--acc);
}
.crumbs a:hover { border-color:rgba(61,214,198,.35); text-decoration:none; }
.crumbs .sep { opacity:.45; }
.toolbar {
  display:grid; gap:10px; margin:0 0 14px; padding:12px;
  background:var(--card); border:1px solid var(--line); border-radius:var(--r);
}
.toolbar-row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.toolbar form { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.toolbar input[type=text], .toolbar input[type=file], .toolbar input:not([type]) {
  background:var(--card2); color:var(--fg); border:1px solid #2a365c;
  border-radius:10px; padding:9px 11px; font:inherit; min-width:0;
}
.toolbar input[type=text]:focus, .toolbar input:not([type]):focus {
  outline:none; border-color:rgba(61,214,198,.55); box-shadow:0 0 0 3px rgba(61,214,198,.12);
}
.toolbar input[type=file] { padding:7px 9px; max-width:240px; }
.toolbar button, .btn {
  display:inline-flex; align-items:center; justify-content:center; gap:6px;
  background:linear-gradient(180deg,#1f8f83,#17655d); border:1px solid transparent;
  color:#eafffc; font-weight:650; font:inherit; padding:9px 13px;
  border-radius:10px; cursor:pointer; text-decoration:none;
  box-shadow:0 4px 12px rgba(23,101,93,.35); transition:transform .12s ease, filter .12s ease;
}
.toolbar button:hover, .btn:hover { filter:brightness(1.08); text-decoration:none; transform:translateY(-1px); }
.toolbar button:active, .btn:active { transform:translateY(0); }
.btn.danger {
  background:linear-gradient(180deg,#b12d4c,#7c1b34);
  box-shadow:0 4px 12px rgba(124,27,52,.35);
}
.btn.ghost {
  background:rgba(24,34,68,.9); border:1px solid #2a365c; color:#c9d2f5;
  box-shadow:none;
}
.btn.sm { padding:6px 10px; font-size:12.5px; border-radius:8px; }
.field-label { display:none; }
.drop {
  position:relative; display:flex; flex-wrap:wrap; gap:10px; align-items:center;
  padding:12px; border-radius:12px; border:1.5px dashed #2f3f6d;
  background:linear-gradient(180deg,rgba(61,214,198,.05),rgba(255,255,255,.02));
  transition:border-color .15s ease, background .15s ease;
}
.drop.on { border-color:var(--acc); background:rgba(61,214,198,.1); }
.drop-hint { color:var(--muted); font-size:12.5px; flex:1 1 160px; min-width:140px; }
.drop-hint strong { color:#b7f5ec; font-weight:650; }
.table-card {
  background:var(--card); border:1px solid var(--line); border-radius:var(--r);
  overflow:hidden; box-shadow:var(--shadow);
}
table { width:100%; border-collapse:collapse; }
th, td { padding:11px 14px; border-bottom:1px solid var(--line2); text-align:left; vertical-align:middle; }
th {
  position:sticky; top:0; z-index:1;
  color:var(--muted); font-weight:650; font-size:11.5px; letter-spacing:.6px;
  text-transform:uppercase; background:#101736;
}
tr:last-child td { border-bottom:none; }
tbody tr { transition:background .12s ease; }
tbody tr:hover td { background:rgba(61,214,198,.05); }
.name-cell { display:flex; align-items:center; gap:9px; min-width:0; }
.ico { flex:0 0 auto; width:1.35em; text-align:center; filter:saturate(.95); }
.name-cell a { font-family:ui-monospace,Consolas,monospace; font-size:13px; word-break:break-all; }
tr.is-dir .name-cell a { font-weight:650; color:#9ff0e6; }
.type-tag {
  margin-left:2px; font-size:10px; font-weight:700; letter-spacing:.4px;
  color:var(--muted); background:rgba(255,255,255,.05);
  border:1px solid var(--line); border-radius:999px; padding:2px 7px;
}
.mono { font-family:ui-monospace,Consolas,monospace; font-size:13px; }
.right { text-align:right; color:var(--muted); white-space:nowrap; }
.actions { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
.actions form { display:inline-flex; }
.empty {
  padding:36px 16px; text-align:center; color:var(--muted);
}
.empty .big { display:block; font-size:28px; margin-bottom:8px; opacity:.85; }
.card {
  background:var(--card); border:1px solid var(--line); border-radius:var(--r);
  padding:16px; box-shadow:var(--shadow);
}
.editor-head {
  display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:space-between;
  margin-bottom:10px;
}
.editor-head .path {
  font-family:ui-monospace,Consolas,monospace; font-size:13px; color:#b7f5ec;
  background:var(--acc-dim); border:1px solid rgba(61,214,198,.28);
  border-radius:999px; padding:5px 11px; word-break:break-all;
}
.editor textarea {
  width:100%; min-height:440px; background:#0a0f22; color:#e8ecff;
  border:1px solid #2a365c; border-radius:12px; padding:14px;
  font:13px/1.5 ui-monospace,Consolas,monospace; resize:vertical;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.03);
}
.editor textarea:focus {
  outline:none; border-color:rgba(61,214,198,.55);
  box-shadow:0 0 0 3px rgba(61,214,198,.12);
}
.editor-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.foot {
  margin-top:18px; text-align:center; color:var(--muted); font-size:12px;
}
.foot code {
  color:#9ff0e6; background:rgba(61,214,198,.1); border:1px solid rgba(61,214,198,.2);
  border-radius:6px; padding:1px 6px;
}
.hide { display:none !important; }
@media (max-width:760px) {
  .shell { padding:12px 10px 36px; }
  th:nth-child(2), td:nth-child(2) { display:none; }
  .actions { justify-content:flex-start; }
  .toolbar input[type=file] { max-width:100%; }
  .btn, .toolbar button { padding:8px 11px; }
}
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
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Unauthorized</title><style>" + CSS + "</style></head>"
            "<body><div class='shell'><div class='top'><div class='brand'>"
            "<span class='logo'>&#9670;</span><div>"
            "<div class='brand-name'>VexDeploy</div>"
            "<div class='brand-sub'>File Manager</div></div></div>"
            "<div class='top-meta'><span class='pill'>locked</span></div></div>"
            "<div class='card' style='margin-top:16px'><h2 style='margin:0 0 8px'>Unauthorized</h2>"
            f"<p style='color:var(--muted);margin:0'>Add token: <code class='mono'>{html.escape('/?token=' + TOKEN)}</code></p>"
            "</div></div></body></html>"
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


_EXT_ICONS = {
    ".py": "🐍", ".js": "📜", ".ts": "📜", ".jsx": "📜", ".tsx": "📜",
    ".json": "{}", ".sh": "🖥️", ".bash": "🖥️", ".md": "📝", ".txt": "📄",
    ".log": "📋", ".yml": "⚙️", ".yaml": "⚙️", ".toml": "⚙️", ".cfg": "⚙️",
    ".conf": "⚙️", ".ini": "⚙️", ".env": "🔐", ".key": "🔐", ".crt": "🔐",
    ".pem": "🔐", ".zip": "📦", ".tar": "📦", ".gz": "📦", ".tgz": "📦",
    ".7z": "📦", ".rar": "📦", ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️",
    ".gif": "🖼️", ".svg": "🖼️", ".webp": "🖼️", ".html": "🌐", ".htm": "🌐",
    ".css": "🎨", ".scss": "🎨", ".db": "🗄️", ".sqlite": "🗄️", ".sql": "🗄️",
    ".pdf": "📕", ".csv": "📊", ".xlsx": "📊", ".exe": "🧩", ".bin": "🧩",
}


def _file_icon(name: str, is_dir: bool) -> str:
    if is_dir:
        return "📁"
    return _EXT_ICONS.get(Path(name).suffix.lower(), "📄")


def _kind(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    ext = Path(name).suffix.lower().lstrip(".")
    return ext or "file"


def _note(msg: str) -> str:
    safe = html.escape(msg or "Something went wrong")
    return (
        "<div class='card'><div class='empty'>"
        "<span class='big'>&#9888;&#65039;</span>"
        f"{safe}</div></div>"
    )


def _page(title: str, body: str, flash: str = "", flash_cls: str = "") -> bytes:
    flash_html = f'<div class="flash {flash_cls}"><span>{html.escape(flash)}</span></div>' if flash else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body>
<div class="shell">
<header class="top">
  <div class="brand">
    <span class="logo">&#9670;</span>
    <div>
      <div class="brand-name">VexDeploy</div>
      <div class="brand-sub">File Manager</div>
    </div>
  </div>
  <div class="top-meta">
    <span class="pill">token-protected</span>
    <span class="pill dim">127.0.0.1 &middot; localhost.run</span>
  </div>
</header>
{flash_html}
{body}
<footer class="foot">Bound to <code>127.0.0.1</code> &middot; exposed via localhost.run &middot; do not share the full URL with token</footer>
</div>
{_DROP_JS}
</body></html>"""
    return doc.encode("utf-8", errors="replace")


_DROP_JS = """
<script>
(function(){
  var d=document.getElementById("drop");
  if(!d) return;
  ["dragenter","dragover"].forEach(function(e){
    d.addEventListener(e,function(ev){ev.preventDefault();d.classList.add("on");});
  });
  ["dragleave","drop"].forEach(function(e){
    d.addEventListener(e,function(ev){ev.preventDefault();d.classList.remove("on");});
  });
  d.addEventListener("drop",function(ev){
    var f=document.getElementById("upfile");
    if(f&&ev.dataTransfer&&ev.dataTransfer.files&&ev.dataTransfer.files.length){
      f.files=ev.dataTransfer.files;
      if(f.form) f.form.submit();
    }
  });
})();
</script>
"""


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
                self._html(_page("Delete", _note(str(exc) or "Delete failed"), str(exc), "err"))
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
                self._html(_page("Mkdir", _note(str(exc) or "Create folder failed"), str(exc), "err"))
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
                self._html(_page("Save", _note(str(exc) or "Save failed"), str(exc), "err"))
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
            self._html(_page("List", _note("bad path"), str(exc), "err"))
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

        rows = []
        try:
            entries = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as exc:
            self._html(_page("List", _note("Permission denied"), str(exc), "err"))
            return
        for ent in entries:
            name = ent.name
            if ent.is_dir():
                href = f"/?token={TOKEN}&path={urllib.parse.quote((rel_disp.rstrip('/') + '/' + name))}"
                rows.append(
                    f"<tr class='is-dir'><td><div class='name-cell'>"
                    f"<span class='ico'>{_file_icon(name, True)}</span>"
                    f"<a class='mono' href='{href}'>{html.escape(name)}</a>"
                    f"<span class='type-tag'>folder</span></div></td>"
                    f"<td class='right'>dir</td><td class='actions'>"
                    f"<a class='btn ghost sm' href='{href}'>Open</a>"
                    f"<form method='post' action='/delete' onsubmit=\"return confirm('Delete {html.escape(name)}?')\">"
                    f"<input type='hidden' name='path' value='{html.escape(str(ent))}'>"
                    f"<button class='btn danger sm' type='submit'>Delete</button></form></td></tr>"
                )
            else:
                try:
                    size = _human(ent.stat().st_size)
                except OSError:
                    size = "?"
                edit = f"/edit?token={TOKEN}&path={urllib.parse.quote(str(ent.relative_to(ROOT)))}"
                dl = f"/download?token={TOKEN}&path={urllib.parse.quote(str(ent.relative_to(ROOT)))}"
                rows.append(
                    f"<tr><td><div class='name-cell'>"
                    f"<span class='ico'>{_file_icon(name, False)}</span>"
                    f"<a class='mono' href='{edit}'>{html.escape(name)}</a>"
                    f"<span class='type-tag'>{html.escape(_kind(name, False))}</span></div></td>"
                    f"<td class='right'>{size}</td><td class='actions'>"
                    f"<a class='btn ghost sm' href='{edit}'>Edit</a> "
                    f"<a class='btn ghost sm' href='{dl}'>Download</a> "
                    f"<form method='post' action='/delete' onsubmit=\"return confirm('Delete {html.escape(name)}?')\">"
                    f"<input type='hidden' name='path' value='{html.escape(str(ent))}'>"
                    f"<button class='btn danger sm' type='submit'>Delete</button></form></td></tr>"
                )

        parent_rel = str(cur.parent.relative_to(ROOT)) if cur != ROOT else ""
        parent_href = f"/?token={TOKEN}" + (
            f"&path={urllib.parse.quote('/' + parent_rel.strip('/'))}" if parent_rel and parent_rel != "." else ""
        )
        up = "" if cur == ROOT else f"<a class='btn ghost' href='{parent_href}'>&#8593; Up</a>"

        crumb_html = " <span class='sep'>/</span> ".join(crumbs)
        n_dirs = sum(1 for e in entries if e.is_dir())
        n_files = len(entries) - n_dirs
        body = f"""
<div class="crumbs">{crumb_html}</div>
<div class="toolbar">
  <div class="toolbar-row">
    {up}
    <span class="pill dim">{n_dirs} folders &middot; {n_files} files</span>
    <form method="get" action="/">
      <input type="hidden" name="token" value="{TOKEN}">
      <input type="text" name="path" placeholder="/etc" value="{html.escape(rel_disp)}">
      <button type="submit">Go</button>
    </form>
  </div>
  <div class="toolbar-row">
    <form method="post" action="/mkdir">
      <input type="hidden" name="path" value="{html.escape(str(cur))}">
      <input type="text" name="name" placeholder="new folder" required>
      <button type="submit">+ Folder</button>
    </form>
  </div>
  <div class="drop" id="drop">
    <form method="post" action="/upload" enctype="multipart/form-data">
      <input type="hidden" name="path" value="{html.escape(str(cur))}">
      <input type="file" id="upfile" name="file" required multiple>
      <button type="submit">Upload</button>
    </form>
    <div class="drop-hint"><strong>Drop files here</strong> or use Upload &middot; stays in this folder</div>
  </div>
</div>
<div class="table-card">
<table><thead><tr><th>Name</th><th class="right">Size</th><th class="right">Actions</th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan=3><div class='empty'><span class='big'>&#128193;</span>This folder is empty</div></td></tr>"}</tbody></table>
</div>
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
            self._html(_page("Edit", _note(str(exc) or "Cannot edit file"), str(exc), "err"))
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
<div class="crumbs"><a href="{back}">&#8592; Back</a> <span class="sep">/</span> <span class="mono">{html.escape(rel)}</span></div>
<div class="card editor">
  <div class="editor-head">
    <span class="path">{html.escape(p.name)}</span>
    <span class="pill dim">{html.escape(_human(p.stat().st_size))}</span>
  </div>
  <form method="post" action="/save">
    <input type="hidden" name="path" value="{html.escape(rel)}">
    <textarea name="content" spellcheck="false">{html.escape(text)}</textarea>
    <div class="editor-actions">
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
            self._html(_page("Download", _note(str(exc) or "Download failed"), str(exc), "err"))
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
            self._html(_page("Upload", _note("No file received"), "No file received", "err"))
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
    """Free non-auth tunnel: ssh -R 80:localhost:PORT nokey@localhost.run"""
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
extract_url() {{
  U=$(grep -Eio 'https://[A-Za-z0-9._-]+\\.(localhost\\.run|lhr\\.life|lhrtunnel\\.link|lhr\\.rocks|lhr\\.link)[A-Za-z0-9._/-]*' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
  if [ -z "$U" ]; then
    H=$(grep -Eio '[A-Za-z0-9._-]+\\.(localhost\\.run|lhr\\.life|lhrtunnel\\.link|lhr\\.rocks|lhr\\.link)' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
    if [ -n "$H" ]; then U="https://$H"; fi
  fi
  echo "$U"
}}
setsid ssh $SSH_OPTS -R 80:127.0.0.1:{p} nokey@localhost.run </dev/null >/tmp/vex-tunnel.log 2>&1 &
echo $! > /tmp/vex-tunnel.pid
URL=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  URL=$(extract_url)
  if [ -n "$URL" ]; then break; fi
  if [ -f /tmp/vex-tunnel.pid ]; then
    PID=$(cat /tmp/vex-tunnel.pid 2>/dev/null)
    if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then break; fi
  fi
  sleep 1
done
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
