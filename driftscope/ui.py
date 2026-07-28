"""The browser UI for the scan service -- one self-contained page, no build step.

Served by `server._Handler` at `GET /ui` (and at `GET /` when the client accepts
`text/html`, so a browser lands on the app while curl/agents still get JSON). The
page is a form that POSTs to the same server's `/scan` and renders the returned
HTML report in an isolated iframe. Everything is inline (no external CSS/JS/fonts),
in keeping with the project's zero-dependency rule.
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
  :root { color-scheme: light dark; --bd:#8883; --mut:#8a8a8a; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; line-height: 1.45; }
  header { padding: 20px 24px 8px; }
  header h1 { margin: 0; font-size: 1.4rem; letter-spacing: -.01em; }
  header p { margin: 4px 0 0; color: var(--mut); font-size: .9rem; }
  main { padding: 12px 24px 40px; max-width: 1100px; }
  form { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
         padding: 14px; border: 1px solid var(--bd); border-radius: 10px; }
  input[type=text], select { padding: 8px 10px; border: 1px solid var(--bd);
         border-radius: 7px; background: transparent; color: inherit; font: inherit; }
  #repo { flex: 1 1 340px; min-width: 240px; }
  label.chk { display: inline-flex; gap: 5px; align-items: center; font-size: .85rem;
         color: var(--mut); }
  button { padding: 8px 18px; border: 0; border-radius: 7px; background: #3a7bd5;
         color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .55; cursor: progress; }
  #status { margin: 14px 2px 6px; font-size: .9rem; color: var(--mut); min-height: 1.2em; }
  #status.err { color: #d9534f; }
  #summary { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
  .chip { font-size: .78rem; font-weight: 600; padding: 3px 9px; border-radius: 999px;
          color: #fff; }
  .critical { background:#b3261e } .high { background:#d9730d } .medium { background:#b58900 }
  .low { background:#3a7bd5 } .current { background:#2e7d32 } .unknown { background:#6b7280 }
  iframe { width: 100%; height: 72vh; border: 1px solid var(--bd); border-radius: 10px;
           margin-top: 8px; background: #fff; }
  pre { overflow: auto; padding: 14px; border: 1px solid var(--bd); border-radius: 10px;
        font-size: .82rem; }
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
    <input type="text" id="repo" placeholder="https://github.com/org/repo" required
           autocomplete="off" autofocus>
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
  <div id="status"></div>
  <div id="summary"></div>
  <div id="out"></div>
</main>
<footer>JSON API on the same URL: <code>POST /scan {"repo": "..."}</code>. This UI just calls it.</footer>
<script>
(function () {
  var f = document.getElementById('f'), status = document.getElementById('status'),
      summary = document.getElementById('summary'), out = document.getElementById('out'),
      go = document.getElementById('go');

  function chips(header) {
    summary.innerHTML = '';
    if (!header) return;
    header.split(',').forEach(function (part) {
      var kv = part.split('='); if (kv.length !== 2) return;
      var span = document.createElement('span');
      span.className = 'chip ' + kv[0];
      span.textContent = kv[0] + ' ' + kv[1];
      summary.appendChild(span);
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
    status.className = ''; status.textContent = 'Cloning and scanning ' + repo + ' …';
    summary.innerHTML = ''; out.innerHTML = ''; go.disabled = true;
    try {
      var res = await fetch('/scan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      var text = await res.text();
      if (!res.ok) {
        var msg = text; try { msg = JSON.parse(text).error || text; } catch (_) {}
        status.className = 'err'; status.textContent = 'Error ' + res.status + ': ' + msg;
        return;
      }
      chips(res.headers.get('X-Drift-Severity'));
      var commit = res.headers.get('X-Drift-Commit');
      status.textContent = 'Scanned ' + repo + (commit ? ' @ ' + commit : '');
      if (format === 'html') {
        var iframe = document.createElement('iframe');
        iframe.setAttribute('sandbox', '');   // isolate the report; it needs no script
        iframe.srcdoc = text;
        out.appendChild(iframe);
      } else {
        var pre = document.createElement('pre'); pre.textContent = text; out.appendChild(pre);
      }
    } catch (err) {
      status.className = 'err'; status.textContent = 'Request failed: ' + err;
    } finally {
      go.disabled = false;
    }
  });
})();
</script>
</body>
</html>
"""
