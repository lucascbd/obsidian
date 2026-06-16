"""
app.py — Interface web do agente. Acessível via browser na rede local.
"""

import os
import logging
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from agent import ask, weekly_summary, market_insights, summarize_meeting

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
</style>
</head>
<body>

<header>
  <h1>🧠 Obsidian MI Agent</h1>
  <span>INSIGHTS & ANALYTICS LATAM</span>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-label">Ações rápidas</div>
    <button class="action-btn" onclick="runAction('weekly')">📅 Resumo semanal</button>
    <button class="action-btn" onclick="runAction('insights')">💡 Insights de mercado</button>
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

  async function runAction(action) {
    document.getElementById('send-btn').disabled = true;
    const labels = { weekly: '📅 Resumo semanal', insights: '💡 Insights de mercado' };
    appendMsg('user', labels[action]);
    appendTyping();

    try {
      const r = await fetch(`/api/${action}`, { method: 'POST' });
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


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data        = request.get_json()
    question    = data.get("question", "").strip()
    collections = data.get("collections")  # None = todas
    if not question:
        return jsonify({"error": "question obrigatório"}), 400
    result = ask(question, collections)
    return jsonify(result)


@app.route("/api/weekly", methods=["POST"])
def api_weekly():
    return jsonify(weekly_summary())


@app.route("/api/insights", methods=["POST"])
def api_insights():
    return jsonify(market_insights())


@app.route("/api/meeting", methods=["POST"])
def api_meeting():
    data    = request.get_json()
    note_id = data.get("note_id", "").strip()
    if not note_id:
        return jsonify({"error": "note_id obrigatório"}), 400
    return jsonify(summarize_meeting(note_id))


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
