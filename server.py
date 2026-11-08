#!/usr/bin/env python3
"""Mobile-oriented file browser: file tree, cross-file grep, syntax highlighting, pinch zoom.

Run from the directory you want to serve:
    fb        # (if this script is on PATH, e.g. ~/bin/fb -> server.py)
    ./server.py
    python3 server.py

Prints URLs with a token for each network interface (incl. tailscale).
Open one on your phone (same wifi) or tailnet.
"""
from __future__ import annotations

import argparse
import codecs
import errno
import html
import json
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path.cwd().resolve()
TOKEN = ""  # set in main(); empty = auth disabled (--no-token)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_GREP_MATCHES = 500
MAX_GREP_FILE_BYTES = 4 * 1024 * 1024
TAIL_THRESHOLD = 100 * 1024
TAIL_LINES = 500
TAIL_MAX_FILE_BYTES = 64 * 1024 * 1024

IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    "bmp": "image/bmp", "ico": "image/x-icon", "avif": "image/avif",
}
VIDEO_MIME = {
    "mp4": "video/mp4", "m4v": "video/x-m4v", "webm": "video/webm",
    "mov": "video/quicktime", "mkv": "video/x-matroska",
    "ogv": "video/ogg", "avi": "video/x-msvideo",
}
AUDIO_MIME = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/opus",
    "flac": "audio/flac", "aac": "audio/aac",
}
MODEL_MIME = {
    "stl": "model/stl",
    "step": "model/step", "stp": "model/step",
}
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".next",
    ".cache", ".tox", ".idea", ".vscode",
}


def safe_path(rel: str) -> Path:
    rel = rel.lstrip("/")
    p = (ROOT / rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError("path escapes root")
    return p


def is_probably_text(buf: bytes) -> bool:
    if not buf:
        return True
    head = buf[:4096]
    if b"\x00" in head:
        return False
    # incremental decode tolerates a multibyte char cut off at the buffer boundary
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(head, final=False)
        return True
    except UnicodeDecodeError:
        return False


def list_dir(path: Path) -> list[dict]:
    entries: list[dict] = []
    try:
        children = sorted(path.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))
    except (PermissionError, OSError):
        return entries
    for child in children:
        if child.name in SKIP_DIRS:
            continue
        entry = {"name": child.name, "type": "dir" if child.is_dir() else "file"}
        if child.is_file():
            try:
                entry["size"] = child.stat().st_size
            except OSError:
                entry["size"] = 0
        entries.append(entry)
    return entries


def do_grep(query: str, ignore_case: bool) -> dict:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        pattern = re.compile(query, flags)
    except re.error:
        pattern = re.compile(re.escape(query), flags)

    results: list[dict] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            p = Path(dirpath) / fname
            try:
                st = p.stat()
                if st.st_size > MAX_GREP_FILE_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:2048]:
                continue
            text = data.decode("utf-8", errors="replace")
            matches = []
            for i, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append({"line": i, "text": line[:400]})
                    total += 1
                    if total >= MAX_GREP_MATCHES:
                        break
            if matches:
                results.append({
                    "path": str(p.relative_to(ROOT)),
                    "matches": matches,
                })
            if total >= MAX_GREP_MATCHES:
                break
        if total >= MAX_GREP_MATCHES:
            break
    return {"total": total, "truncated": total >= MAX_GREP_MATCHES, "results": results}


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="color-scheme" content="dark">
<title>__FB_TITLE__</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/styles/github-dark.min.css">
<style>
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; height: 100%; overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background: #0d1117; color: #c9d1d9; -webkit-text-size-adjust: none; }
button { font: inherit; }

#topbar { display: flex; align-items: center; gap: 6px; padding: 6px 8px;
  background: #161b22; border-bottom: 1px solid #30363d; height: 56px; }
#topbar button { background: #21262d; border: 1px solid #30363d;
  color: #c9d1d9; padding: 0 14px; border-radius: 6px;
  font-size: 16px; height: 44px; min-width: 48px; }
#topbar button:active { background: #30363d; }
#title { flex: 1; font-size: 14px; overflow: hidden; white-space: nowrap;
  text-overflow: ellipsis; opacity: 0.85; padding: 0 4px; direction: rtl; text-align: left; }
#title > span { direction: ltr; unicode-bidi: bidi-override; }

#main { position: relative; height: calc(100% - 56px); }

#sidebar { position: absolute; left: 0; top: 0; bottom: 0;
  width: 85%; max-width: 420px; background: #0d1117;
  border-right: 1px solid #30363d; transform: translateX(-100%);
  transition: transform .2s ease; z-index: 10;
  overflow-y: auto; overflow-x: hidden; -webkit-overflow-scrolling: touch; }
#sidebar.open { transform: translateX(0); box-shadow: 2px 0 20px rgba(0,0,0,.5); }
.tree { padding: 8px; font-size: 15px; }
.node { padding: 12px 10px; display: flex; align-items: center; gap: 8px;
  border-radius: 4px; white-space: nowrap; cursor: pointer; user-select: none;
  min-height: 44px; }
.node:active { background: #21262d; }
.node.sel { background: #1f6feb33; }
.node .icon { width: 18px; flex-shrink: 0; text-align: center;
  opacity: 0.7; font-size: 13px; }
.node .name { overflow: hidden; text-overflow: ellipsis; }
.children { padding-left: 16px; }

#overlay { position: absolute; inset: 0; background: rgba(0,0,0,.5);
  z-index: 9; opacity: 0; pointer-events: none; transition: opacity .2s; }
#overlay.show { opacity: 1; pointer-events: auto; }

#content { position: absolute; inset: 0; overflow: auto;
  -webkit-overflow-scrolling: touch; }
#viewerWrap { margin: 0; padding: 12px 14px 60vh;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px; line-height: 1.55;
  white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word;
  tab-size: 2; background: transparent; overflow: visible; }
#viewerWrap code { display: block; background: transparent !important;
  padding: 0 !important; overflow: visible !important; }
.hlmark { background: #9e6a0366; border-radius: 2px; }

#tailNote { padding: 8px 14px; background: #21262d;
  border-bottom: 1px solid #30363d; font-size: 12px;
  color: #8b949e; position: sticky; top: 0; z-index: 1;
  display: flex; align-items: center; gap: 10px; }
#tailNote button { background: #30363d; border: 1px solid #484f58;
  color: #c9d1d9; border-radius: 6px; padding: 0 14px;
  font-size: 12px; height: 30px; flex-shrink: 0; margin-left: auto; }
#tailNote button:active { background: #484f58; }

#viewTabs { display: none; position: sticky; top: 0; z-index: 2;
  gap: 6px; padding: 8px; background: #0d1117;
  border-bottom: 1px solid #30363d; }
#viewTabs.show { display: flex; }
#viewTabs button { flex: 1; height: 40px; background: #161b22;
  border: 1px solid #30363d; color: #8b949e;
  border-radius: 6px; font-size: 14px; }
#viewTabs button.active { background: #1f6feb33; color: #c9d1d9;
  border-color: #1f6feb; }

#mdWrap { padding: 16px 14px 60vh; font-size: 16px; line-height: 1.6;
  max-width: 860px; overflow-wrap: break-word; }
#mdWrap h1, #mdWrap h2 { border-bottom: 1px solid #30363d;
  padding-bottom: .3em; }
#mdWrap h1 { font-size: 1.6em; } #mdWrap h2 { font-size: 1.35em; }
#mdWrap h3 { font-size: 1.15em; }
#mdWrap a { color: #58a6ff; }
#mdWrap img { max-width: 100%; height: auto; background: #161b22;
  border-radius: 4px; }
#mdWrap code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: #6e768166; padding: .15em .35em; border-radius: 4px;
  font-size: .88em; }
#mdWrap pre { background: #161b22; border: 1px solid #30363d;
  padding: 12px; border-radius: 6px; overflow-x: auto; }
#mdWrap pre code { background: transparent; padding: 0; display: block;
  font-size: .85em; line-height: 1.5; }
#mdWrap .mermaid { margin: 0 0 16px; overflow-x: auto; text-align: center;
  white-space: pre-wrap; }
#mdWrap .mermaid svg { max-width: 100%; height: auto; }
#mdWrap blockquote { border-left: 3px solid #30363d; margin: 0 0 16px;
  padding: 0 1em; color: #8b949e; }
#mdWrap table { border-collapse: collapse; display: block;
  overflow-x: auto; max-width: 100%; }
#mdWrap th, #mdWrap td { border: 1px solid #30363d; padding: 6px 12px; }
#mdWrap th { background: #161b22; }
#mdWrap hr { border: 0; border-top: 1px solid #30363d; margin: 20px 0; }
#mdWrap ul, #mdWrap ol { padding-left: 1.6em; }
#mdWrap li { margin: .2em 0; }
#mdWrap input[type=checkbox] { margin-right: 6px; }

#imageWrap { padding: 12px; text-align: center; }
#imageView { max-width: 100%; height: auto;
  background: #161b22; border-radius: 4px;
  image-rendering: -webkit-optimize-contrast; }
#imageView.actual { max-width: none; cursor: zoom-out; }
#imageView:not(.actual) { cursor: zoom-in; }

#mediaWrap { padding: 12px; }
#videoView { display: block; width: 100%; max-height: 75vh;
  background: #000; border-radius: 6px; }
#audioView { display: block; width: 100%; }
#mediaMeta { display: flex; align-items: center; gap: 12px;
  margin-top: 12px; font-size: 13px; color: #8b949e; }
#mediaMeta .dlBtn { margin-left: auto; display: inline-flex;
  align-items: center; height: 40px; padding: 0 18px;
  background: #238636; border-radius: 6px; color: #fff;
  text-decoration: none; font-size: 14px; }
#mediaMeta .dlBtn:active { background: #2ea043; }

#modelWrap { padding: 12px; }
#modelStage { position: relative; width: 100%; height: calc(100vh - 140px);
  min-height: 260px; max-height: 75vh; background: #161b22;
  border: 1px solid #30363d; border-radius: 6px; overflow: hidden;
  touch-action: none; user-select: none; -webkit-user-select: none; }
#modelCanvas { display: block; width: 100%; height: 100%; outline: none; }
#modelStatus { position: absolute; inset: 0; display: flex;
  align-items: center; justify-content: center; font-size: 14px;
  color: #8b949e; pointer-events: none; background: rgba(13,17,23,.55); }
#modelMeta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-top: 12px; font-size: 13px; color: #8b949e; }
#modelMeta .metaText { margin-right: auto; }
#modelMeta .ctrlBtn { height: 40px; padding: 0 14px; background: #21262d;
  border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9;
  font-size: 14px; }
