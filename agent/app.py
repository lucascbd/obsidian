"""
app.py — Interface web do agente. Acessível via browser na rede local.
"""

import os
import logging
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from agent import ask, weekly_summary, market_insights, summarize_meeting, vault_review, repair_vault, purge_vault, ingest_file, ingest_url, ingest_zip, get_root_folders

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
<title>Obsidian MI Agent</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  :root {
    --bg: #0f1117; --surface: #181c27; --surface2: #1e2335;
    --border: #272d42; --accent: #4f7fff; --green: #22c55e;
    --yellow: #f59e0b; --text: #e2e8f0; --muted: #64748b; --label: #94a3b8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; }

  header {
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 15px; font-weight: 600; }
  header span { font-size: 11px; color: var(--muted); font-family: 'JetBrains Mono', monospace; }

  .layout { display: flex; flex: 1; height: calc(100vh - 57px); }

  /* Sidebar */
  .sidebar {
    width: 220px; flex-shrink: 0;
    border-right: 1px solid var(--border);
    padding: 16px 12px;
    display: flex; flex-direction: column; gap: 6px;
  }
  .sidebar-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; padding: 0 8px; margin: 8px 0 4px; }
  .action-btn {
    background: transparent; border: none; color: var(--label);
    padding: 9px 12px; border-radius: 7px; font-size: 13px;
    cursor: pointer; text-align: left; font-family: 'Inter', sans-serif;
    transition: all 0.15s; display: flex; align-items: center; gap: 8px;
  }
  .action-btn:hover { background: var(--surface2); color: var(--text); }
  .action-btn.active { background: var(--accent); color: #fff; }

  /* Main */
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

  .chat-area {
    flex: 1; overflow-y: auto; padding: 24px;
    display: flex; flex-direction: column; gap: 16px;
  }

  .msg { display: flex; flex-direction: column; gap: 6px; max-width: 820px; }
  .msg.user { align-self: flex-end; align-items: flex-end; }
  .msg.agent { align-self: flex-start; }

  .msg-bubble {
    padding: 12px 16px; border-radius: 12px; font-size: 14px; line-height: 1.6;
    white-space: pre-wrap; word-break: break-word;
  }
  .msg.user .msg-bubble { background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
  .msg.agent .msg-bubble { background: var(--surface); border: 1px solid var(--border); border-bottom-left-radius: 4px; }

  .msg-sources { display: flex; gap: 6px; flex-wrap: wrap; }
  .source-tag {
    font-size: 10px; padding: 2px 8px; border-radius: 4px;
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--muted); font-family: 'JetBrains Mono', monospace;
  }

  .typing { display: flex; align-items: center; gap: 8px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; border-bottom-left-radius: 4px; width: fit-content; }
  .typing span { width: 6px; height: 6px; border-radius: 50%; background: var(--muted); animation: bounce 1.2s infinite; }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-6px); } }

  .input-area {
    border-top: 1px solid var(--border);
    padding: 16px 24px;
    display: flex; gap: 10px; align-items: flex-end;
  }
  .col-filter {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 10px; font-size: 12px;
    color: var(--label); font-family: 'Inter', sans-serif; outline: none;
  }
  .col-filter:focus { border-color: var(--accent); }
  textarea {
    flex: 1; background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 14px; font-size: 14px;
    color: var(--text); font-family: 'Inter', sans-serif; outline: none;
    resize: none; min-height: 44px; max-height: 160px; line-height: 1.5;
    transition: border-color 0.15s;
  }
  textarea:focus { border-color: var(--accent); }
  .send-btn {
    background: var(--accent); border: none; color: #fff;
    padding: 10px 18px; border-radius: 8px; font-size: 14px;
    cursor: pointer; font-family: 'Inter', sans-serif; font-weight: 500;
    transition: opacity 0.15s; height: 44px;
  }
  .send-btn:hover { opacity: 0.85; }
  .send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .empty-chat {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 8px;
    color: var(--muted); text-align: center; padding: 40px;
  }
  .empty-chat .icon { font-size: 40px; margin-bottom: 8px; }
  .empty-chat h2 { font-size: 16px; color: var(--label); }
  .empty-chat p { font-size: 13px; max-width: 360px; line-height: 1.6; }

  .scrollbar-thin::-webkit-scrollbar { width: 4px; }
  .scrollbar-thin::-webkit-scrollbar-track { background: transparent; }
  .scrollbar-thin::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .upload-btn {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; color: var(--label); padding: 9px 12px;
    font-size: 13px; cursor: pointer; font-family: 'Inter', sans-serif;
    height: 44px; display: flex; align-items: center; gap: 6px;
    transition: all 0.15s;
  }
  .upload-btn:hover { border-color: var(--accent); color: var(--text); }

  .drop-overlay {
    position: fixed; inset: 0; background: rgba(79,127,255,.15);
    border: 2px dashed var(--accent); z-index: 999;
    display: none; align-items: center; justify-content: center;
    font-size: 20px; color: var(--accent); pointer-events: none;
  }
  .drop-overlay.active { display: flex; }
