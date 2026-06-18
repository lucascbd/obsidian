"""
app.py — Interface web do agente. Acessível via browser na rede local.
"""

import os
import queue
import json
import logging
import threading
from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
from flask_cors import CORS
from agent import ask, weekly_summary, market_insights, summarize_meeting, vault_review, repair_vault, purge_vault, purge_orphan_leaves, ingest_file, ingest_url, ingest_zip, get_root_folders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key")

# ── HTML da interface ────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Obsidian Agent</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  :root {
    --bg:#0f1117; --surface:#181c27; --surface2:#1e2335;
    --border:#272d42; --accent:#4f7fff; --text:#e2e8f0;
    --muted:#64748b; --label:#94a3b8;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column}
  header{border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
  header h1{font-size:15px;font-weight:600}
  .scope-wrap{display:flex;align-items:center;gap:8px}
  .scope-wrap label{font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace}
  #root-select{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:12px;color:var(--label);font-family:'Inter',sans-serif;outline:none;min-width:140px}
  #root-select:focus{border-color:var(--accent)}
  .layout{display:flex;flex:1;overflow:hidden}
  .sidebar{width:210px;flex-shrink:0;border-right:1px solid var(--border);padding:14px 10px;display:flex;flex-direction:column;gap:4px;overflow-y:auto}
  .s-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;padding:0 8px;margin:10px 0 4px}
  .s-btn{background:transparent;border:none;color:var(--label);padding:8px 10px;border-radius:7px;font-size:13px;cursor:pointer;text-align:left;font-family:'Inter',sans-serif;width:100%;display:flex;align-items:center;gap:8px}
  .s-btn:hover{background:var(--surface2);color:var(--text)}
  .s-btn.active{background:var(--accent);color:#fff}
  .main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .chat{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px}
  .chat::-webkit-scrollbar{width:4px}
  .chat::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
  .empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--muted);text-align:center;padding:40px}
  .empty .icon{font-size:40px;margin-bottom:8px}
  .empty h2{font-size:16px;color:var(--label)}
  .empty p{font-size:13px;max-width:340px;line-height:1.6}
  .msg{display:flex;flex-direction:column;gap:5px;max-width:800px}
  .msg.user{align-self:flex-end;align-items:flex-end}
  .msg.agent{align-self:flex-start}
  .bubble{padding:11px 15px;border-radius:12px;font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
  .msg.user .bubble{background:var(--accent);color:#fff;border-bottom-right-radius:4px}
  .msg.agent .bubble{background:var(--surface);border:1px solid var(--border);border-bottom-left-radius:4px}
  .msg-time{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace}
  .msg.user .msg-time{text-align:right}
  .sources{display:flex;gap:5px;flex-wrap:wrap}
  .src-tag{font-size:10px;padding:2px 7px;border-radius:4px;background:var(--surface2);border:1px solid var(--border);color:var(--muted);font-family:'JetBrains Mono',monospace}
  .typing-wrap{display:flex;align-items:center;gap:8px;padding:11px 15px;background:var(--surface);border:1px solid var(--border);border-radius:12px;border-bottom-left-radius:4px;width:fit-content}
  .typing-wrap span{width:6px;height:6px;border-radius:50%;background:var(--muted);animation:bounce 1.2s infinite}
  .typing-wrap span:nth-child(2){animation-delay:.2s}
  .typing-wrap span:nth-child(3){animation-delay:.4s}
  @keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
  .prog-list{padding:8px 14px;background:var(--surface);border:1px solid var(--border);border-radius:12px;border-bottom-left-radius:4px;display:flex;flex-direction:column;gap:4px;max-width:580px}
  .prog-item{font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;display:flex;align-items:flex-start;gap:6px}
  .prog-item .t-name{color:var(--accent);font-weight:500;min-width:90px;flex-shrink:0}
  .prog-item .t-detail{color:var(--label);word-break:break-all}
  .input-wrap{border-top:1px solid var(--border);padding:14px 20px;display:flex;flex-direction:column;gap:6px;flex-shrink:0}
  .chip-area{display:flex;gap:6px;flex-wrap:wrap}
  .chip{display:inline-flex;align-items:center;gap:5px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:3px 9px;font-size:12px;color:var(--label);font-family:'JetBrains Mono',monospace}
  .chip .rm{cursor:pointer;color:var(--muted);margin-left:2px}
  .chip .rm:hover{color:#ef4444}
  .input-row{display:flex;gap:8px;align-items:flex-end}
  textarea{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 13px;font-size:14px;color:var(--text);font-family:'Inter',sans-serif;outline:none;resize:none;min-height:44px;max-height:160px;line-height:1.5}
  textarea:focus{border-color:var(--accent)}
  .btn-icon{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--label);width:44px;height:44px;font-size:17px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .15s}
  .btn-icon:hover{border-color:var(--accent);color:var(--text)}
  .btn-send{background:var(--accent);border:none;color:#fff;padding:0 18px;border-radius:8px;font-size:14px;cursor:pointer;font-family:'Inter',sans-serif;font-weight:500;height:44px;white-space:nowrap}
  .btn-send:hover{opacity:.85}
  .btn-send:disabled{opacity:.4;cursor:not-allowed}
  .drop-overlay{position:fixed;inset:0;background:rgba(79,127,255,.15);border:2px dashed var(--accent);z-index:999;display:none;align-items:center;justify-content:center;font-size:20px;color:var(--accent);pointer-events:none}
  .drop-overlay.active{display:flex}
</style>
</head>
<body>

<div class="drop-overlay" id="drop-overlay">📄 Solte o arquivo para importar</div>

<!-- inputs de arquivo fora de qualquer popover -->
<input type="file" id="inp-analyze" style="display:none" accept=".pdf,.docx,.txt,.md,.html,.htm,.csv">
<input type="file" id="inp-ingest"  style="display:none" accept=".pdf,.docx,.txt,.md,.html,.htm,.csv,.zip">
<input type="file" id="inp-import"  style="display:none" accept=".pdf,.docx,.txt,.md,.html,.htm,.csv,.zip">

<header>
  <h1>🧠 Obsidian Agent</h1>
  <div class="scope-wrap">
    <label>ESCOPO</label>
    <select id="root-select" onchange="onRootChange()">
      <option value="">🌐 Todo o vault</option>
    </select>
  </div>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="s-label">Importar</div>
    <button class="s-btn" onclick="document.getElementById('inp-import').click()">📄 Arquivo / PDF / ZIP</button>
    <button class="s-btn" onclick="promptURL()">🔗 URL / Artigo</button>
    <div class="s-label">Ações rápidas</div>
    <button class="s-btn" onclick="runAction('weekly')">📅 Resumo semanal</button>
    <button class="s-btn" onclick="runAction('insights')">💡 Insights de mercado</button>
    <button class="s-btn" onclick="runAction('vault-review')">🔗 Revisar vault</button>
    <div class="s-label">Filtrar busca</div>
    <button class="s-btn active" id="f-all" onclick="setFilter(null,this)">🗂 Tudo</button>
    <div id="filter-roots"></div>
  </aside>

  <div class="main">
    <div class="chat scrollbar-thin" id="chat">
      <div class="empty" id="empty">
        <div class="icon">🔍</div>
        <h2>Pergunte sobre seu vault</h2>
        <p>Use as ações rápidas ou digite uma pergunta. O agente busca nas suas notas e responde com base no contexto real.</p>
      </div>
    </div>

    <div class="input-wrap">
      <div class="chip-area" id="chip-area"></div>
      <div class="input-row">
        <button class="btn-icon" title="Analisar arquivo no chat" onclick="document.getElementById('inp-analyze').click()">🔍</button>
        <button class="btn-icon" title="Salvar arquivo no vault"  onclick="document.getElementById('inp-ingest').click()">📎</button>
        <textarea id="input" placeholder="Pergunte algo... (Enter envia, Shift+Enter nova linha)"
          onkeydown="handleKey(event)" oninput="autoResize(this)" rows="1"></textarea>
        <button class="btn-send" id="send-btn" onclick="sendMessage()">Enviar</button>
      </div>
    </div>
  </div>
</div>

<script>
  var activeFilter = null;
  var activeRoot   = null;
  var attachedFile = null; // {file, mode:'analyze'|'ingest'}

  // ── File inputs ─────────────────────────────────────────────────────────────
  document.getElementById('inp-analyze').addEventListener('change', function(e) {
    var f = e.target.files[0]; if (!f) return;
    e.target.value = '';
    attachedFile = {file: f, mode: 'analyze'};
    renderChip(f.name, 'analyze');
  });
  document.getElementById('inp-ingest').addEventListener('change', function(e) {
    var f = e.target.files[0]; if (!f) return;
    e.target.value = '';
    attachedFile = {file: f, mode: 'ingest'};
    renderChip(f.name, 'ingest');
  });
  document.getElementById('inp-import').addEventListener('change', function(e) {
    var f = e.target.files[0]; if (!f) return;
    e.target.value = '';
    uploadFile(f);
  });

  function renderChip(name, mode) {
    var lbl = mode === 'analyze' ? '🔍 analisar' : '💾 vault';
    document.getElementById('chip-area').innerHTML =
      '<div class="chip">📎 ' + name + ' <span style="color:var(--accent)">' + lbl + '</span>' +
      '<span class="rm" onclick="clearAttach()">×</span></div>';
  }
  function clearAttach() {
    attachedFile = null;
    document.getElementById('chip-area').innerHTML = '';
  }

  // ── Roots ───────────────────────────────────────────────────────────────────
  function onRootChange() {
    activeRoot = document.getElementById('root-select').value || null;
  }
  function getRoot() { return activeRoot; }

  async function loadRoots() {
    try {
      var r    = await fetch('/api/roots');
      var data = await r.json();
      var roots = data.roots || [];

      // Se ainda vazio, tenta vault-stats para extrair raízes dos IDs
      if (roots.length === 0) {
        var r2   = await fetch('/api/vault-stats');
        var d2   = await r2.json();
        var ids  = d2.active_note_ids || [];
        var seen = {};
        ids.forEach(function(id) {
          var parts = id.split('/');
          if (parts.length > 1) seen[parts[0]] = true;
        });
        roots = Object.keys(seen).sort();
      }

      var sel  = document.getElementById('root-select');
      var filt = document.getElementById('filter-roots');
      roots.forEach(function(root) {
        var opt = document.createElement('option');
        opt.value = root;
        opt.textContent = '📁 ' + root;
        sel.appendChild(opt);

        var btn = document.createElement('button');
        btn.className = 's-btn';
        btn.id = 'f-' + root;
        btn.textContent = '📁 ' + root;
        btn.onclick = function() { setFilter(root, btn); };
        filt.appendChild(btn);
      });
    } catch(e) { console.warn('loadRoots error', e); }
  }

  function setFilter(col, el) {
    activeFilter = col;
    document.querySelectorAll('.sidebar .s-btn').forEach(function(b) { b.classList.remove('active'); });
    el.classList.add('active');
  }

  // ── UI helpers ───────────────────────────────────────────────────────────────
  function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }
  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }
  function fmtTime() {
    return new Date().toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});
  }
  function appendMsg(role, content, sources) {
    var chat = document.getElementById('chat');
    var emp  = document.getElementById('empty');
    if (emp) emp.remove();

    var div    = document.createElement('div');
    div.className = 'msg ' + role;
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = content;
    div.appendChild(bubble);

    if (sources && sources.length) {
      var src = document.createElement('div');
      src.className = 'sources';
      sources.forEach(function(s) {
        var tag = document.createElement('span');
        tag.className = 'src-tag';
        tag.textContent = s.split('/').pop();
        tag.title = s;
        src.appendChild(tag);
      });
      div.appendChild(src);
    }
    var ts = document.createElement('div');
    ts.className = 'msg-time';
    ts.textContent = fmtTime();
    div.appendChild(ts);
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return div;
  }
  function appendTyping() {
    var chat = document.getElementById('chat');
    var div  = document.createElement('div');
    div.className = 'msg agent';
    div.id = 'typing';
    div.innerHTML = '<div class="typing-wrap"><span></span><span></span><span></span></div>';
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }
  function removeTyping() { var el = document.getElementById('typing'); if(el) el.remove(); }

  function createProgPanel() {
    var chat = document.getElementById('chat');
    var emp  = document.getElementById('empty'); if(emp) emp.remove();
    var div  = document.createElement('div');
    div.className = 'msg agent'; div.id = 'prog-panel';
    var list = document.createElement('div');
    list.className = 'prog-list';
    div.appendChild(list);
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return list;
  }
  function addProgItem(list, tool, detail) {
    var item = document.createElement('div');
    item.className = 'prog-item';
    item.innerHTML = '<span class="t-name">' + tool + '</span><span class="t-detail">' + (detail||'') + '</span>';
    list.appendChild(item);
    document.getElementById('chat').scrollTop = 99999;
  }
  function removeProgPanel() { var el = document.getElementById('prog-panel'); if(el) el.remove(); }

  // ── Send ─────────────────────────────────────────────────────────────────────
  async function sendMessage() {
    var input   = document.getElementById('input');
    var q       = input.value.trim();
    var hasFile = attachedFile !== null;
    if (!q && !hasFile) return;

    input.value = '';
    input.style.height = 'auto';
    document.getElementById('send-btn').disabled = true;

    var userLabel = hasFile ? ('📎 ' + attachedFile.file.name + (q ? '\\n' + q : '')) : q;
    appendMsg('user', userLabel);

    // Salvar arquivo no vault
    if (hasFile && attachedFile.mode === 'ingest') {
      var snap = attachedFile; clearAttach();
      appendTyping();
      var form = new FormData();
      form.append('file', snap.file);
      if (getRoot()) form.append('root', getRoot());
      try {
        var r = await fetch('/api/upload', {method:'POST', body:form});
        var d = await r.json();
        removeTyping();
        appendMsg('agent', d.answer, d.sources);
      } catch(e) { removeTyping(); appendMsg('agent', '❌ Erro ao importar arquivo.'); }
      document.getElementById('send-btn').disabled = false;
      input.focus(); return;
    }

    // Análise de arquivo ou pergunta normal (SSE)
    var url, init;
    if (hasFile && attachedFile.mode === 'analyze') {
      var snap = attachedFile; clearAttach();
      var form = new FormData();
      form.append('file', snap.file);
      if (q) form.append('question', q);
      if (getRoot()) form.append('root', getRoot());
      url = '/api/analyze/stream'; init = {method:'POST', body:form};
    } else {
      var body = {question: q};
      if (activeFilter) body.collections = [activeFilter];
      if (getRoot()) body.root = getRoot();
      url = '/api/ask/stream';
      init = {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
    }

    var progList = null;
    try {
      var resp = await fetch(url, init);
      if (!resp.ok) throw new Error('http ' + resp.status);
      progList = createProgPanel();
      var reader  = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf     = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, {stream:true});
        var lines = buf.split('\\n');
        buf = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (!line.startsWith('data: ')) continue;
          try {
            var evt = JSON.parse(line.slice(6));
            if (evt.type === 'progress') { addProgItem(progList, evt.tool, evt.detail); }
            else if (evt.type === 'done')  { removeProgPanel(); appendMsg('agent', evt.answer, evt.sources); }
            else if (evt.type === 'error') { removeProgPanel(); appendMsg('agent', '❌ ' + evt.message); }
          } catch(e) {}
        }
      }
    } catch(e) {
      removeProgPanel(); removeTyping();
      appendMsg('agent', '❌ Erro de conexão com o agente. Verifique os logs.');
    }
    document.getElementById('send-btn').disabled = false;
    input.focus();
  }

  // ── Upload direto (sidebar / drag-drop) ─────────────────────────────────────
  async function uploadFile(file) {
    appendMsg('user', (file.name.endsWith('.zip') ? '🗜 ' : '📄 ') + 'Importando: ' + file.name);
    appendTyping();
    document.getElementById('send-btn').disabled = true;
    var form = new FormData();
    form.append('file', file);
    if (getRoot()) form.append('root', getRoot());
    try {
      var r = await fetch('/api/upload', {method:'POST', body:form});
      var d = await r.json();
      removeTyping(); appendMsg('agent', d.answer, d.sources);
    } catch(e) { removeTyping(); appendMsg('agent', '❌ Erro ao importar.'); }
    document.getElementById('send-btn').disabled = false;
  }

  async function promptURL() {
    var url = prompt('Cole a URL do artigo ou página:');
    if (!url || !url.startsWith('http')) return;
    appendMsg('user', '🔗 ' + url);
    appendTyping();
    document.getElementById('send-btn').disabled = true;
    try {
      var r = await fetch('/api/upload-url', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:url, root:getRoot()})});
      var d = await r.json();
      removeTyping(); appendMsg('agent', d.answer, d.sources);
    } catch(e) { removeTyping(); appendMsg('agent', '❌ Erro ao importar URL.'); }
    document.getElementById('send-btn').disabled = false;
  }

  async function runAction(action) {
    var labels = {weekly:'📅 Resumo semanal', insights:'💡 Insights de mercado', 'vault-review':'🔗 Revisar vault'};
    appendMsg('user', labels[action] + (getRoot() ? ' (' + getRoot() + ')' : ''));
    appendTyping();
    document.getElementById('send-btn').disabled = true;
    try {
      var r = await fetch('/api/' + action, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({root:getRoot()})});
      var d = await r.json();
      removeTyping(); appendMsg('agent', d.answer, d.sources);
    } catch(e) { removeTyping(); appendMsg('agent', '❌ Erro ao executar ação.'); }
    document.getElementById('send-btn').disabled = false;
  }

  // ── Drag & drop ──────────────────────────────────────────────────────────────
  document.addEventListener('dragover', function(e) { e.preventDefault(); document.getElementById('drop-overlay').classList.add('active'); });
  document.addEventListener('dragleave', function(e) { if (!e.relatedTarget) document.getElementById('drop-overlay').classList.remove('active'); });
  document.addEventListener('drop', function(e) {
    e.preventDefault();
    document.getElementById('drop-overlay').classList.remove('active');
    var f = e.dataTransfer.files[0]; if (f) uploadFile(f);
  });

  loadRoots();