#modelMeta .ctrlBtn:active { background: #30363d; }
#modelMeta .ctrlBtn.active { background: #1f6feb33; border-color: #1f6feb; color: #c9d1d9; }
#modelMeta .dlBtn { display: inline-flex; align-items: center; height: 40px;
  padding: 0 18px; background: #238636; border-radius: 6px; color: #fff;
  text-decoration: none; font-size: 14px; }
#modelMeta .dlBtn:active { background: #2ea043; }
#modelParts { margin-top: 12px; border: 1px solid #30363d;
  border-radius: 6px; background: #161b22; overflow: hidden; }
.mp-head { display: flex; align-items: center; gap: 6px;
  padding: 8px 10px; border-bottom: 1px solid #30363d;
  font-size: 13px; color: #8b949e; }
.mp-head span { margin-right: auto; }
.mp-btn { height: 32px; padding: 0 12px; background: #21262d;
  border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9;
  font-size: 12px; flex-shrink: 0; }
.mp-btn:active { background: #30363d; }
.mp-list { max-height: 260px; overflow-y: auto; -webkit-overflow-scrolling: touch; }
.mp-row { display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-bottom: 1px solid #21262d;
  font-size: 14px; min-height: 44px; cursor: pointer; user-select: none; }
.mp-row:last-child { border-bottom: none; }
.mp-row:active { background: #21262d; }
.mp-row.off { opacity: 0.5; }
.mp-row input[type=checkbox] { width: 18px; height: 18px; margin: 0; flex-shrink: 0; }
.mp-swatch { width: 12px; height: 12px; border-radius: 3px;
  border: 1px solid rgba(255,255,255,.25); flex-shrink: 0; }
.mp-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mp-tris { font-size: 12px; color: #8b949e; flex-shrink: 0; }

.line { display: block; padding-left: 3.2em; position: relative;
  min-height: 1em; }
.ln { position: absolute; left: 0; width: 2.6em;
  text-align: right; color: #6e7681;
  user-select: none; -webkit-user-select: none;
  cursor: pointer; touch-action: manipulation; }
.ln:active { color: #c9d1d9; background: #30363d; border-radius: 3px; }

.modal { position: absolute; inset: 0; background: rgba(0,0,0,.6);
  display: none; z-index: 20; align-items: center; justify-content: center;
  padding: 16px; }
.modal.show { display: flex; }
.modal-body { background: #161b22; border: 1px solid #30363d;
  border-radius: 8px; padding: 14px; width: 100%; max-width: 520px; }
.modal-body .label { font-size: 13px; opacity: 0.85; margin-bottom: 8px; }
.modal-body textarea { width: 100%; box-sizing: border-box;
  background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
  border-radius: 6px; padding: 8px 10px;
  font-size: 16px; font-family: ui-monospace, monospace; resize: vertical;
  min-height: 80px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end;
  margin-top: 10px; }
.modal-actions button { padding: 10px 18px; border-radius: 6px;
  font-size: 15px; border: none; height: 44px; min-width: 72px; }
.modal-actions .cancel { background: #30363d; color: #c9d1d9; }
.modal-actions .submit { background: #238636; color: #fff; }

#placeholder { padding: 40px 20px; text-align: center;
  opacity: 0.55; font-size: 14px; line-height: 1.7; }

#grep { position: absolute; top: 0; left: 0; right: 0;
  background: #161b22; border-bottom: 1px solid #30363d;
  padding: 8px; z-index: 8; max-height: 70vh;
  flex-direction: column; display: none; }
#grep.show { display: flex; }
#grep .row { display: flex; gap: 6px; align-items: center; }
#grep input[type=text] { flex: 1; background: #0d1117; border: 1px solid #30363d;
  color: #c9d1d9; padding: 11px 12px; border-radius: 6px;
  font-size: 16px; /* prevent iOS zoom */ font-family: ui-monospace, monospace;
  height: 44px; }
#grep .row button { background: #238636; border: none; color: #fff;
  padding: 10px 16px; border-radius: 6px; font-size: 15px; height: 44px; }
#grep #grepClose { background: #30363d; }
#grep .opts { display: flex; gap: 16px; padding: 8px 0 0;
  font-size: 14px; opacity: 0.85; }
#grep label { display: flex; align-items: center; gap: 6px;
  min-height: 32px; padding: 4px 0; }
#grep label input[type=checkbox] { width: 18px; height: 18px; }

#grepResults { overflow: auto; border-top: 1px solid #30363d;
  margin-top: 8px; flex: 1; min-height: 0; }
.gr-file { padding: 10px 12px; font-weight: 600; background: #21262d;
  border-bottom: 1px solid #30363d; font-size: 14px; cursor: pointer;
  position: sticky; top: 0; min-height: 40px; display: flex; align-items: center; }
.gr-hit { padding: 12px 8px 12px 24px; font-family: ui-monospace, monospace;
  font-size: 13px; cursor: pointer; border-bottom: 1px solid #21262d;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  min-height: 44px; display: flex; align-items: center; }
.gr-hit .ln { color: #8b949e; margin-right: 8px; display: inline-block; min-width: 2.5em; text-align: right; }
.gr-hit:active { background: #1f6feb33; }
.gr-msg { padding: 12px; opacity: 0.6; font-size: 13px; }

#toast { position: absolute; bottom: 20px; left: 50%;
  transform: translateX(-50%); background: #161b22;
  border: 1px solid #30363d; padding: 8px 14px; border-radius: 20px;
  font-size: 13px; opacity: 0; transition: opacity .2s;
  pointer-events: none; z-index: 100; max-width: 80%; }
#toast.show { opacity: 1; }
</style>
</head>
<body>
<div id="topbar">
  <button id="menuBtn" aria-label="files">☰</button>
  <button id="grepBtn" aria-label="grep">⌕</button>
  <div id="title"><span>select a file</span></div>
  <button id="copyBtn" aria-label="copy path">⧉</button>
  <button id="fontDown" aria-label="smaller">A−</button>
  <button id="fontUp" aria-label="larger">A+</button>
</div>
<div id="main">
  <div id="sidebar"><div class="tree" id="tree"></div></div>
  <div id="overlay"></div>
  <div id="content">
    <div id="placeholder">
      tap ☰ to browse files<br>
      tap ⌕ to search across files<br>
      pinch to change font size
    </div>
    <div id="viewTabs">
      <button id="tabPreview">preview</button>
      <button id="tabSource">source</button>
    </div>
    <div id="tailNote" style="display:none"></div>
    <pre id="viewerWrap" style="display:none"><code id="viewer"></code></pre>
    <div id="mdWrap" style="display:none"></div>
    <div id="imageWrap" style="display:none"><img id="imageView" alt=""></div>
    <div id="mediaWrap" style="display:none">
      <video id="videoView" controls playsinline preload="metadata" style="display:none"></video>
      <audio id="audioView" controls preload="metadata" style="display:none"></audio>
      <div id="mediaMeta"></div>
    </div>
    <div id="modelWrap" style="display:none">
      <div id="modelStage">
        <canvas id="modelCanvas"></canvas>
        <div id="modelStatus" style="display:none"></div>
      </div>
      <div id="modelMeta"></div>
      <div id="modelParts" style="display:none"></div>
    </div>
  </div>
  <div id="grep">
    <div class="row">
      <input id="grepInput" type="text" placeholder="grep pattern (regex)"
             autocapitalize="off" autocorrect="off" autocomplete="off"
             spellcheck="false">
      <button id="grepGo">go</button>
      <button id="grepClose">×</button>
    </div>
    <div class="opts">
      <label><input type="checkbox" id="grepIgnoreCase" checked> ignore case</label>
    </div>
    <div id="grepResults"></div>
  </div>
</div>
<div id="toast"></div>
<div id="commentModal" class="modal">
  <div class="modal-body">
    <div id="commentLabel" class="label">Insert after line</div>
    <textarea id="commentText" autocapitalize="off" autocorrect="off"
              autocomplete="off" spellcheck="false"></textarea>
    <div class="modal-actions">
      <button id="commentCancel" class="cancel">cancel</button>
      <button id="commentSubmit" class="submit">submit</button>
    </div>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.9/dist/purify.min.js"></script>
<script>
const ROOT_NAME = __FB_TITLE_JS__;
const $ = id => document.getElementById(id);

async function api(path, params) {
  const u = new URL(path, location.origin);
  if (params) for (const k in params) {
    if (params[k] !== undefined && params[k] !== null) u.searchParams.set(k, params[k]);
  }
  const r = await fetch(u, { credentials: 'same-origin' });
  if (!r.ok) throw new Error('http ' + r.status);
  return r.json();
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1600);
}

// --- file tree (lazy) ---
async function loadTree(path, container) {
  let entries;
  try { entries = await api('/api/tree', { path }); }
  catch (e) { container.innerHTML = '<div class="gr-msg">error</div>'; return; }
  container.innerHTML = '';
  for (const e of entries) {
    const row = document.createElement('div');
    row.className = 'node';
    const full = path ? path + '/' + e.name : e.name;
    row.dataset.path = full;
    row.dataset.type = e.type;
    const ico = e.type === 'dir' ? '▸' : '·';
    row.innerHTML = `<span class="icon">${ico}</span><span class="name">${escapeHtml(e.name)}</span>`;
    container.appendChild(row);
    if (e.type === 'dir') {
      const kids = document.createElement('div');
      kids.className = 'children';
      kids.style.display = 'none';
      container.appendChild(kids);
      row.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        if (kids.style.display === 'none') {
          if (!kids.dataset.loaded) {
            await loadTree(full, kids);
            kids.dataset.loaded = '1';
          }
          kids.style.display = '';
          row.querySelector('.icon').textContent = '▾';
        } else {
          kids.style.display = 'none';
          row.querySelector('.icon').textContent = '▸';
        }
      });
    } else {
      row.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        document.querySelectorAll('#tree .node.sel').forEach(n => n.classList.remove('sel'));
        row.classList.add('sel');
        await openFile(full);
        closeSidebar();
      });
    }
  }
}

// --- file viewer ---
let currentPath = null;
let currentFull = false;
const EXT_LANG = {
  js:'javascript', mjs:'javascript', cjs:'javascript',
  ts:'typescript', tsx:'typescript', jsx:'javascript',
  py:'python', rb:'ruby', go:'go', rs:'rust',
  java:'java', kt:'kotlin', swift:'swift',
  c:'c', h:'c', cpp:'cpp', hpp:'cpp', cc:'cpp', cxx:'cpp',
  cs:'csharp', php:'php', pl:'perl', lua:'lua',
  html:'xml', htm:'xml', xml:'xml', svg:'xml',
  css:'css', scss:'scss', sass:'scss', less:'less',
  json:'json', yml:'yaml', yaml:'yaml', toml:'ini', ini:'ini',
  md:'markdown', markdown:'markdown',
  sh:'bash', bash:'bash', zsh:'bash', fish:'bash',
  sql:'sql', dockerfile:'dockerfile',
  vim:'vim', el:'lisp', clj:'clojure', ex:'elixir', erl:'erlang',
  hs:'haskell', ml:'ocaml', nim:'nim', zig:'zig',
  tf:'hcl', proto:'protobuf', graphql:'graphql', gql:'graphql',
  r:'r', jl:'julia', scala:'scala', dart:'dart',
  makefile:'makefile', cmake:'cmake',
};