</style>
</head>
<body>
<div class="drop-overlay" id="drop-overlay">📄 Solte o arquivo para importar</div>
<input type="file" id="file-input" style="display:none" accept=".pdf,.docx,.txt,.md,.html,.htm,.csv,.zip">

<header>
  <h1>🧠 Obsidian Agent</h1>
  <div style="display:flex;align-items:center;gap:12px">
    <label style="font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace">ESCOPO</label>
    <select id="root-select" class="col-filter" onchange="onRootChange()" style="min-width:140px;font-size:13px">
      <option value="">🌐 Todo o vault</option>
    </select>
    <span id="root-badge" style="font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace"></span>
  </div>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-label">Importar</div>
    <button class="action-btn" onclick="document.getElementById('file-input').click()">📄 Arquivo (PDF, DOCX…)</button>
    <button class="action-btn" onclick="promptURL()">🔗 URL / Artigo</button>
    <div class="sidebar-label">Ações rápidas</div>
    <button class="action-btn" onclick="runAction('weekly')">📅 Resumo semanal</button>
    <button class="action-btn" onclick="runAction('insights')">💡 Insights de mercado</button>
    <button class="action-btn" onclick="runAction('vault-review')">🔗 Revisar vault</button>
    <div class="sidebar-label">Filtrar por</div>
    <button class="action-btn" id="f-all"          onclick="setFilter(null, this)">🗂 Tudo</button>
    <button class="action-btn" id="f-reunioes"     onclick="setFilter('reunioes', this)">📝 Reuniões</button>
    <button class="action-btn" id="f-projetos"     onclick="setFilter('projetos', this)">📋 Projetos</button>
    <button class="action-btn" id="f-analises"     onclick="setFilter('analises', this)">📊 Análises</button>
    <button class="action-btn" id="f-stakeholders" onclick="setFilter('stakeholders', this)">👤 Stakeholders</button>
    <button class="action-btn" id="f-referencias"  onclick="setFilter('referencias', this)">📚 Referências</button>
  </aside>

  <div class="main">
    <div class="chat-area scrollbar-thin" id="chat">
      <div class="empty-chat" id="empty">
        <div class="icon">🔍</div>
        <h2>Pergunte sobre seu vault</h2>
        <p>Use as ações rápidas ou digite uma pergunta. O agente busca nas suas notas e responde com base no contexto real.</p>
      </div>
    </div>

    <div class="input-area">
      <select class="col-filter" id="col-select">
        <option value="">Todas as coleções</option>
        <option value="reunioes">Reuniões</option>
        <option value="projetos">Projetos</option>
        <option value="analises">Análises</option>
        <option value="stakeholders">Stakeholders</option>
        <option value="referencias">Referências</option>
        <option value="inbox">Inbox</option>
      </select>
      <textarea id="input" placeholder="Pergunte algo sobre seu vault... (Enter para enviar)"
        onkeydown="handleKey(event)" oninput="autoResize(this)" rows="1"></textarea>
      <button class="send-btn" id="send-btn" onclick="sendMessage()">Enviar</button>
    </div>
  </div>
</div>