</script>
</body>
</html>
"""

# ── Rotas ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/roots", methods=["GET"])
def api_roots():
    roots = get_root_folders()
    # Fallback: usa coleções do ChromaDB se CouchDB retornar vazio
    if not roots:
        try:
            import chromadb as _chroma
            from agent import CHROMA_HOST, CHROMA_PORT
            client = _chroma.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
            roots = sorted(c.name for c in client.list_collections())
        except Exception:
            pass
    log.info(f"api_roots: {roots}")
    return jsonify({"roots": roots})


@app.route("/api/vault-stats", methods=["GET"])
def api_vault_stats():
    """Diagnóstico: contagem de docs por tipo e lista de raízes."""
    import requests as req
    from agent import COUCHDB_URL, COUCHDB_DB, COUCHDB_AUTH
    try:
        r = req.get(f"{COUCHDB_URL}/{COUCHDB_DB}/_all_docs",
                    params={"include_docs": True}, auth=COUCHDB_AUTH, timeout=30)
        r.raise_for_status()
        rows = r.json().get("rows", [])
        active, deleted, leaves, system = [], [], [], []
        for row in rows:
            nid = row["id"]
            doc = row.get("doc", {})
            if nid.startswith("_"):
                system.append(nid)
            elif nid.startswith("h:"):
                leaves.append(nid)
            elif doc.get("deleted"):
                deleted.append(nid)
            else:
                active.append(nid)
        return jsonify({
            "total_docs": len(rows),
            "active_notes": len(active),
            "deleted_notes": len(deleted),
            "leaf_docs": len(leaves),
            "system_docs": len(system),
            "active_note_ids": sorted(active),
            "deleted_note_ids": sorted(deleted),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Endpoint síncrono (compatibilidade). Retorna JSON quando o agente termina."""
    data        = request.get_json()
    question    = data.get("question", "").strip()
    collections = data.get("collections")
    root        = data.get("root") or None
    if not question:
        return jsonify({"error": "question obrigatório"}), 400
    result = ask(question, collections, root=root)
    return jsonify(result)