function langFor(name) {
  const lower = name.toLowerCase();
  if (lower === 'makefile' || lower.endsWith('.mk')) return 'makefile';
  if (lower === 'dockerfile') return 'dockerfile';
  const dot = lower.lastIndexOf('.');
  if (dot < 0) return null;
  return EXT_LANG[lower.slice(dot + 1)] || null;
}

let watchCtrl = null;
let watchPollCtrl = null;
let watchWoken = false;
let currentMtime = null;

async function openFile(path, opts) {
  opts = opts || {};
  const { jumpLine, fromHistory, preserveScroll, full } = opts;
  const isReload = !!preserveScroll && currentPath === path;
  currentPath = path;
  currentFull = !!full;
  $('title').firstElementChild.textContent = path;
  document.title = ROOT_NAME + '/' + path;
  if (!fromHistory) {
    history.pushState({view: 'file', path, jumpLine: jumpLine || null}, '',
      '#' + encodeURIComponent(path));
  }
  cancelWatch();
  try {
    const res = await api('/api/file', { path, full: full ? '1' : undefined });
    const wrap = $('viewerWrap');
    const viewer = $('viewer');
    const imageWrap = $('imageWrap');
    const imageView = $('imageView');
    $('placeholder').style.display = 'none';
    $('viewTabs').classList.remove('show');
    $('mdWrap').style.display = 'none';
    $('mediaWrap').style.display = 'none';
    if (!res.model) {
      $('modelWrap').style.display = 'none';
      stopModel();
    }
    stopMedia();
    mdSource = null;
    const savedScroll = preserveScroll ? $('content').scrollTop : null;
    const tailNote = $('tailNote');
    if (res.image) {
      wrap.style.display = 'none';
      tailNote.style.display = 'none';
      imageView.classList.remove('actual');
      if (res.tooLarge) {
        imageWrap.style.display = 'none';
        imageView.removeAttribute('src');
        toast('image too large (' + res.size + ' bytes)');
      } else {
        imageWrap.style.display = '';
        imageView.src = '/api/raw?path=' + encodeURIComponent(path) + '&v=' + (res.mtime || '');
      }
    } else if (res.model) {
      wrap.style.display = 'none';
      imageWrap.style.display = 'none';
      imageView.removeAttribute('src');
      tailNote.style.display = 'none';
      showModel(path, res, { preserveView: isReload });
    } else if (res.media || res.binary) {
      wrap.style.display = 'none';
      imageWrap.style.display = 'none';
      imageView.removeAttribute('src');
      tailNote.style.display = 'none';
      showMedia(path, res);
    } else {
      imageWrap.style.display = 'none';
      imageView.removeAttribute('src');
      wrap.style.display = '';
      viewer.textContent = res.content + (res.truncated ? '\n\n[truncated at ' + res.size + ' bytes]' : '');
      const lang = langFor(path.split('/').pop());
      viewer.className = lang ? 'language-' + lang : '';
      delete viewer.dataset.highlighted;
      if (window.hljs && res.content.length <= 500000) {
        try { hljs.highlightElement(viewer); } catch (e) {}
      }
      wrapLines(viewer, res.startLine || 1);
      if (res.tail) {
        const last = (res.startLine || 1) + res.tailLines - 1;
        const msg = res.partial
          ? 'tail · last ' + res.tailLines + ' lines (file > 64 MiB, line numbers relative)'
          : 'tail · lines ' + res.startLine + '–' + last + ' of ' + res.totalLines;
        setTailNote(msg, 'full', () => openFile(path, { fromHistory: true, full: true }));
      } else if (res.fullView) {
        const kb = Math.round(res.size / 1024);
        setTailNote('full · ' + kb + ' KiB', 'tail', () => openFile(path, { fromHistory: true }));
      } else {
        tailNote.style.display = 'none';
      }
      if (lang === 'markdown' && !res.tail && !res.truncated && window.marked) {
        mdSource = res.content;
        mdRenderedFor = null;
        $('viewTabs').classList.add('show');
        applyMdTab(jumpLine ? 'source' : mdTab);
      }
    }
    if (jumpLine) scrollToLine(jumpLine);
    else if (savedScroll !== null) $('content').scrollTop = savedScroll;
    else if (res.tail && !res.binary) {
      const lastEl = viewer.querySelector('.line:last-child');
      if (lastEl) lastEl.scrollIntoView({ block: 'end' });
      else $('content').scrollTop = 0;
    } else $('content').scrollTop = 0;
    currentMtime = res.mtime || null;
    if (res.mtime) startWatch(path, res.mtime);
    return true;
  } catch (e) {
    toast('failed to load ' + path);
    if (currentPath === path && currentMtime && !watchCtrl) {
      startWatch(path, currentMtime);
    }
    return false;
  }
}

function fmtSize(n) {
  return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MiB'
    : Math.max(1, Math.round(n / 1024)) + ' KiB';
}

function showMedia(path, res) {
  const vv = $('videoView'), av = $('audioView');
  $('mediaWrap').style.display = '';
  vv.style.display = 'none';
  av.style.display = 'none';
  const src = '/api/raw?path=' + encodeURIComponent(path) + '&v=' + (res.mtime || '');
  if (res.media === 'video') { vv.src = src; vv.style.display = ''; }
  else if (res.media === 'audio') { av.src = src; av.style.display = ''; }
  $('mediaMeta').innerHTML =
    '<span>' + (res.media || 'binary file') + ' · ' + fmtSize(res.size) + '</span>' +
    '<a class="dlBtn" href="/api/download?path=' + encodeURIComponent(path) + '" download>download</a>';
}

function stopMedia() {
  for (const id of ['videoView', 'audioView']) {
    const el = $(id);
    try { el.pause(); } catch (e) {}
    if (el.hasAttribute('src')) {
      el.removeAttribute('src');
      try { el.load(); } catch (e) {}
    }
  }
}

// --- 3D model preview (STL / STEP) ---
let threePromise = null;
let occtPromise = null;
let modelLoadCtrl = null;
let modelCtx = null;

function loadThree() {
  if (!threePromise) {
    threePromise = import('https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js')
      .catch(e => { threePromise = null; throw e; });
  }
  return threePromise;
}

function loadOcct() {
  if (!occtPromise) {
    const base = 'https://cdn.jsdelivr.net/npm/occt-import-js@0.0.23/dist/';
    occtPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = base + 'occt-import-js.js';
      s.onload = () => {
        occtimportjs({ locateFile: f => base + f }).then(resolve, err => {
          occtPromise = null; reject(err);
        });
      };
      s.onerror = () => { occtPromise = null; reject(new Error('load failed')); };
      document.head.appendChild(s);
    });
  }
  return occtPromise;
}

function parseSTL(buffer, THREE) {
  const view = new DataView(buffer);
  let isBinary = false;
  if (buffer.byteLength >= 84) {
    const nTri = view.getUint32(80, true);
    if (84 + nTri * 50 === buffer.byteLength) {
      isBinary = true;
    } else {
      const head = new Uint8Array(buffer, 0, Math.min(512, buffer.byteLength));
      for (let i = 0; i < head.length; i++) {
        if (head[i] === 0 && 84 + nTri * 50 <= buffer.byteLength) {
          isBinary = true; break;
        }
      }
    }
  }
  if (isBinary) {
    const nTri = view.getUint32(80, true);
    const positions = new Float32Array(nTri * 9);
    const normals = new Float32Array(nTri * 9);
    let off = 84;
    for (let i = 0; i < nTri; i++) {
      let nx = view.getFloat32(off, true);
      let ny = view.getFloat32(off + 4, true);
      let nz = view.getFloat32(off + 8, true);
      const ax = view.getFloat32(off + 12, true), ay = view.getFloat32(off + 16, true), az = view.getFloat32(off + 20, true);
      const bx = view.getFloat32(off + 24, true), by = view.getFloat32(off + 28, true), bz = view.getFloat32(off + 32, true);
      const cx = view.getFloat32(off + 36, true), cy = view.getFloat32(off + 40, true), cz = view.getFloat32(off + 44, true);
      off += 50;
      if (nx === 0 && ny === 0 && nz === 0) {
        const ux = bx - ax, uy = by - ay, uz = bz - az;
        const vx = cx - ax, vy = cy - ay, vz = cz - az;
        nx = uy * vz - uz * vy;
        ny = uz * vx - ux * vz;
        nz = ux * vy - uy * vx;
        const len = Math.hypot(nx, ny, nz) || 1;
        nx /= len; ny /= len; nz /= len;
      }
      const j = i * 9;
      positions[j] = ax; positions[j + 1] = ay; positions[j + 2] = az;
      positions[j + 3] = bx; positions[j + 4] = by; positions[j + 5] = bz;
      positions[j + 6] = cx; positions[j + 7] = cy; positions[j + 8] = cz;
      normals[j] = nx; normals[j + 1] = ny; normals[j + 2] = nz;
      normals[j + 3] = nx; normals[j + 4] = ny; normals[j + 5] = nz;
      normals[j + 6] = nx; normals[j + 7] = ny; normals[j + 8] = nz;
    }
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geom.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
    return {
      object: new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
        color: 0x6cb6ff, roughness: 0.45, metalness: 0.15, side: THREE.DoubleSide,
      })),
      triangles: nTri,
    };
  }
  const text = new TextDecoder('utf-8').decode(buffer);
  const pos = [];
  const re = /vertex\s+([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s+([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s+([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)/gi;
  let m;
  while ((m = re.exec(text)) !== null) {
    pos.push(parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3]));
  }
  if (!pos.length) throw new Error('empty STL');
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geom.computeVertexNormals();
  return {
    object: new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
      color: 0x6cb6ff, roughness: 0.45, metalness: 0.15, side: THREE.DoubleSide,
    })),
    triangles: Math.floor(pos.length / 9),
  };
}

function normRgb(c) {
  const s = (c[0] > 1 || c[1] > 1 || c[2] > 1) ? 1 / 255 : 1;
  return [c[0] * s, c[1] * s, c[2] * s];
}

const PASTEL_PALETTE = [
  0x8ecae6, 0xffb4a2, 0xa8e6cf, 0xfde284,
  0xcdb4db, 0xffcad4, 0x90dbf4, 0xf1c0e8,
  0xb9fbc0, 0xffd6a5, 0xa3c4f3, 0xfbf8cc,
];