<script>
  let activeFilter = null;
  let activeRoot   = null;

  // ── Root selector ─────────────────────────────────────────────────────────
  async function loadRoots() {
    try {
      const r = await fetch('/api/roots');
      const data = await r.json();
      const sel = document.getElementById('root-select');
      data.roots.forEach(root => {
        const opt = document.createElement('option');
        opt.value = root;
        opt.textContent = '📁 ' + root;
        sel.appendChild(opt);
      });
    } catch(e) { console.warn('Erro ao carregar raízes', e); }
  }

  function onRootChange() {
    const sel = document.getElementById('root-select');
    activeRoot = sel.value || null;
    const badge = document.getElementById('root-badge');
    badge.textContent = activeRoot ? `isolado em "${activeRoot}"` : '';
  }

  function getRoot() { return activeRoot; }

  function setFilter(col, el) {
    activeFilter = col;
    document.querySelectorAll('.sidebar .action-btn').forEach(b => b.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('col-select').value = col || '';
  }

  function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }

  function appendMsg(role, content, sources) {
    const chat = document.getElementById('chat');
    document.getElementById('empty')?.remove();

    const div = document.createElement('div');
    div.className = `msg ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.textContent = content;
    div.appendChild(bubble);

    if (sources && sources.length) {
      const src = document.createElement('div');
      src.className = 'msg-sources';
      sources.forEach(s => {
        const tag = document.createElement('span');
        tag.className = 'source-tag';
        tag.textContent = s.split('/').pop();
        tag.title = s;
        src.appendChild(tag);
      });
      div.appendChild(src);
    }

    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return div;
  }

  function appendTyping() {
    const chat = document.getElementById('chat');
    const div = document.createElement('div');
    div.className = 'msg agent';
    div.id = 'typing-indicator';
    div.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }

  function removeTyping() {
    document.getElementById('typing-indicator')?.remove();
  }

  async function sendMessage() {
    const input = document.getElementById('input');
    const q = input.value.trim();
    if (!q) return;

    const col = document.getElementById('col-select').value;
    input.value = '';
    input.style.height = 'auto';
    document.getElementById('send-btn').disabled = true;

    appendMsg('user', q);
    appendTyping();

    try {
      const body = { question: q };
      if (col) body.collections = [col];
      if (getRoot()) body.root = getRoot();

      const r = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await r.json();
      removeTyping();
      appendMsg('agent', data.answer, data.sources);
    } catch (e) {
      removeTyping();
      appendMsg('agent', '❌ Erro ao consultar o agente. Verifique os logs.');
    }

    document.getElementById('send-btn').disabled = false;
    input.focus();
  }

  loadRoots();

  // ── Upload ────────────────────────────────────────────────────────────────
  document.getElementById('file-input').addEventListener('change', async e => {
    const file = e.target.files[0];
    if (!file) return;
    e.target.value = '';
    await uploadFile(file);
  });

  document.addEventListener('dragover', e => { e.preventDefault(); document.getElementById('drop-overlay').classList.add('active'); });
  document.addEventListener('dragleave', e => { if (!e.relatedTarget) document.getElementById('drop-overlay').classList.remove('active'); });
  document.addEventListener('drop', async e => {
    e.preventDefault();
    document.getElementById('drop-overlay').classList.remove('active');
    const file = e.dataTransfer.files[0];
    if (file) await uploadFile(file);
  });

  async function uploadFile(file) {
    const isZip = file.name.toLowerCase().endsWith('.zip');
    appendMsg('user', `${isZip ? '🗜' : '📄'} Importando: ${file.name}${getRoot() ? ` → ${getRoot()}/` : ''}`);
    appendTyping();
    document.getElementById('send-btn').disabled = true;
    const form = new FormData();
    form.append('file', file);
    if (getRoot()) form.append('root', getRoot());
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await r.json();
      removeTyping();
      appendMsg('agent', data.answer, data.sources);
    } catch(e) {
      removeTyping();
      appendMsg('agent', '❌ Erro ao importar arquivo.');
    }
    document.getElementById('send-btn').disabled = false;
  }

  async function promptURL() {
    const url = prompt('Cole a URL do artigo ou página:');
    if (!url || !url.startsWith('http')) return;
    appendMsg('user', `🔗 Importando URL: ${url}${getRoot() ? ` → ${getRoot()}/` : ''}`);
    appendTyping();
    document.getElementById('send-btn').disabled = true;
    try {
      const r = await fetch('/api/upload-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, root: getRoot() })
      });
      const data = await r.json();
      removeTyping();
      appendMsg('agent', data.answer, data.sources);
    } catch(e) {
      removeTyping();
      appendMsg('agent', '❌ Erro ao importar URL.');
    }
    document.getElementById('send-btn').disabled = false;
  }

  async function runAction(action) {
    document.getElementById('send-btn').disabled = true;
    const labels = { weekly: '📅 Resumo semanal', insights: '💡 Insights de mercado', 'vault-review': '🔗 Revisar vault' };
    const rootLabel = getRoot() ? ` (${getRoot()})` : '';
    appendMsg('user', labels[action] + rootLabel);
    appendTyping();

    try {
      const r = await fetch(`/api/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: getRoot() }),
      });
      const data = await r.json();
      removeTyping();
      appendMsg('agent', data.answer, data.sources);
    } catch (e) {
      removeTyping();
      appendMsg('agent', '❌ Erro ao executar ação.');
    }
    document.getElementById('send-btn').disabled = false;
  }
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
    return jsonify({"roots": get_root_folders()})


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
    data        = request.get_json()
    question    = data.get("question", "").strip()
    collections = data.get("collections")
    root        = data.get("root") or None
    if not question:
        return jsonify({"error": "question obrigatório"}), 400
    result = ask(question, collections, root=root)
    return jsonify(result)


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