@app.route("/api/ask/stream", methods=["POST"])
def api_ask_stream():
    """Endpoint SSE: emite eventos de progresso enquanto o agente trabalha."""
    data        = request.get_json()
    question    = (data or {}).get("question", "").strip()
    collections = (data or {}).get("collections")
    root        = (data or {}).get("root") or None

    if not question:
        def _err():
            yield f"data: {json.dumps({'type': 'error', 'message': 'question obrigatório'})}\n\n"
        return Response(stream_with_context(_err()), mimetype="text/event-stream")

    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def _on_progress(event: dict):
        """Callback chamado pelo agente a cada tool call."""
        tool   = event.get("tool", "")
        args   = event.get("args", {})
        preview = event.get("result_preview", "")

        # Monta detalhe legível por tool
        if tool == "move_note":
            detail = f"{args.get('source_id', '')} → {args.get('dest_id', '')}"
        elif tool == "add_tags":
            tags = args.get("tags", [])
            detail = f"{args.get('note_id', '')} +{tags}"
        elif tool == "delete_note":
            detail = args.get("note_id", "")
        elif tool == "create_note":
            detail = args.get("path", "")
        elif tool == "edit_note":
            detail = args.get("note_id", "")
        elif tool == "search_vault":
            detail = args.get("query", "")[:80]
        elif tool == "read_note":
            detail = args.get("note_id", "")
        elif tool == "ensure_settings":
            detail = f"root={args.get('root', '')}"
        else:
            detail = preview[:120] if preview else ""

        q.put({"type": "progress", "tool": tool, "detail": detail})

    def _run_agent():
        try:
            result = ask(question, collections, root=root, on_progress=_on_progress)
            q.put({"type": "done", "answer": result.get("answer", ""), "sources": result.get("sources", [])})
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=_run_agent, daemon=True)
    thread.start()

    def _generate():
        while True:
            try:
                item = q.get(timeout=30)  # verifica a cada 30s
            except queue.Empty:
                # Agente ainda rodando — envia keepalive para não fechar a conexão
                if thread.is_alive():
                    yield ": keepalive\n\n"
                    continue
                # Thread morreu sem enviar sentinel — erro silencioso
                yield f"data: {json.dumps({'type': 'error', 'message': 'agente encerrou inesperadamente'})}\n\n"
                break
            if item is _SENTINEL:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/analyze/stream", methods=["POST"])