function makeMat(THREE, color) {
  return new THREE.MeshStandardMaterial({
    color, roughness: 0.45, metalness: 0.15, side: THREE.DoubleSide,
  });
}

function rgbToHex(rgb) {
  return '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v * 255))).toString(16).padStart(2, '0')).join('');
}

async function parseSTEP(buffer, THREE) {
  const occt = await loadOcct();
  const res = occt.ReadStepFile(new Uint8Array(buffer), null);
  if (!res || !res.success || !res.meshes || !res.meshes.length) {
    throw new Error('STEP parse failed');
  }
  const nodeNames = new Map();
  (function walk(node, parentName) {
    if (!node) return;
    const cur = (node.name && node.name.trim()) || parentName || '';
    if (Array.isArray(node.meshes)) {
      for (const mi of node.meshes) {
        if (!nodeNames.has(mi) && cur) nodeNames.set(mi, cur);
      }
    }
    if (Array.isArray(node.children)) {
      for (const ch of node.children) walk(ch, cur);
    }
  })(res.root, '');

  const group = new THREE.Group();
  const parts = [];
  let triangles = 0;
  let meshIdx = 0;
  for (let rawIdx = 0; rawIdx < res.meshes.length; rawIdx++) {
    const m = res.meshes[rawIdx];
    const posArr = m.attributes && m.attributes.position && (m.attributes.position.array || m.attributes.position);
    if (!posArr || !posArr.length) continue;
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.Float32BufferAttribute(posArr, 3));
    const normArr = m.attributes.normal && (m.attributes.normal.array || m.attributes.normal);
    if (normArr && normArr.length) {
      geom.setAttribute('normal', new THREE.Float32BufferAttribute(normArr, 3));
    }
    const idxArr = m.index && (m.index.array || m.index);
    let mTris = 0;
    if (idxArr && idxArr.length) {
      geom.setIndex(idxArr);
      mTris = Math.floor(idxArr.length / 3);
    } else {
      mTris = Math.floor(posArr.length / 9);
    }
    triangles += mTris;
    if (!geom.attributes.normal) geom.computeVertexNormals();
    const baseCol = m.color ? normRgb(m.color) : [0.42, 0.71, 1.0];
    let swatchRgb = baseCol;
    let origMat;
    if (m.brep_faces && m.brep_faces.some(f => f.color)) {
      const firstColored = m.brep_faces.find(f => f.color);
      if (firstColored && !m.color) swatchRgb = normRgb(firstColored.color);
      const mats = [];
      const matMap = new Map();
      const getMat = (rgb) => {
        const key = rgb.map(v => Math.round(v * 255)).join(',');
        if (!matMap.has(key)) {
          matMap.set(key, mats.length);
          mats.push(makeMat(THREE, new THREE.Color(rgb[0], rgb[1], rgb[2])));
        }
        return matMap.get(key);
      };
      for (const f of m.brep_faces) {
        const mi = getMat(f.color ? normRgb(f.color) : baseCol);
        geom.addGroup(f.first * 3, (f.last - f.first + 1) * 3, mi);
      }
      origMat = mats;
    } else {
      origMat = makeMat(THREE, new THREE.Color(baseCol[0], baseCol[1], baseCol[2]));
    }
    const pastelNum = PASTEL_PALETTE[meshIdx % PASTEL_PALETTE.length];
    const pastelMat = makeMat(THREE, pastelNum);
    const rawName = (m.name && m.name.trim()) || nodeNames.get(rawIdx) || '';
    const name = '#' + (meshIdx + 1) + (rawName ? ' ' + rawName : ' part');
    meshIdx++;
    const mesh = new THREE.Mesh(geom, origMat);
    mesh.userData = { origMat, pastelMat };
    group.add(mesh);
    parts.push({
      mesh,
      name,
      triangles: mTris,
      origHex: rgbToHex(swatchRgb),
      pastelHex: '#' + pastelNum.toString(16).padStart(6, '0'),
    });
  }
  if (!group.children.length) throw new Error('empty STEP model');
  return { object: group, triangles, parts };
}

function ensureModelCtx(THREE) {
  if (modelCtx) return modelCtx;
  const stage = $('modelStage');
  const canvas = $('modelCanvas');
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x161b22, 1);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
  const hemi = new THREE.HemisphereLight(0xffffff, 0x30363d, 0.85);
  scene.add(hemi);
  const keyLight = new THREE.DirectionalLight(0xffffff, 0.9);
  keyLight.position.set(1, 2, 1.5);
  camera.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0x9ecbff, 0.35);
  fillLight.position.set(-1.5, -1, 0.5);
  camera.add(fillLight);
  scene.add(camera);

  const ctx = {
    THREE, stage, canvas, renderer, scene, camera,
    root: null, grid: null, parts: null, modelPath: null, wireframe: false,
    pastel: localStorage.getItem('modelPastel') === '1',
    target: new THREE.Vector3(),
    boundRadius: 1,
    radius: 3, phi: Math.PI / 3, theta: Math.PI / 4,
    rafId: 0,
    updateCamera() {
      const offset = new THREE.Vector3().setFromSphericalCoords(ctx.radius, ctx.phi, ctx.theta);
      camera.position.copy(ctx.target).add(offset);
      camera.lookAt(ctx.target);
      camera.near = Math.max(ctx.boundRadius * 0.001, ctx.radius * 0.005);
      camera.far = Math.max(ctx.boundRadius * 20, ctx.radius * 10);
      camera.updateProjectionMatrix();
      ctx.requestRender();
    },
    resetView() {
      const visBox = new THREE.Box3();
      if (ctx.root) {
        ctx.root.updateMatrixWorld(true);
        ctx.root.traverse(n => {
          if (n.isMesh && n.visible) visBox.expandByObject(n);
        });
      }
      if (!visBox.isEmpty()) {
        const sz = new THREE.Vector3();
        visBox.getCenter(ctx.target);
        visBox.getSize(sz);
        ctx.fitRadius = Math.max(sz.length() * 0.5, 1e-3);
      } else {
        ctx.target.set(0, 0, 0);
        ctx.fitRadius = ctx.boundRadius;
      }
      ctx.phi = Math.PI / 3;
      ctx.theta = Math.PI / 4;
      const fovRad = (camera.fov * Math.PI) / 180;
      const aspect = Math.max(0.2, camera.aspect || 1);
      const r = ctx.fitRadius || ctx.boundRadius;
      const fitV = r / Math.sin(fovRad / 2);
      const fitH = r / (Math.sin(fovRad / 2) * Math.min(1, aspect));
      ctx.radius = Math.max(fitV, fitH) * 1.15;
      ctx.updateCamera();
    },
    panPixels(dx, dy) {
      const h = canvas.clientHeight || 1;
      const scale = (2 * ctx.radius * Math.tan((camera.fov * Math.PI) / 360)) / h;
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      const up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      ctx.target.addScaledVector(right, -dx * scale).addScaledVector(up, dy * scale);
      ctx.updateCamera();
    },
    zoomFactor(k) {
      const minR = Math.min(ctx.boundRadius, ctx.fitRadius || ctx.boundRadius) * 0.05;
      ctx.radius = Math.max(minR, Math.min(ctx.boundRadius * 50, ctx.radius * k));
      ctx.updateCamera();
    },
    resize() {
      const w = stage.clientWidth || 300;
      const h = stage.clientHeight || 300;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      ctx.requestRender();
    },
    renderNow() {
      if (ctx.rafId) {
        cancelAnimationFrame(ctx.rafId);
        ctx.rafId = 0;
      }
      if ($('modelWrap').style.display !== 'none') {
        renderer.render(scene, camera);
      }
    },
    requestRender() {
      if (ctx.rafId) return;
      ctx.rafId = requestAnimationFrame(() => {
        ctx.rafId = 0;
        if ($('modelWrap').style.display !== 'none') {
          renderer.render(scene, camera);
        }
      });
    },
    clearModel() {
      const disposeMat = (mat) => {
        if (!mat) return;
        if (Array.isArray(mat)) mat.forEach(m => m.dispose());
        else mat.dispose();
      };
      if (ctx.root) {
        scene.remove(ctx.root);
        ctx.root.traverse(n => {
          if (n.geometry) n.geometry.dispose();
          disposeMat(n.material);
          if (n.userData) {
            disposeMat(n.userData.origMat);
            disposeMat(n.userData.pastelMat);
          }
        });
        ctx.root = null;
      }
      if (ctx.grid) {
        scene.remove(ctx.grid);
        if (ctx.grid.geometry) ctx.grid.geometry.dispose();
        if (ctx.grid.material) ctx.grid.material.dispose();
        ctx.grid = null;
      }
      ctx.parts = null;
      ctx.modelPath = null;
    },
    setWireframe(on) {
      ctx.wireframe = on;
      const applyWf = (mat) => {
        if (!mat) return;
        if (Array.isArray(mat)) mat.forEach(m => { m.wireframe = on; });
        else mat.wireframe = on;
      };
      if (ctx.root) {
        ctx.root.traverse(n => {
          applyWf(n.material);
          if (n.userData) {
            applyWf(n.userData.origMat);
            applyWf(n.userData.pastelMat);
          }
        });
      }
      ctx.requestRender();
    },
    setPastel(on) {
      ctx.pastel = on;
      localStorage.setItem('modelPastel', on ? '1' : '0');
      if (ctx.root) {
        ctx.root.traverse(n => {
          if (n.userData && n.userData.pastelMat) {
            n.material = on ? n.userData.pastelMat : n.userData.origMat;
          }
        });
      }
      ctx.setWireframe(ctx.wireframe);
    },
  };

  // mouse controls
  let dragMode = null, lastX = 0, lastY = 0;
  canvas.addEventListener('contextmenu', e => e.preventDefault());
  canvas.addEventListener('mousedown', e => {
    dragMode = (e.button === 2 || e.button === 1 || e.shiftKey) ? 'pan' : 'orbit';
    lastX = e.clientX; lastY = e.clientY;
  });
  window.addEventListener('mousemove', e => {
    if (!dragMode) return;
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    if (dragMode === 'orbit') {
      ctx.theta -= dx * 0.008;
      ctx.phi = Math.max(0.02, Math.min(Math.PI - 0.02, ctx.phi - dy * 0.008));
      ctx.updateCamera();
    } else {
      ctx.panPixels(dx, dy);
    }
  });
  window.addEventListener('mouseup', () => { dragMode = null; });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    ctx.zoomFactor(e.deltaY > 0 ? 1.12 : 0.89);
  }, { passive: false });
  canvas.addEventListener('dblclick', () => ctx.resetView());

  // touch controls (1-finger orbit, 2-finger pinch zoom + pan)
  let tState = null;
  canvas.addEventListener('touchstart', e => {
    if (e.touches.length === 1) {
      tState = { mode: 'orbit', x: e.touches[0].clientX, y: e.touches[0].clientY };
    } else if (e.touches.length === 2) {
      const x0 = e.touches[0].clientX, y0 = e.touches[0].clientY;
      const x1 = e.touches[1].clientX, y1 = e.touches[1].clientY;
      tState = {
        mode: 'pinch',
        cx: (x0 + x1) / 2, cy: (y0 + y1) / 2,
        dist: Math.hypot(x0 - x1, y0 - y1) || 1,
      };
    }
  }, { passive: true });
  canvas.addEventListener('touchmove', e => {
    if (!tState) return;
    e.preventDefault();
    if (e.touches.length === 1 && tState.mode === 'orbit') {
      const dx = e.touches[0].clientX - tState.x;
      const dy = e.touches[0].clientY - tState.y;
      tState.x = e.touches[0].clientX;
      tState.y = e.touches[0].clientY;
      ctx.theta -= dx * 0.009;
      ctx.phi = Math.max(0.02, Math.min(Math.PI - 0.02, ctx.phi - dy * 0.009));
      ctx.updateCamera();
    } else if (e.touches.length === 2 && tState.mode === 'pinch') {
      const x0 = e.touches[0].clientX, y0 = e.touches[0].clientY;
      const x1 = e.touches[1].clientX, y1 = e.touches[1].clientY;
      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
      const dist = Math.hypot(x0 - x1, y0 - y1) || 1;
      ctx.panPixels(cx - tState.cx, cy - tState.cy);
      ctx.zoomFactor(tState.dist / dist);
      tState.cx = cx; tState.cy = cy; tState.dist = dist;
    }
  }, { passive: false });
  canvas.addEventListener('touchend', e => {
    if (e.touches.length === 1) {
      tState = { mode: 'orbit', x: e.touches[0].clientX, y: e.touches[0].clientY };
    } else if (e.touches.length === 0) {
      tState = null;
    }
  }, { passive: true });
  canvas.addEventListener('touchcancel', () => { tState = null; }, { passive: true });

  if (window.ResizeObserver) {
    new ResizeObserver(() => ctx.resize()).observe(stage);
  } else {
    window.addEventListener('resize', () => ctx.resize());
  }
  modelCtx = ctx;
  return ctx;
}

