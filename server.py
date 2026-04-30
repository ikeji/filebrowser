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
from urllib.parse import parse_qs, urlparse

ROOT = Path.cwd().resolve()
TOKEN = ""  # set in main()

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
  color: #8b949e; position: sticky; top: 0; z-index: 1; }

#imageWrap { padding: 12px; text-align: center; }
#imageView { max-width: 100%; height: auto;
  background: #161b22; border-radius: 4px;
  image-rendering: -webkit-optimize-contrast; }
#imageView.actual { max-width: none; cursor: zoom-out; }
#imageView:not(.actual) { cursor: zoom-in; }

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
    <div id="tailNote" style="display:none"></div>
    <pre id="viewerWrap" style="display:none"><code id="viewer"></code></pre>
    <div id="imageWrap" style="display:none"><img id="imageView" alt=""></div>
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

async function openFile(path, opts) {
  opts = opts || {};
  const { jumpLine, fromHistory, preserveScroll } = opts;
  currentPath = path;
  $('title').firstElementChild.textContent = path;
  document.title = ROOT_NAME + '/' + path;
  if (!fromHistory) {
    history.pushState({view: 'file', path, jumpLine: jumpLine || null}, '',
      '#' + encodeURIComponent(path));
  }
  cancelWatch();
  try {
    const res = await api('/api/file', { path });
    const wrap = $('viewerWrap');
    const viewer = $('viewer');
    const imageWrap = $('imageWrap');
    const imageView = $('imageView');
    $('placeholder').style.display = 'none';
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
    } else {
      imageWrap.style.display = 'none';
      imageView.removeAttribute('src');
      wrap.style.display = '';
      if (res.binary) {
        viewer.removeAttribute('class');
        viewer.textContent = '[binary file · ' + res.size + ' bytes]';
        tailNote.style.display = 'none';
      } else {
        viewer.textContent = res.content + (res.truncated ? '\n\n[truncated at ' + res.size + ' bytes]' : '');
        const lang = langFor(path.split('/').pop());
        viewer.className = lang ? 'language-' + lang : '';
        delete viewer.dataset.highlighted;
        if (window.hljs) {
          try { hljs.highlightElement(viewer); } catch (e) {}
        }
        wrapLines(viewer, res.startLine || 1);
        if (res.tail) {
          const last = (res.startLine || 1) + res.tailLines - 1;
          tailNote.textContent = res.partial
            ? 'tail · last ' + res.tailLines + ' lines (file > 64 MiB, line numbers relative)'
            : 'tail · lines ' + res.startLine + '–' + last + ' of ' + res.totalLines;
          tailNote.style.display = '';
        } else {
          tailNote.style.display = 'none';
        }
      }
    }
    if (jumpLine) scrollToLine(jumpLine);
    else if (savedScroll !== null) $('content').scrollTop = savedScroll;
    else if (res.tail && !res.binary) {
      const lastEl = viewer.querySelector('.line:last-child');
      if (lastEl) lastEl.scrollIntoView({ block: 'end' });
      else $('content').scrollTop = 0;
    } else $('content').scrollTop = 0;
    if (res.mtime) startWatch(path, res.mtime);
  } catch (e) {
    toast('failed to load ' + path);
  }
}

function cancelWatch() {
  if (watchCtrl) { try { watchCtrl.abort(); } catch (e) {} watchCtrl = null; }
}

async function startWatch(path, mtime) {
  cancelWatch();
  const ctrl = new AbortController();
  watchCtrl = ctrl;
  let curMtime = mtime;
  while (currentPath === path && !ctrl.signal.aborted) {
    try {
      const u = new URL('/api/watch', location.origin);
      u.searchParams.set('path', path);
      u.searchParams.set('mtime', curMtime);
      const r = await fetch(u, { signal: ctrl.signal, credentials: 'same-origin' });
      if (ctrl.signal.aborted || currentPath !== path) return;
      if (r.status === 204) continue;
      if (!r.ok) {
        await new Promise(res => setTimeout(res, 2000));
        continue;
      }
      const data = await r.json();
      if (data.deleted) { toast('file deleted'); return; }
      if (data.changed) {
        toast('reloading…');
        await openFile(path, { fromHistory: true, preserveScroll: true });
        return; // openFile starts a new watch
      }
    } catch (e) {
      if (ctrl.signal.aborted) return;
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

// --- font size / pinch zoom ---
let fontSize = parseFloat(localStorage.getItem('fs') || '13');
if (!isFinite(fontSize) || fontSize < 6 || fontSize > 60) fontSize = 13;

function applyFontSize() {
  $('viewerWrap').style.fontSize = fontSize.toFixed(1) + 'px';
  localStorage.setItem('fs', fontSize.toFixed(1));
}
applyFontSize();

$('fontUp').addEventListener('click', () => { fontSize = Math.min(60, fontSize + 1); applyFontSize(); });
$('fontDown').addEventListener('click', () => { fontSize = Math.max(6, fontSize - 1); applyFontSize(); });

const content = $('content');
let pinchStart = null;
content.addEventListener('touchstart', (e) => {
  if (e.touches.length === 2) {
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
  $('commentText').value = '';
  $('commentModal').classList.add('show');
  setTimeout(() => $('commentText').focus(), 50);
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

    def _authed(self, qs: dict) -> bool:
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
            extra = [("Set-Cookie", f"fb_token={TOKEN}; Path=/; SameSite=Strict")]
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
            try:
                p = safe_path(rel)
                if not p.is_file():
                    self._send(404, b"not found", "text/plain"); return
                st = p.stat()
                size = st.st_size
                mtime = str(st.st_mtime_ns)
                ext = p.suffix.lower().lstrip(".")
                if ext in IMAGE_MIME:
                    self._json({"size": size, "mtime": mtime, "image": True,
                                "tooLarge": size > MAX_IMAGE_BYTES})
                    return
                if size > TAIL_THRESHOLD:
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
                if not ctype:
                    self._send(415, b"unsupported", "text/plain"); return
                if p.stat().st_size > MAX_IMAGE_BYTES:
                    self._send(413, b"too large", "text/plain"); return
                self._send(200, p.read_bytes(), ctype)
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
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                try:
                    st = p.stat()
                    cur = str(st.st_mtime_ns)
                    if cur != old:
                        self._json({"changed": True, "mtime": cur}); return
                except FileNotFoundError:
                    self._json({"changed": True, "deleted": True}); return
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
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=0,
                    help="default: random free port in 8000-8999")
    ap.add_argument("--token", default=None,
                    help="auth token (default: $FB_TOKEN, else random)")
    args = ap.parse_args()

    global TOKEN
    TOKEN = args.token or os.environ.get("FB_TOKEN") or secrets.token_urlsafe(18)

    httpd = bind_server(args.host, args.port)
    port = httpd.server_address[1]
    ifaces = list_interfaces()
    if not ifaces:
        ifaces = [("lan", lan_ip())]
    label_w = max((len(lbl) for lbl, _ in ifaces), default=5)

    def print_urls() -> None:
        print(f"[fb] serving: {ROOT}")
        seen: set[str] = set()
        for label, ip in ifaces:
            if ip in seen:
                continue
            seen.add(ip)
            print(f"[fb] {label:<{label_w}}  http://{ip}:{port}/?t={TOKEN}")
        print(f"[fb] {'local':<{label_w}}  http://127.0.0.1:{port}/?t={TOKEN}")

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
