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
  input[type=text], select, #repos { padding: 8px 10px; border: 1px solid var(--bd);
         border-radius: 7px; background: transparent; color: inherit; font: inherit; }
  #repos { flex: 1 1 340px; min-width: 240px; min-height: 38px; line-height: 1.4;
           resize: vertical; overflow: hidden; }
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
  .more { margin: 8px 0 0; }
  .more summary { cursor: pointer; color: var(--mut); font-size: .85rem; }
  .more textarea { width: 100%; margin-top: 6px; padding: 9px 10px; border: 1px solid var(--bd);
                   border-radius: 8px; background: transparent; color: inherit; font: inherit;
                   font-size: .85rem; resize: vertical; }
  .tabs { display: flex; flex-wrap: wrap; gap: 4px; margin: 10px 0 0; border-bottom: 1px solid var(--bd); }
  .tab { padding: 7px 12px; border: 1px solid transparent; border-bottom: none; border-radius: 8px 8px 0 0;
         cursor: pointer; font-size: .85rem; color: var(--mut); }
  .tab.active { color: inherit; border-color: var(--bd); background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .tab .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: 1px; }
  .tab.fail { color: #d9534f; }
</style>
</head>
<body>
<header>
  <h1>DriftScope</h1>
  <p>Point it at a repo. See how far behind latest stable everything is.</p>
</header>
<main>
  <form id="f">
    <textarea id="repos" rows="1" autofocus autocomplete="off"
      placeholder="https://github.com/org/repo &mdash; add more (one per line) to compare across repos"></textarea>
    <select id="format" title="report format">
      <option value="html" selected>HTML report</option>
      <option value="markdown">Markdown</option>
      <option value="json">JSON</option>
      <option value="text">Text</option>
    </select>
    <label class="chk"><input type="checkbox" id="no_az" checked> no&nbsp;az</label>
    <label class="chk"><input type="checkbox" id="no_github"> no&nbsp;GitHub</label>
    <label class="chk" title="issue open/close rate + release cadence for the top repos (slower)"><input type="checkbox" id="velocity"> issue&nbsp;history</label>
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

  var reposEl = document.getElementById('repos');
  reposEl.addEventListener('input', function () {   // grow with the number of URLs
    reposEl.style.height = 'auto'; reposEl.style.height = reposEl.scrollHeight + 'px';
  });
  reposEl.addEventListener('keydown', function (ev) {   // Cmd/Ctrl+Enter submits
    if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') { ev.preventDefault(); f.requestSubmit(); }
  });

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

  function renderInto(container, content, isHtml) {
    if (isHtml) {
      var fr = document.createElement('iframe');
      fr.setAttribute('sandbox', 'allow-scripts');   // report's filter/collapse need JS
      fr.srcdoc = content; container.appendChild(fr);
    } else {
      var pre = document.createElement('pre'); pre.textContent = content; container.appendChild(pre);
    }
  }
  function shortName(url) { return url.replace(/\\/+$/, '').split('/').pop() || url; }

  f.addEventListener('submit', async function (e) {
    e.preventDefault();
    // One box, one-or-many: split on any whitespace/comma (URLs contain neither),
    // so a single URL scans one repo and several (any layout) compare them.
    var repos = document.getElementById('repos').value
                  .split(/[\\s,]+/).map(function (s) { return s.trim(); }).filter(Boolean);
    if (!repos.length) { setStatus('Enter at least one repo URL.', false, 'err'); return; }
    var repo = repos[0];
    var multi = repos.length > 1;
    var format = document.getElementById('format').value;
    var opts = {
      no_az: document.getElementById('no_az').checked,
      no_github: document.getElementById('no_github').checked,
      velocity: document.getElementById('velocity').checked
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
      var pre = p.repo_total ? ('Repo ' + p.repo_index + '/' + p.repo_total + ' · ') : '';
      if (p.phase === 'repo') {
        ecos.innerHTML = ''; ecoDone = {}; ecoTotal = {};
        setStatus('Repo ' + p.repo_index + '/' + p.repo_total + ': ' + shortName(p.repo) + ' …', true);
      } else if (p.phase === 'cloning') setStatus(pre + 'Cloning ' + shortName(p.repo) + ' …', true);
      else if (p.phase === 'cloned') setStatus(pre + 'Cloned' + (p.commit ? ' @ ' + p.commit : '') + '; scanning …', true);
      else if (p.phase === 'discovered') {
        ecoTotal = p.ecosystems || {}; ecoDone = {}; ecos.innerHTML = '';
        Object.keys(ecoTotal).sort().forEach(ecoChip);
        setStatus(pre + 'Found ' + p.components + ' components; resolving …', true);
      } else if (p.phase === 'resolving') {
        ecoDone[p.ecosystem] = (ecoDone[p.ecosystem] || 0) + 1;
        ecoChip(p.ecosystem);
        setStatus(pre + 'Resolving ' + p.ecosystem + ' … ' + p.done + '/' + p.total, true);
      } else if (p.phase === 'deltas') setStatus(pre + 'Analyzing upstream deltas …', true);
      else if (p.phase === 'vulns') setStatus(pre + 'Checking known vulnerabilities …', true);
      else if (p.phase === 'rendering') setStatus(pre + 'Rendering report …', true);
    }

    function onDoneSingle(r) {
      if (r.status >= 400) {
        var msg = r.body; try { msg = JSON.parse(r.body).error || r.body; } catch (_) {}
        setStatus('Error ' + r.status + ': ' + msg, false, 'err'); ecos.innerHTML = '';
        return;
      }
      sevChips(r.severity);
      var secs = ((performance.now() - t0) / 1000).toFixed(1);
      setStatus('✓ Scanned ' + shortName(repo) + (r.commit ? ' @ ' + r.commit : '') + ' in ' + secs + 's', false, 'done');
      renderInto(out, r.body, format === 'html');
    }

    function onDoneMulti(payload) {
      if (payload.error) { setStatus('Error: ' + payload.error, false, 'err'); return; }
      ecos.innerHTML = ''; summary.innerHTML = '';
      var okN = payload.repos.filter(function (x) { return x.ok; }).length;
      var secs = ((performance.now() - t0) / 1000).toFixed(1);
      setStatus('✓ Compared ' + payload.repos.length + ' repos (' + okN + ' ok) in ' + secs + 's', false, 'done');

      var bar = document.createElement('div'); bar.className = 'tabs';
      var panes = document.createElement('div');
      function addTab(labelHtml, cls, build) {
        var tab = document.createElement('div'); tab.className = 'tab ' + (cls || ''); tab.innerHTML = labelHtml;
        var pane = document.createElement('div'); pane.style.display = 'none'; build(pane);
        tab.addEventListener('click', function () {
          [].forEach.call(bar.children, function (c) { c.classList.remove('active'); });
          [].forEach.call(panes.children, function (c) { c.style.display = 'none'; });
          tab.classList.add('active'); pane.style.display = '';
        });
        bar.appendChild(tab); panes.appendChild(pane); return tab;
      }
      var first = addTab('Comparison', '', function (pane) { renderInto(pane, payload.comparison_html, true); });
      payload.repos.forEach(function (rp) {
        var dot = "<span class='dot' style='background:" + (rp.ok ? '#2e7d32' : '#d9534f') + "'></span>";
        addTab(dot + shortName(rp.repo), rp.ok ? '' : 'fail', function (pane) {
          if (rp.ok) renderInto(pane, rp.html, true);
          else {
            var d = document.createElement('div'); d.style.padding = '14px'; d.className = 'note';
            d.textContent = 'Could not scan: ' + (rp.error || 'unknown'); pane.appendChild(d);
          }
        });
      });
      out.appendChild(bar); out.appendChild(panes); first.click();
    }

    var url = multi ? '/scan-multi?stream=1' : '/scan?stream=1';
    var reqBody = multi ? Object.assign({ repos: repos }, opts)
                        : Object.assign({ repo: repo, format: format }, opts);
    try {
      var res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify(reqBody)
      });
      var ctype = res.headers.get('Content-Type') || '';
      if (ctype.indexOf('text/event-stream') < 0) {  // 400/503 come back as plain JSON
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
          else if (ev === 'done') { multi ? onDoneMulti(payload) : onDoneSingle(payload); }
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