function fmtDim(v) {
  return v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
}

function renderPartsPanel(parts, ctx) {
  const box = $('modelParts');
  if (!parts || !parts.length) {
    box.style.display = 'none';
    box.innerHTML = '';
    return;
  }
  box.style.display = '';
  box.innerHTML =
    '<div class="mp-head">' +
      '<span id="mpCount"></span>' +
      '<button type="button" class="mp-btn" id="mpAll">all</button>' +
      '<button type="button" class="mp-btn" id="mpNone">none</button>' +
    '</div>' +
    '<div class="mp-list" id="mpList"></div>';
  const list = $('mpList');
  const rows = [];

  const syncUI = () => {
    let vis = 0;
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i], r = rows[i];
      const on = p.mesh.visible;
      if (on) vis++;
      r.cb.checked = on;
      r.row.classList.toggle('off', !on);
      r.sw.style.background = ctx.pastel ? p.pastelHex : p.origHex;
    }
    $('mpCount').textContent = 'parts (' + vis + ' / ' + parts.length + ')';
    ctx.requestRender();
  };

  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const row = document.createElement('div');
    row.className = 'mp-row';
    row.innerHTML =
      '<input type="checkbox" checked>' +
      '<span class="mp-swatch"></span>' +
      '<span class="mp-name">' + escapeHtml(p.name) + '</span>' +
      '<span class="mp-tris">' + p.triangles.toLocaleString() + '</span>' +
      '<button type="button" class="mp-btn">only</button>';
    const cb = row.querySelector('input');
    const sw = row.querySelector('.mp-swatch');
    const onlyBtn = row.querySelector('button');
    row.addEventListener('click', (e) => {
      if (e.target === onlyBtn) return;
      if (e.target !== cb) cb.checked = !cb.checked;
      p.mesh.visible = cb.checked;
      syncUI();
    });
    onlyBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const othersVisible = parts.some((o, idx) => idx !== i && o.mesh.visible);
      if (p.mesh.visible && !othersVisible) {
        parts.forEach(o => { o.mesh.visible = true; });
      } else {
        parts.forEach((o, idx) => { o.mesh.visible = (idx === i); });
      }
      syncUI();
    });
    list.appendChild(row);
    rows.push({ row, cb, sw });
  }

  $('mpAll').addEventListener('click', () => {
    parts.forEach(p => { p.mesh.visible = true; });
    syncUI();
  });
  $('mpNone').addEventListener('click', () => {
    parts.forEach(p => { p.mesh.visible = false; });
    syncUI();
  });

  ctx.onPastelChange = syncUI;
  syncUI();
}

async function showModel(path, res, opts) {
  opts = opts || {};
  const keepView = !!(opts.preserveView && modelCtx && modelCtx.modelPath === path && modelCtx.root);
  if (keepView) {
    if (modelLoadCtrl) {
      try { modelLoadCtrl.abort(); } catch (e) {}
      modelLoadCtrl = null;
    }
  } else {
    stopModel();
  }
  const ctrl = new AbortController();
  modelLoadCtrl = ctrl;
  $('modelWrap').style.display = '';
  const status = $('modelStatus');
  status.textContent = 'loading ' + res.model.toUpperCase() + '…';
  status.style.display = '';
  const renderMeta = (extra) => {
    const meta = $('modelMeta');
    meta.innerHTML =
      '<span class="metaText">' + escapeHtml(res.model + ' · ' + fmtSize(res.size) + (extra ? ' · ' + extra : '')) + '</span>' +
      (res.model === 'step' ? '<button type="button" class="ctrlBtn" id="modelPastelBtn">pastel</button>' : '') +
      '<button type="button" class="ctrlBtn" id="modelWireBtn">wire</button>' +
      '<button type="button" class="ctrlBtn" id="modelResetBtn">reset</button>' +
      '<a class="dlBtn" href="/api/download?path=' + encodeURIComponent(path) + '" download>download</a>';
    const pb = $('modelPastelBtn'), wb = $('modelWireBtn'), rb = $('modelResetBtn');
    if (pb) {
      const isPastel = modelCtx ? modelCtx.pastel : localStorage.getItem('modelPastel') === '1';
      pb.classList.toggle('active', isPastel);
      pb.addEventListener('click', () => {
        const next = !(modelCtx ? modelCtx.pastel : localStorage.getItem('modelPastel') === '1');
        localStorage.setItem('modelPastel', next ? '1' : '0');
        if (modelCtx) {
          modelCtx.setPastel(next);
          if (modelCtx.onPastelChange) modelCtx.onPastelChange();
        }
        pb.classList.toggle('active', next);
      });
    }
    if (wb && modelCtx) {
      wb.classList.toggle('active', modelCtx.wireframe);
      wb.addEventListener('click', () => {
        modelCtx.setWireframe(!modelCtx.wireframe);
        wb.classList.toggle('active', modelCtx.wireframe);
      });
    }
    if (rb && modelCtx) rb.addEventListener('click', () => modelCtx.resetView());
  };
  if (!keepView) renderMeta('');
  try {
    const src = '/api/raw?path=' + encodeURIComponent(path) + '&v=' + (res.mtime || '');
    const [THREE, buf] = await Promise.all([
      loadThree(),
      fetch(src, { signal: ctrl.signal, credentials: 'same-origin' }).then(r => {
        if (!r.ok) throw new Error('http ' + r.status);
        return r.arrayBuffer();
      }),
    ]);
    if (ctrl.signal.aborted) return;
    if (res.model === 'step') status.textContent = 'tessellating STEP…';
    const parsed = res.model === 'step'
      ? await parseSTEP(buf, THREE)
      : parseSTL(buf, THREE);
    if (ctrl.signal.aborted) return;

    const ctx = ensureModelCtx(THREE);
    let savedView = null;
    if (keepView && ctx.modelPath === path && ctx.root) {
      const visByName = new Map();
      const visByIdx = [];
      if (ctx.parts) {
        for (let i = 0; i < ctx.parts.length; i++) {
          const p = ctx.parts[i];
          visByName.set(p.name, p.mesh.visible);
          visByIdx.push(p.mesh.visible);
        }
      }
      savedView = {
        target: ctx.target.clone(),
        radius: ctx.radius,
        phi: ctx.phi,
        theta: ctx.theta,
        visByName,
        visByIdx,
      };
    }
    ctx.clearModel();
    const obj = parsed.object;
    const origBox = new THREE.Box3().setFromObject(obj);
    const origSize = new THREE.Vector3();
    origBox.getSize(origSize);

    // CAD files are typically Z-up; rotate to Three.js Y-up
    const holder = new THREE.Group();
    obj.rotation.x = -Math.PI / 2;
    holder.add(obj);
    holder.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(holder);
    const center = new THREE.Vector3();
    const size = new THREE.Vector3();
    box.getCenter(center);
    box.getSize(size);
    holder.position.sub(center);

    const maxDim = Math.max(size.x, size.y, size.z, 1e-3);
    ctx.boundRadius = Math.max(size.length() * 0.5, 1e-3);
    const gridSize = Math.pow(10, Math.ceil(Math.log10(maxDim * 1.5)));
    const grid = new THREE.GridHelper(gridSize, 10, 0x30363d, 0x21262d);
    grid.position.y = -size.y / 2;
    ctx.scene.add(grid);
    ctx.grid = grid;

    ctx.scene.add(holder);
    ctx.root = holder;
    ctx.parts = parsed.parts || null;
    ctx.modelPath = path;
    if (savedView && ctx.parts) {
      for (let i = 0; i < ctx.parts.length; i++) {
        const p = ctx.parts[i];
        if (savedView.visByName.has(p.name)) {
          p.mesh.visible = savedView.visByName.get(p.name);
        } else if (i < savedView.visByIdx.length) {
          p.mesh.visible = savedView.visByIdx[i];
        }
      }
    }
    ctx.pastel = localStorage.getItem('modelPastel') === '1';
    if (ctx.pastel) ctx.setPastel(true);
    else if (ctx.wireframe) ctx.setWireframe(true);
    renderPartsPanel(ctx.parts, ctx);
    ctx.resize();
    if (savedView) {
      ctx.target.copy(savedView.target);
      ctx.radius = savedView.radius;
      ctx.phi = savedView.phi;
      ctx.theta = savedView.theta;
      ctx.updateCamera();
    } else {
      ctx.resetView();
    }
    status.style.display = 'none';
    const dims = fmtDim(origSize.x) + '×' + fmtDim(origSize.y) + '×' + fmtDim(origSize.z);
    renderMeta(parsed.triangles.toLocaleString() + ' tris · ' + dims);
    ctx.renderNow();
  } catch (e) {
    if (ctrl.signal.aborted) return;
    if (!opts.retried) {
      await new Promise(r => setTimeout(r, 400));
      if (!ctrl.signal.aborted && currentPath === path) {
        return showModel(path, res, { preserveView: opts.preserveView, retried: true });
      }
      return;
    }
    status.textContent = 'failed to render 3D model';
  }
}