def api_analyze_stream():
    """Extrai texto do arquivo enviado e passa para o agente analisar via SSE."""
    from agent import _extract_text_from_file, ask as agent_ask

    if "file" not in request.files:
        def _err():
            yield f"data: {json.dumps({'type':'error','message':'arquivo obrigatório'})}\n\n"
        return Response(stream_with_context(_err()), mimetype="text/event-stream")

    f        = request.files["file"]
    question = request.form.get("question", "").strip()
    root     = request.form.get("root") or None
    content  = f.read()

    try:
        raw_text = _extract_text_from_file(f.filename, content)
    except Exception as e:
        def _err():
            yield f"data: {json.dumps({'type':'error','message':f'Erro ao extrair texto: {e}'})}\n\n"
        return Response(stream_with_context(_err()), mimetype="text/event-stream")

    if not raw_text.strip():
        def _err():
            yield f"data: {json.dumps({'type':'error','message':'Não foi possível extrair texto do arquivo.'})}\n\n"
        return Response(stream_with_context(_err()), mimetype="text/event-stream")

    instruction = (
        f"O usuário enviou o arquivo **{f.filename}** com o seguinte conteúdo:\n\n"
        f"```\n{raw_text[:12000]}\n```\n\n"
    )
    if question:
        instruction += f"Pergunta do usuário: {question}"
    else:
        instruction += (
            "Analise este documento e forneça:\n"
            "1. Resumo dos pontos principais\n"
            "2. Insights relevantes\n"
            "3. Possíveis conexões com notas do vault (use search_vault para verificar)\n"
            "4. Sugestão de onde salvar no vault caso o usuário queira"
        )

    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def _on_progress(event: dict):
        tool   = event.get("tool", "")
        args   = event.get("args", {})
        detail = args.get("query", args.get("note_id", ""))[:80]
        q.put({"type": "progress", "tool": tool, "detail": detail})

    def _run():
        try:
            result = agent_ask(instruction, root=root, on_progress=_on_progress)
            q.put({"type": "done", "answer": result.get("answer", ""), "sources": result.get("sources", [])})
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def _generate():
        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                if thread.is_alive():
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps({'type':'error','message':'agente encerrou inesperadamente'})}\n\n"
                break
            if item is _SENTINEL:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/weekly", methods=["POST"])
