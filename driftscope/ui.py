"""The browser UI for the scan service -- one self-contained page, no build step.

Served by `server._Handler` at `GET /ui` (and at `GET /` when the client accepts
`text/html`). The form POSTs to `/scan?stream=1` and reads the server's Server-Sent
Events: progress events drive a live status line + per-ecosystem chips + a spinner,
and the final `done` event carries the rendered report, shown in a sandboxed iframe.
Everything is inline (no external CSS/JS/fonts), per the project's zero-dep rule.
"""

from __future__ import annotations

# No f-string / .format here: the CSS and JS are full of `{}`, so this stays a
# plain literal and the braces are themselves.
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DriftScope</title>
<style>
  :root { color-scheme: light dark; --bd:#8883; --mut:#8a8a8a; --accent:#3a7bd5; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; line-height: 1.45; }
  header { padding: 20px 24px 8px; }
  header h1 { margin: 0; font-size: 1.4rem; letter-spacing: -.01em; }
  header p { margin: 4px 0 0; color: var(--mut); font-size: .9rem; }
  main { padding: 12px 24px 40px; max-width: 1100px; }
  form { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
         padding: 14px; border: 1px solid var(--bd); border-radius: 10px; }
  input[type=text], select { padding: 8px 10px; border: 1px solid var(--bd);
         border-radius: 7px; background: transparent; color: inherit; font: inherit; }
  #repo { flex: 1 1 340px; min-width: 240px; }
  label.chk { display: inline-flex; gap: 5px; align-items: center; font-size: .85rem; color: var(--mut); }
  button { padding: 8px 18px; border: 0; border-radius: 7px; background: var(--accent);
           color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .55; cursor: progress; }
  #status { margin: 16px 2px 6px; font-size: .92rem; min-height: 1.2em; display: flex; align-items: center; }
  #status.err #stext { color: #d9534f; }
  #status.done #stext { color: #2e7d32; }
  .spin { display: inline-block; width: 14px; height: 14px; margin-right: 9px;
          border: 2px solid #8884; border-top-color: var(--accent); border-radius: 50%;
          animation: spin .7s linear infinite; }
  .spin[hidden] { display: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #ecos { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0; }
  .ecochip { font-size: .74rem; padding: 2px 9px; border: 1px solid var(--bd);
             border-radius: 999px; color: var(--mut); font-variant-numeric: tabular-nums; }
  .ecochip.done { border-color: #2e7d32; color: #2e7d32; }
  #summary { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
  .chip { font-size: .78rem; font-weight: 600; padding: 3px 9px; border-radius: 999px; color: #fff; }
  .critical { background:#b3261e } .high { background:#d9730d } .medium { background:#b58900 }
  .low { background:#3a7bd5 } .current { background:#2e7d32 } .unknown { background:#6b7280 }
  iframe { width: 100%; height: 72vh; border: 1px solid var(--bd); border-radius: 10px; margin-top: 8px; background: #fff; }
  pre { overflow: auto; padding: 14px; border: 1px solid var(--bd); border-radius: 10px; font-size: .82rem; }
  footer { color: var(--mut); font-size: .78rem; padding: 0 24px 24px; }
</style>
</head>
<body>
<header>
  <h1>DriftScope</h1>
  <p>Point it at a repo. See how far behind latest stable everything is.</p>
</header>
<main>
  <form id="f">
    <input type="text" id="repo" placeholder="https://github.com/org/repo" required autocomplete="off" autofocus>
    <select id="format" title="report format">
      <option value="html" selected>HTML report</option>
      <option value="markdown">Markdown</option>
      <option value="json">JSON</option>
      <option value="text">Text</option>
    </select>
    <label class="chk"><input type="checkbox" id="no_az" checked> no&nbsp;az</label>
    <label class="chk"><input type="checkbox" id="no_github"> no&nbsp;GitHub</label>
    <button id="go" type="submit">Scan</button>
  </form>
  <div id="status"><span id="spin" class="spin" hidden></span><span id="stext"></span></div>
  <div id="ecos"></div>
  <div id="summary"></div>
  <div id="out"></div>
</main>
<footer>Streams live progress over Server-Sent Events. JSON API on the same URL: <code>POST /scan {"repo": "..."}</code>.</footer>
<script>
(function () {
  var f = document.getElementById('f'), go = document.getElementById('go'),
      statusEl = document.getElementById('status'), spin = document.getElementById('spin'),
      stext = document.getElementById('stext'), ecos = document.getElementById('ecos'),
      summary = document.getElementById('summary'), out = document.getElementById('out');

  function setStatus(text, busy, cls) {
    stext.textContent = text; spin.hidden = !busy;
    statusEl.className = cls || '';
  }
  function sevChips(header) {
    summary.innerHTML = '';
    if (!header) return;
    header.split(',').forEach(function (part) {
      var kv = part.split('='); if (kv.length !== 2) return;
      var s = document.createElement('span');
      s.className = 'chip ' + kv[0]; s.textContent = kv[0] + ' ' + kv[1];
      summary.appendChild(s);
    });
  }

  f.addEventListener('submit', async function (e) {
    e.preventDefault();
    var repo = document.getElementById('repo').value.trim();
    if (!repo) return;
    var format = document.getElementById('format').value;
    var body = {
      repo: repo, format: format,
      no_az: document.getElementById('no_az').checked,
      no_github: document.getElementById('no_github').checked
    };
    go.disabled = true;
    ecos.innerHTML = ''; summary.innerHTML = ''; out.innerHTML = '';
    setStatus('Starting …', true);
    var t0 = performance.now();
    var ecoDone = {}, ecoTotal = {};

    function ecoChip(name) {
      var id = 'eco-' + name.replace(/[^a-z0-9]/gi, '_');
      var el = document.getElementById(id);
      if (!el) { el = document.createElement('span'); el.id = id; el.className = 'ecochip'; ecos.appendChild(el); }
      var d = ecoDone[name] || 0, t = ecoTotal[name] || 0;
      el.textContent = name + ' ' + d + '/' + t;
      if (t && d >= t) el.classList.add('done');
      return el;
    }

    function onProgress(p) {
      if (p.phase === 'cloning') setStatus('Cloning ' + p.repo + ' …', true);
      else if (p.phase === 'cloned') setStatus('Cloned' + (p.commit ? ' @ ' + p.commit : '') + '; scanning files …', true);
      else if (p.phase === 'discovered') {
        ecoTotal = p.ecosystems || {}; ecoDone = {};
        Object.keys(ecoTotal).sort().forEach(ecoChip);
        setStatus('Found ' + p.components + ' components across ' + Object.keys(ecoTotal).length + ' ecosystems; resolving …', true);
      } else if (p.phase === 'resolving') {
        ecoDone[p.ecosystem] = (ecoDone[p.ecosystem] || 0) + 1;
        ecoChip(p.ecosystem);
        setStatus('Resolving ' + p.ecosystem + ' … ' + p.done + '/' + p.total, true);
      } else if (p.phase === 'deltas') setStatus('Analyzing upstream deltas for ' + p.count + ' findings …', true);
      else if (p.phase === 'vulns') setStatus('Checking known vulnerabilities …', true);
      else if (p.phase === 'rendering') setStatus('Rendering report …', true);
    }

    function onDone(r) {
      if (r.status >= 400) {
        var msg = r.body; try { msg = JSON.parse(r.body).error || r.body; } catch (_) {}
        setStatus('Error ' + r.status + ': ' + msg, false, 'err'); ecos.innerHTML = '';
        return;
      }
      sevChips(r.severity);
      var secs = ((performance.now() - t0) / 1000).toFixed(1);
      setStatus('✓ Scanned ' + repo + (r.commit ? ' @ ' + r.commit : '') + ' in ' + secs + 's', false, 'done');
      if (format === 'html') {
        var iframe = document.createElement('iframe');
        iframe.setAttribute('sandbox', 'allow-scripts');   // report's filter/collapse need JS
        iframe.srcdoc = r.body; out.appendChild(iframe);
      } else {
        var pre = document.createElement('pre'); pre.textContent = r.body; out.appendChild(pre);
      }
    }

    try {
      var res = await fetch('/scan?stream=1', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify(body)
      });
      // Pre-scan errors (503 busy, 400 bad body) come back as plain JSON, not a stream.
      var ctype = res.headers.get('Content-Type') || '';
      if (ctype.indexOf('text/event-stream') < 0) {
        var t = await res.text(); var m = t; try { m = JSON.parse(t).error || t; } catch (_) {}
        setStatus('Error ' + res.status + ': ' + m, false, 'err'); return;
      }
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var sep;
        while ((sep = buf.indexOf('\\n\\n')) >= 0) {
          var frame = buf.slice(0, sep); buf = buf.slice(sep + 2);
          var ev = 'message', data = '';
          frame.split('\\n').forEach(function (line) {
            if (line.indexOf('event:') === 0) ev = line.slice(6).trim();
            else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
          });
          if (!data) continue;
          var payload = JSON.parse(data);
          if (ev === 'progress') onProgress(payload);
          else if (ev === 'done') onDone(payload);
        }
      }
    } catch (err) {
      setStatus('Request failed: ' + err, false, 'err');
    } finally {
      go.disabled = false;
    }
  });
})();
</script>
</body>
</html>
"""