function stopModel() {
  if (modelLoadCtrl) {
    try { modelLoadCtrl.abort(); } catch (e) {}
    modelLoadCtrl = null;
  }
  const partsBox = $('modelParts');
  if (partsBox) { partsBox.style.display = 'none'; partsBox.innerHTML = ''; }
  if (modelCtx) {
    modelCtx.onPastelChange = null;
    modelCtx.clearModel();
  }
}

function setTailNote(msg, btnLabel, onClick) {
  const tailNote = $('tailNote');
  tailNote.textContent = msg;
  const b = document.createElement('button');
  b.textContent = btnLabel;
  b.addEventListener('click', onClick);
  tailNote.appendChild(b);
  tailNote.style.display = '';
}

function cancelWatch() {
  if (watchPollCtrl) { try { watchPollCtrl.abort(); } catch (e) {} watchPollCtrl = null; }
  if (watchCtrl) { try { watchCtrl.abort(); } catch (e) {} watchCtrl = null; }
}

function wakeWatch() {
  if (modelCtx && $('modelWrap').style.display !== 'none') {
    modelCtx.resize();
    modelCtx.renderNow();
  }
  if (!currentPath) return;
  if (!watchCtrl || watchCtrl.signal.aborted) {
    if (currentMtime) startWatch(currentPath, currentMtime);
    return;
  }
  if (watchPollCtrl) {
    watchWoken = true;
    try { watchPollCtrl.abort(); } catch (e) {}
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') wakeWatch();
});
window.addEventListener('focus', wakeWatch);
window.addEventListener('online', wakeWatch);
window.addEventListener('pageshow', wakeWatch);

async function startWatch(path, mtime) {
  cancelWatch();
  const ctrl = new AbortController();
  watchCtrl = ctrl;
  let curMtime = mtime;
  while (currentPath === path && !ctrl.signal.aborted) {
    const pollCtrl = new AbortController();
    watchPollCtrl = pollCtrl;
    const onAbort = () => { try { pollCtrl.abort(); } catch (e) {} };
    ctrl.signal.addEventListener('abort', onAbort, { once: true });
    const timer = setTimeout(onAbort, 35000);
    try {
      const u = new URL('/api/watch', location.origin);
      u.searchParams.set('path', path);
      u.searchParams.set('mtime', curMtime);
      const r = await fetch(u, { signal: pollCtrl.signal, credentials: 'same-origin' });
      clearTimeout(timer);
      ctrl.signal.removeEventListener('abort', onAbort);
      if (ctrl.signal.aborted || currentPath !== path) return;
      if (r.status === 204) continue;
      if (!r.ok) {
        await new Promise(res => setTimeout(res, 2000));
        continue;
      }
      const data = await r.json();
      if (data.deleted) {
        toast('file deleted');
        curMtime = 'deleted';
        await new Promise(res => setTimeout(res, 1000));
        continue;
      }
      if (data.changed) {
        toast('reloading…');
        const ok = await openFile(path, { fromHistory: true, preserveScroll: true, full: currentFull });
        if (ok) return; // openFile starts a new watch
        await new Promise(res => setTimeout(res, 2000));
      }
    } catch (e) {
      clearTimeout(timer);
      ctrl.signal.removeEventListener('abort', onAbort);
      if (ctrl.signal.aborted || currentPath !== path) return;
      if (watchWoken) {
        watchWoken = false;
        continue;
      }
      await new Promise(res => setTimeout(res, 2000));
    }
  }
}

function scrollToLine(line) {
  const el = $('viewer').querySelector('.line[data-line="' + line + '"]');
  if (el) {
    const top = el.offsetTop - 20;
    $('content').scrollTop = Math.max(0, top);
    return;
  }
  const cs = getComputedStyle($('viewerWrap'));
  const lh = parseFloat(cs.lineHeight) || (parseFloat(cs.fontSize) * 1.55);
  const padTop = parseFloat(cs.paddingTop) || 0;
  $('content').scrollTop = Math.max(0, padTop + (line - 3) * lh);
}

// --- wrap every source line in <span class="line"><span class="ln">N</span>...</span>
// Walks the DOM so hljs multi-line tokens (comments/strings) stay highlighted.
function wrapLines(codeEl, startLine) {
  const start = startLine || 1;
  // Collect segments per source line.
  const lines = [[]]; // lines[i] = array of { stack: [{tag,cls},...], text }
  function walk(node, stack) {
    if (node.nodeType === 3) {
      const parts = String(node.nodeValue).split('\n');
      for (let i = 0; i < parts.length; i++) {
        if (i > 0) lines.push([]);
        if (parts[i] !== '') lines[lines.length - 1].push({ stack: stack.slice(), text: parts[i] });
      }
    } else if (node.nodeType === 1) {
      const frame = { tag: node.tagName, cls: node.getAttribute('class') || '' };
      stack.push(frame);
      for (const c of Array.from(node.childNodes)) walk(c, stack);
      stack.pop();
    }
  }
  for (const c of Array.from(codeEl.childNodes)) walk(c, []);

  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const segs = lines[i];
    const n = i + start;
    let html = '<span class="line" data-line="' + n + '"><span class="ln" data-line="' + n + '">' + n + '</span>';
    for (const seg of segs) {
      for (const fr of seg.stack) {
        const t = fr.tag.toLowerCase();
        html += '<' + t + (fr.cls ? ' class="' + escapeAttr(fr.cls) + '"' : '') + '>';
      }
      html += escapeHtml(seg.text);
      for (let k = seg.stack.length - 1; k >= 0; k--) {
        html += '</' + seg.stack[k].tag.toLowerCase() + '>';
      }
    }
    html += '</span>';
    out.push(html);
  }
  codeEl.innerHTML = out.join('');
}

function escapeAttr(s) {
  return s.replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// --- markdown preview (tabbed) ---
let mdSource = null;       // raw markdown of the current file, or null
let mdRenderedFor = null;  // path the preview in #mdWrap was rendered for
let mdTab = localStorage.getItem('mdTab') === 'source' ? 'source' : 'preview';

function resolveRel(dir, rel) {
  const stack = dir ? dir.split('/') : [];
  for (const part of rel.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') stack.pop();
    else stack.push(part);
  }
  return stack.join('/');
}

function renderMdPreview() {
  if (mdRenderedFor === currentPath) return;
  const el = $('mdWrap');
  let out;
  try { out = marked.parse(mdSource); }
  catch (e) { out = '<p>markdown render error</p>'; }
  if (window.DOMPurify) out = DOMPurify.sanitize(out);
  el.innerHTML = out;
  const dir = currentPath.split('/').slice(0, -1).join('/');
  el.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (/^([a-z][a-z0-9+.-]*:|\/\/)/i.test(src)) return;
    img.src = '/api/raw?path=' + encodeURIComponent(resolveRel(dir, src));
  });
  el.querySelectorAll('a[href]').forEach(a => {
    const href = a.getAttribute('href') || '';
    if (/^([a-z][a-z0-9+.-]*:|\/\/|#)/i.test(href)) {
      a.target = '_blank'; a.rel = 'noopener';
      return;
    }
    a.addEventListener('click', (e) => {
      e.preventDefault();
      openFile(resolveRel(dir, href));
    });
  });
  const diagrams = [];
  el.querySelectorAll('pre > code.language-mermaid').forEach(c => {
    const div = document.createElement('div');
    div.className = 'mermaid';
    div.textContent = c.textContent;
    c.parentNode.replaceWith(div);
    diagrams.push(div);
  });
  if (window.hljs) {
    el.querySelectorAll('pre code').forEach(c => {
      try { hljs.highlightElement(c); } catch (e) {}
    });
  }
  mdRenderedFor = currentPath;
  if (diagrams.length) renderMermaid(diagrams);
}

// mermaid is large, so it is only fetched when a preview actually has a diagram.
let mermaidLoading = null;
function loadMermaid() {
  if (!mermaidLoading) {
    mermaidLoading = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js';
      s.onload = () => {
        mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' });
        resolve();
      };
      s.onerror = () => { mermaidLoading = null; reject(new Error('load failed')); };
      document.head.appendChild(s);
    });
  }
  return mermaidLoading;
}

function renderMermaid(nodes) {
  loadMermaid().then(() => {
    // the file may have changed while the script was loading
    nodes = nodes.filter(n => n.isConnected);
    if (nodes.length) return mermaid.run({ nodes, suppressErrors: true });
  }).catch(() => {});
}

function applyMdTab(tab) {
  $('tabPreview').classList.toggle('active', tab === 'preview');
  $('tabSource').classList.toggle('active', tab === 'source');
  if (tab === 'preview' && mdSource !== null) {
    $('viewerWrap').style.display = 'none';
    $('mdWrap').style.display = '';  // visible first: mermaid needs layout to measure
    renderMdPreview();
  } else {
    $('mdWrap').style.display = 'none';
    $('viewerWrap').style.display = '';
  }
}

$('tabPreview').addEventListener('click', () => {
  mdTab = 'preview'; localStorage.setItem('mdTab', mdTab); applyMdTab(mdTab);
});
$('tabSource').addEventListener('click', () => {
  mdTab = 'source'; localStorage.setItem('mdTab', mdTab); applyMdTab(mdTab);
});

// --- font size / pinch zoom ---
let fontSize = parseFloat(localStorage.getItem('fs') || '13');
if (!isFinite(fontSize) || fontSize < 6 || fontSize > 60) fontSize = 13;

function applyFontSize() {
  $('viewerWrap').style.fontSize = fontSize.toFixed(1) + 'px';
  $('mdWrap').style.fontSize = (fontSize + 3).toFixed(1) + 'px';
  localStorage.setItem('fs', fontSize.toFixed(1));
}
applyFontSize();

$('fontUp').addEventListener('click', () => { fontSize = Math.min(60, fontSize + 1); applyFontSize(); });
$('fontDown').addEventListener('click', () => { fontSize = Math.max(6, fontSize - 1); applyFontSize(); });