def api_weekly():
    data = request.get_json(silent=True) or {}
    return jsonify(weekly_summary(root=data.get("root") or None))


@app.route("/api/insights", methods=["POST"])
def api_insights():
    data = request.get_json(silent=True) or {}
    return jsonify(market_insights(root=data.get("root") or None))


@app.route("/api/vault-review", methods=["POST"])
def api_vault_review():
    data = request.get_json(silent=True) or {}
    return jsonify(vault_review(root=data.get("root") or None))


@app.route("/api/repair", methods=["POST"])
def api_repair():
    return jsonify(repair_vault())


@app.route("/api/purge", methods=["POST"])
def api_purge():
    data        = request.get_json(silent=True) or {}
    keep_prefix = data.get("keep_prefix") or None
    return jsonify(purge_vault(keep_prefix=keep_prefix))


@app.route("/api/purge-leaves", methods=["POST"])
def api_purge_leaves():
    return jsonify(purge_orphan_leaves())


@app.route("/api/meeting", methods=["POST"])
def api_meeting():
    data    = request.get_json()
    note_id = data.get("note_id", "").strip()
    root    = data.get("root") or None
    if not note_id:
        return jsonify({"error": "note_id obrigatório"}), 400
    return jsonify(summarize_meeting(note_id, root=root))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "arquivo obrigatório"}), 400
    f    = request.files["file"]
    root = request.form.get("root") or None
    content = f.read()
    if not content:
        return jsonify({"error": "arquivo vazio"}), 400
    if f.filename.lower().endswith(".zip"):
        return jsonify(ingest_zip(content, root=root))
    return jsonify(ingest_file(f.filename, content, root=root))


@app.route("/api/upload-url", methods=["POST"])
def api_upload_url():
    data = request.get_json()
    url  = (data or {}).get("url", "").strip()
    root = (data or {}).get("root") or None
    if not url:
        return jsonify({"error": "url obrigatória"}), 400
    result = ingest_url(url, root=root)
    return jsonify(result)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