const content = $('content');
let pinchStart = null;
content.addEventListener('touchstart', (e) => {
  if (e.touches.length === 2 && !(e.target.closest && e.target.closest('#modelStage'))) {
    const dx = e.touches[0].clientX - e.touches[1].clientX;
    const dy = e.touches[0].clientY - e.touches[1].clientY;
    pinchStart = { dist: Math.hypot(dx, dy) || 1, startFs: fontSize };
  }
}, { passive: true });
content.addEventListener('touchmove', (e) => {
  if (e.touches.length === 2 && pinchStart) {
    const dx = e.touches[0].clientX - e.touches[1].clientX;
    const dy = e.touches[0].clientY - e.touches[1].clientY;
    const d = Math.hypot(dx, dy);
    const next = pinchStart.startFs * (d / pinchStart.dist);
    fontSize = Math.max(6, Math.min(60, next));
    applyFontSize();
    e.preventDefault();
  }
}, { passive: false });
content.addEventListener('touchend', (e) => { if (e.touches.length < 2) pinchStart = null; }, { passive: true });
content.addEventListener('touchcancel', () => { pinchStart = null; }, { passive: true });

// --- sidebar ---
function openSidebar() { $('sidebar').classList.add('open'); $('overlay').classList.add('show'); }
function closeSidebar() { $('sidebar').classList.remove('open'); $('overlay').classList.remove('show'); }
$('menuBtn').addEventListener('click', () => {
  if ($('sidebar').classList.contains('open')) closeSidebar(); else openSidebar();
});
$('overlay').addEventListener('click', () => { closeSidebar(); $('grep').classList.remove('show'); });

// --- grep ---
$('grepBtn').addEventListener('click', () => {
  const el = $('grep');
  el.classList.toggle('show');
  if (el.classList.contains('show')) setTimeout(() => $('grepInput').focus(), 0);
});
$('grepClose').addEventListener('click', () => $('grep').classList.remove('show'));

async function runGrep() {
  const q = $('grepInput').value;
  if (!q.trim()) return;
  const ci = $('grepIgnoreCase').checked ? '1' : '0';
  const box = $('grepResults');
  box.innerHTML = '<div class="gr-msg">searching…</div>';
  try {
    const res = await api('/api/grep', { q, i: ci });
    box.innerHTML = '';
    if (!res.results.length) {
      box.innerHTML = '<div class="gr-msg">no matches</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    for (const f of res.results) {
      const fe = document.createElement('div');
      fe.className = 'gr-file';
      fe.textContent = f.path + '  (' + f.matches.length + ')';
      fe.addEventListener('click', () => {
        openFile(f.path);
        $('grep').classList.remove('show');
      });
      frag.appendChild(fe);
      for (const m of f.matches) {
        const he = document.createElement('div');
        he.className = 'gr-hit';
        he.innerHTML = `<span class="ln">${m.line}</span>${escapeHtml(m.text)}`;
        he.addEventListener('click', () => {
          openFile(f.path, { jumpLine: m.line });
          $('grep').classList.remove('show');
        });
        frag.appendChild(he);
      }
    }
    if (res.truncated) {
      const t = document.createElement('div');
      t.className = 'gr-msg';
      t.textContent = 'truncated at ' + res.total + ' matches';
      frag.appendChild(t);
    }
    box.appendChild(frag);
  } catch (e) {
    box.innerHTML = '<div class="gr-msg" style="color:salmon">error</div>';
  }
}
$('grepGo').addEventListener('click', runGrep);
$('grepInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') runGrep(); });

async function copyPath() {
  if (!currentPath) { toast('no file selected'); return; }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(currentPath);
    } else {
      // fallback for non-secure contexts (plain http over LAN/tailscale)
      const ta = document.createElement('textarea');
      ta.value = currentPath;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '0';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (!ok) throw new Error('execCommand copy failed');
    }
    toast('copied ' + currentPath);
  } catch (e) {
    toast('copy failed: ' + currentPath);
  }
}
$('copyBtn').addEventListener('click', copyPath);

// --- long-press line number → insert comment ---
let pendingLine = null;
let lpTimer = null;
let lpStart = null;

function openCommentDialog(line) {
  if (!currentPath || line == null) return;
  pendingLine = line;
  $('commentLabel').textContent = 'Insert after line ' + line + ' of ' + currentPath;
  const ta = $('commentText');
  ta.value = 'NOTE: ';
  $('commentModal').classList.add('show');
  setTimeout(() => {
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }, 50);
}
function closeCommentDialog() {
  $('commentModal').classList.remove('show');
  pendingLine = null;
}
async function submitComment() {
  const text = $('commentText').value;
  if (!text.trim() || !currentPath || pendingLine == null) { closeCommentDialog(); return; }
  try {
    const u = new URL('/api/insert', location.origin);
    u.searchParams.set('path', currentPath);
    u.searchParams.set('line', String(pendingLine));
    const r = await fetch(u, {
      method: 'POST',
      body: text,
      credentials: 'same-origin',
      headers: {'Content-Type': 'text/plain; charset=utf-8'},
    });
    if (!r.ok) throw new Error('http ' + r.status);
    closeCommentDialog();
    // watch long-poll will pick up the mtime change and reload
  } catch (e) {
    toast('insert failed');
  }
}
$('commentSubmit').addEventListener('click', submitComment);
$('commentCancel').addEventListener('click', closeCommentDialog);
$('commentModal').addEventListener('click', (e) => {
  if (e.target.id === 'commentModal') closeCommentDialog();
});

// desktop right-click on line number
$('viewer').addEventListener('contextmenu', (e) => {
  const ln = e.target.closest && e.target.closest('.ln');
  if (!ln) return;
  e.preventDefault();
  openCommentDialog(parseInt(ln.dataset.line, 10));
});

// mobile long-press
$('viewer').addEventListener('touchstart', (e) => {
  if (e.touches.length !== 1) {
    if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
    return;
  }
  const ln = e.target.closest && e.target.closest('.ln');
  if (!ln) return;
  lpStart = { x: e.touches[0].clientX, y: e.touches[0].clientY };
  const n = parseInt(ln.dataset.line, 10);
  lpTimer = setTimeout(() => {
    lpTimer = null;
    openCommentDialog(n);
    if (navigator.vibrate) navigator.vibrate(20);
  }, 500);
}, { passive: true });
$('viewer').addEventListener('touchmove', (e) => {
  if (!lpTimer || !lpStart || e.touches.length !== 1) return;
  const dx = e.touches[0].clientX - lpStart.x;
  const dy = e.touches[0].clientY - lpStart.y;
  if (Math.hypot(dx, dy) > 10) { clearTimeout(lpTimer); lpTimer = null; }
}, { passive: true });
$('viewer').addEventListener('touchend', () => {
  if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
}, { passive: true });
$('viewer').addEventListener('touchcancel', () => {
  if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
}, { passive: true });

// tap image to toggle fit-width / actual size
$('imageView').addEventListener('click', () => {
  $('imageView').classList.toggle('actual');
});

function showTree() {
  cancelWatch();
  $('viewerWrap').style.display = 'none';
  $('mdWrap').style.display = 'none';
  $('viewTabs').classList.remove('show');
  mdSource = null;
  $('mediaWrap').style.display = 'none';
  stopMedia();
  $('modelWrap').style.display = 'none';
  stopModel();
  $('imageWrap').style.display = 'none';
  $('imageView').removeAttribute('src');
  $('tailNote').style.display = 'none';
  $('placeholder').style.display = '';
  $('title').firstElementChild.textContent = 'select a file';
  document.title = ROOT_NAME;
  currentPath = null;
  $('grep').classList.remove('show');
  openSidebar();
}

window.addEventListener('popstate', (e) => {
  const s = e.state;
  if (!s || s.view === 'tree') {
    showTree();
  } else if (s.view === 'file' && s.path) {
    openFile(s.path, { jumpLine: s.jumpLine, fromHistory: true });
  }
});

// init: restore file from #hash if present (survives reload / deep link)
(function init() {
  let initPath = null;
  if (location.hash && location.hash.length > 1) {
    try { initPath = decodeURIComponent(location.hash.slice(1)); }
    catch (e) { initPath = null; }
  }
  loadTree('', $('tree'));
  if (initPath) {
    history.replaceState({view: 'file', path: initPath}, '', location.href);
    openFile(initPath, { fromHistory: true });
  } else {
    history.replaceState({view: 'tree'}, '', location.pathname + location.search);
    openSidebar();
  }
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "fb/0.1"
    # HTTP/1.1 is required for <video>: Safari/iOS AVFoundation refuses to
    # play media from HTTP/1.0 servers. Every response sets Content-Length,
    # so keep-alive is safe.
    protocol_version = "HTTP/1.1"

    def _authed(self, qs: dict) -> bool:
        if not TOKEN:
            return True
        t = qs.get("t", [None])[0]
        if t and secrets.compare_digest(t, TOKEN):
            return True
        for raw in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = raw.strip().partition("=")
            if k == "fb_token" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def _send(self, status: int, body: bytes, ctype: str, extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra:
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _stream_file(self, p: Path, ctype: str, download: bool = False) -> None:
        """Send a file, streaming in chunks, honoring a single bytes= Range."""
        try:
            size = p.stat().st_size
            fh = p.open("rb")
        except OSError:
            self._send(404, b"not found", "text/plain")
            return
        try:
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''" + quote(p.name))
            self.end_headers()
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    # file shrank mid-stream; body is short of Content-Length,
                    # so the connection must not be reused
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except OSError:
            # client gone or read failed mid-stream; headers may already be
            # sent, so no error response — just make sure the connection
            # (short of Content-Length) is never reused
            self.close_connection = True
        finally:
            fh.close()

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._authed(qs):
            self._send(401,
                       b"need token. use the URL printed when the server started.",
                       "text/plain; charset=utf-8")
            return

        path = u.path
        if path == "/" or path == "/index.html":
            extra = ([("Set-Cookie", f"fb_token={TOKEN}; Path=/; SameSite=Strict")]
                     if TOKEN else None)
            root_name = ROOT.name or "/"
            page = (INDEX_HTML
                    .replace("__FB_TITLE__", html.escape(root_name))
                    .replace("__FB_TITLE_JS__", json.dumps(root_name)))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", extra)
            return

        if path == "/api/tree":
            rel = qs.get("path", [""])[0]
            try:
                p = safe_path(rel)
                if not p.is_dir():
                    self._send(400, b"not a directory", "text/plain"); return
                self._json(list_dir(p))
            except Exception:
                self._send(400, b"bad path", "text/plain")
            return

        if path == "/api/file":
            rel = qs.get("path", [""])[0]
            full = qs.get("full", ["0"])[0] == "1"
            try:
                p = safe_path(rel)
                if not p.is_file():
                    self._send(404, b"not found", "text/plain"); return
                st = p.stat()
                size = st.st_size
                mtime = f"{st.st_mtime_ns}-{size}"
                ext = p.suffix.lower().lstrip(".")
                if ext in IMAGE_MIME:
                    self._json({"size": size, "mtime": mtime, "image": True,
                                "tooLarge": size > MAX_IMAGE_BYTES})
                    return
                if ext in VIDEO_MIME or ext in AUDIO_MIME:
                    kind = "video" if ext in VIDEO_MIME else "audio"
                    self._json({"size": size, "mtime": mtime, "media": kind})
                    return
                if ext in MODEL_MIME:
                    kind = "stl" if ext == "stl" else "step"
                    self._json({"size": size, "mtime": mtime, "model": kind})
                    return
                if size > TAIL_THRESHOLD and not full:
                    partial = size > TAIL_MAX_FILE_BYTES
                    with p.open("rb") as fh:
                        if partial:
                            fh.seek(size - TAIL_MAX_FILE_BYTES)
                            data = fh.read()
                            nl = data.find(b"\n")
                            if nl >= 0:
                                data = data[nl + 1:]
                        else:
                            data = fh.read()
                    if not is_probably_text(data):
                        self._json({"size": size, "mtime": mtime, "binary": True}); return
                    lines = data.splitlines()
                    tail = lines[-TAIL_LINES:]
                    total_lines = None if partial else len(lines)
                    start_line = (total_lines - len(tail) + 1) if total_lines is not None else None
                    content = b"\n".join(tail).decode("utf-8", errors="replace")
                    self._json({
                        "size": size, "mtime": mtime, "binary": False,
                        "tail": True, "partial": partial,
                        "tailLines": len(tail), "totalLines": total_lines,
                        "startLine": start_line, "content": content,
                    })
                    return
                with p.open("rb") as fh:
                    data = fh.read(MAX_FILE_BYTES + 1)
                truncated = len(data) > MAX_FILE_BYTES
                if truncated:
                    data = data[:MAX_FILE_BYTES]
                payload = {"size": size, "mtime": mtime, "truncated": truncated}
                if full and size > TAIL_THRESHOLD:
                    payload["fullView"] = True
                if not is_probably_text(data):
                    payload["binary"] = True
                else:
                    payload["binary"] = False
                    payload["content"] = data.decode("utf-8", errors="replace")
                self._json(payload)
            except Exception:
                self._send(400, b"bad path", "text/plain")
            return

        if path == "/api/raw":
            rel = qs.get("path", [""])[0]
            try:
                p = safe_path(rel)
                if not p.is_file():
                    self._send(404, b"not found", "text/plain"); return
                ext = p.suffix.lower().lstrip(".")
                ctype = IMAGE_MIME.get(ext)
                if ctype:
                    if p.stat().st_size > MAX_IMAGE_BYTES:
                        self._send(413, b"too large", "text/plain"); return
                    self._send(200, p.read_bytes(), ctype)
                    return
                # anything else: stream the exact bytes (media inline,
                # unknown types as octet-stream so browsers download them)
                ctype = (VIDEO_MIME.get(ext) or AUDIO_MIME.get(ext)
                         or MODEL_MIME.get(ext) or "application/octet-stream")
                self._stream_file(p, ctype)
            except Exception:
                self._send(400, b"bad path", "text/plain")
            return

        if path == "/api/download":
            rel = qs.get("path", [""])[0]
            try:
                p = safe_path(rel)
                if not p.is_file():
                    self._send(404, b"not found", "text/plain"); return
                self._stream_file(p, "application/octet-stream", download=True)
            except Exception:
                self._send(400, b"bad path", "text/plain")
            return

        if path == "/api/watch":
            rel = qs.get("path", [""])[0]
            old = qs.get("mtime", [""])[0]
            try:
                p = safe_path(rel)
            except Exception:
                self._send(400, b"bad path", "text/plain"); return
            has_size = "-" in old or old == "deleted"
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                try:
                    st = p.stat()
                    cur = f"{st.st_mtime_ns}-{st.st_size}" if has_size else str(st.st_mtime_ns)
                    if cur != old:
                        # Wait briefly for non-atomic writes (truncate + multi-chunk write) to settle
                        settle_end = time.monotonic() + 1.0
                        prev_sig = (st.st_mtime_ns, st.st_size)
                        while time.monotonic() < settle_end:
                            time.sleep(0.05)
                            try:
                                st2 = p.stat()
                            except OSError:
                                break
                            sig = (st2.st_mtime_ns, st2.st_size)
                            if st2.st_size > 0 and sig == prev_sig:
                                st = st2
                                break
                            prev_sig = sig
                            st = st2
                        cur = f"{st.st_mtime_ns}-{st.st_size}" if has_size else str(st.st_mtime_ns)
                        self._json({"changed": True, "mtime": cur}); return
                except FileNotFoundError:
                    if old != "deleted":
                        # Wait briefly in case file is being replaced via unlink + create
                        reappear_end = time.monotonic() + 0.5
                        reappeared = False
                        while time.monotonic() < reappear_end:
                            time.sleep(0.05)
                            if p.exists():
                                reappeared = True
                                break
                        if not reappeared:
                            self._json({"changed": True, "deleted": True}); return
                        continue
                except OSError:
                    pass
                time.sleep(0.5)
            self._send(204, b"", "text/plain")
            return

        if path == "/api/grep":
            q = qs.get("q", [""])[0]
            ci = qs.get("i", ["1"])[0] == "1"
            if not q:
                self._json({"results": [], "total": 0, "truncated": False}); return
            try:
                self._json(do_grep(q, ci))
            except Exception as exc:
                self._send(500, f"grep error: {exc}".encode(), "text/plain")
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._authed(qs):
            self._send(401, b"unauthorized", "text/plain; charset=utf-8"); return
        # without a token there is no SameSite cookie guarding writes, so
        # reject cross-site POSTs (other web pages targeting this server)
        origin = self.headers.get("Origin")
        if not TOKEN and origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.close_connection = True  # request body is left unread
            self._send(403, b"cross-origin", "text/plain; charset=utf-8"); return

        if u.path == "/api/insert":
            rel = qs.get("path", [""])[0]
            try:
                line_no = int(qs.get("line", ["0"])[0])
            except ValueError:
                self._send(400, b"bad line", "text/plain"); return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 64 * 1024:
                self._send(400, b"empty or too large", "text/plain"); return
            body = self.rfile.read(length)
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                self._send(400, b"utf-8 required", "text/plain"); return
            try:
                p = safe_path(rel)
                if not p.is_file():
                    self._send(404, b"not found", "text/plain"); return
                cur = p.read_text("utf-8", errors="replace")
                lines = cur.split("\n")
                idx = max(0, min(line_no, len(lines)))
                insert_lines = text.split("\n")
                # strip a single trailing empty line so user typing "foo\n"
                # doesn't produce a blank line
                if insert_lines and insert_lines[-1] == "":
                    insert_lines.pop()
                if not insert_lines:
                    self._send(400, b"nothing to insert", "text/plain"); return
                lines[idx:idx] = insert_lines
                new = "\n".join(lines)
                p.write_text(new, "utf-8")
                self._json({"ok": True, "inserted": len(insert_lines)})
            except Exception as exc:
                self._send(500, f"insert error: {exc}".encode(), "text/plain")
            return

        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # quieter default log
        sys.stderr.write("[fb] %s - %s\n" % (self.address_string(), fmt % args))


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _is_tailscale(ip: str) -> bool:
    # Tailscale CGNAT range 100.64.0.0/10
    try:
        a, b, *_ = (int(p) for p in ip.split("."))
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def list_interfaces() -> list[tuple[str, str]]:
    """Return [(label, ip), ...] for non-loopback IPv4 addresses."""
    import subprocess
    try:
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    out: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = parts[1]
        if iface == "lo":
            continue
        ip = parts[3].split("/")[0]
        label = "tailscale" if _is_tailscale(ip) or iface.startswith("tailscale") else iface
        out.append((label, ip))
    # Ensure tailscale entries come last (they're usually the more memorable URL).
    out.sort(key=lambda t: (t[0] == "tailscale", t[0]))
    return out


def bind_server(host: str, port: int) -> ThreadingHTTPServer:
    """Bind the HTTP server. If port==0, pick a random free port in 8000-8999."""
    if port:
        return ThreadingHTTPServer((host, port), Handler)
    candidates = list(range(8000, 9000))
    random.shuffle(candidates)
    last_err: OSError | None = None
    for p in candidates[:50]:
        try:
            return ThreadingHTTPServer((host, p), Handler)
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                last_err = exc
                continue
            raise
    # all 50 picks collided — fall back to OS-assigned ephemeral port
    try:
        return ThreadingHTTPServer((host, 0), Handler)
    except OSError as exc:
        raise last_err or exc


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=None,
                    help="bind address (default: 0.0.0.0, or 127.0.0.1 with --no-token)")
    ap.add_argument("--port", type=int, default=0,
                    help="default: random free port in 8000-8999")
    ap.add_argument("--token", default=None,
                    help="auth token (default: $FB_TOKEN, else random)")
    ap.add_argument("--no-token", action="store_true",
                    help="disable auth: anyone who can reach the port gets in")
    args = ap.parse_args()
    if args.no_token and args.token:
        ap.error("--no-token and --token are mutually exclusive")

    global TOKEN
    if not args.no_token:
        TOKEN = args.token or os.environ.get("FB_TOKEN") or secrets.token_urlsafe(18)
    query = f"?t={TOKEN}" if TOKEN else ""
    host = args.host or ("127.0.0.1" if args.no_token else "0.0.0.0")

    httpd = bind_server(host, args.port)
    port = httpd.server_address[1]
    if host in ("127.0.0.1", "localhost"):
        ifaces = []  # loopback only: other interfaces are unreachable
    else:
        ifaces = list_interfaces() or [("lan", lan_ip())]
    label_w = max((len(lbl) for lbl, _ in ifaces), default=5)

    def print_urls() -> None:
        print(f"[fb] serving: {ROOT}")
        if not TOKEN:
            print("[fb] WARNING: auth disabled (--no-token)")
        seen: set[str] = set()
        for label, ip in ifaces:
            if ip in seen:
                continue
            seen.add(ip)
            print(f"[fb] {label:<{label_w}}  http://{ip}:{port}/{query}")
        print(f"[fb] {'local':<{label_w}}  http://127.0.0.1:{port}/{query}")

    print_urls()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        while True:
            line = sys.stdin.readline()
            if not line:  # EOF (Ctrl-D or stdin closed) — keep serving
                threading.Event().wait()
                break
            print_urls()
    except KeyboardInterrupt:
        print("\n[fb] bye")


if __name__ == "__main__":
    main()
