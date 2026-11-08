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
from agent import ask, weekly_summary, summarize_meeting, vault_review, normalize_tags, purge_vault, ingest_file, ingest_url, ingest_zip, get_vaults, stop_agent, reset_stop, tool_read_note, reindex_vault, rebuild_indices, obsidian_maintenance, generate_output, ingest_email, process_email_payload
from core import vault_fs
from core import settings as app_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key")

# ── HTML da interface ────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mordomo do Conhecimento</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  :root{
    --bg:#0f1117; --surface:#181c27; --surface2:#1e2335; --border:#272d42;
    --accent:#4f7fff; --accent2:#7c5cff; --text:#e2e8f0; --muted:#64748b; --label:#94a3b8;
    --green:#3ecf8e; --amber:#f5a623; --red:#ef4e6b;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
  header .brand{display:flex;align-items:center;gap:10px}
  header h1{font-size:15px;font-weight:600}
  .vault-badge{font-size:11px;color:var(--label);background:var(--surface2);border:1px solid var(--border);padding:3px 9px;border-radius:20px;font-family:'JetBrains Mono',monospace}
  .gear{background:var(--surface);border:1px solid var(--border);color:var(--label);width:36px;height:36px;border-radius:9px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center}
  .gear:hover{background:var(--surface2);color:var(--text)}
  .layout{display:flex;flex:1;overflow:hidden}

  /* ── Sidebar (Assistente) ── */
  .sidebar{width:340px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
  .side-head{padding:12px 14px 8px;font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:var(--muted)}
  .chat{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:10px}
  .chat::-webkit-scrollbar{width:4px}.chat::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
  .chat-empty{color:var(--muted);font-size:12px;text-align:center;margin:auto;padding:20px;line-height:1.6}
  .msg{display:flex;flex-direction:column;gap:4px;max-width:100%}
  .msg.user{align-items:flex-end}
  .bubble{padding:9px 12px;border-radius:11px;font-size:13px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
  .msg.user .bubble{background:var(--accent);color:#fff;border-bottom-right-radius:3px}
  .msg.agent .bubble{background:var(--surface);border:1px solid var(--border);border-bottom-left-radius:3px}
  .sources{display:flex;gap:4px;flex-wrap:wrap}
  .src-tag{font-size:9px;padding:1px 6px;border-radius:4px;background:var(--surface2);border:1px solid var(--border);color:var(--muted);font-family:'JetBrains Mono',monospace}
  .prog-list{padding:7px 11px;background:var(--surface);border:1px solid var(--border);border-radius:11px;display:flex;flex-direction:column;gap:3px}
  .prog-item{font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace;display:flex;gap:6px}
  .prog-item .t-name{color:var(--accent);min-width:84px;flex-shrink:0}
  .typing-wrap{display:flex;gap:5px;padding:9px 12px;background:var(--surface);border:1px solid var(--border);border-radius:11px;width:fit-content}
  .typing-wrap span{width:6px;height:6px;border-radius:50%;background:var(--muted);animation:b 1.2s infinite}
  .typing-wrap span:nth-child(2){animation-delay:.2s}.typing-wrap span:nth-child(3){animation-delay:.4s}
  @keyframes b{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-5px)}}
  .side-actions{display:flex;flex-wrap:wrap;gap:5px;padding:8px 14px;border-top:1px solid var(--border)}
  .chip{background:var(--surface);border:1px solid var(--border);color:var(--label);font-size:11px;padding:5px 9px;border-radius:7px;cursor:pointer;font-family:'Inter',sans-serif}
  .chip:hover{background:var(--surface2);color:var(--text)}
  .input-wrap{border-top:1px solid var(--border);padding:10px 14px;display:flex;gap:7px;align-items:flex-end}
  textarea#input{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:9px;padding:9px 11px;color:var(--text);font-family:'Inter',sans-serif;font-size:13px;resize:none;outline:none;max-height:120px;line-height:1.4}
  textarea#input:focus{border-color:var(--accent)}
  .send-btn{background:var(--accent);border:none;color:#fff;width:38px;height:38px;border-radius:9px;cursor:pointer;font-size:15px;flex-shrink:0}
  .send-btn:disabled{opacity:.5;cursor:not-allowed}
  #stop-btn{display:none;background:var(--red)}

  /* ── Dashboard (3 tiles) ── */
  .main{flex:1;overflow-y:auto;padding:20px;display:grid;grid-template-columns:repeat(3,1fr);gap:16px;align-content:start}
  @media(max-width:1100px){.main{grid-template-columns:1fr}}
  .tile{background:var(--surface);border:1px solid var(--border);border-radius:14px;display:flex;flex-direction:column;min-height:340px;overflow:hidden}
  .tile-head{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
  .tile-ico{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0}
  .tile-ico.email{background:rgba(79,127,255,.15)}
  .tile-ico.teams{background:rgba(124,92,255,.15)}
  .tile-ico.todo{background:rgba(62,207,142,.15)}
  .tile-head h2{font-size:14px;font-weight:600;flex:1}
  .tile-count{font-size:11px;color:var(--muted);background:var(--surface2);border:1px solid var(--border);padding:2px 8px;border-radius:20px;font-family:'JetBrains Mono',monospace}
  .tile-body{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:9px}
  .tile-empty{margin:auto;text-align:center;color:var(--muted);font-size:12px;line-height:1.6;padding:24px 12px}
  .tile-empty .big{font-size:30px;opacity:.5;margin-bottom:8px}
  .soon{display:inline-block;margin-top:10px;font-size:10px;color:var(--amber);border:1px solid rgba(245,166,35,.3);background:rgba(245,166,35,.08);padding:2px 8px;border-radius:20px}
  /* cards de exemplo (estrutura p/ dados futuros do Graph) */
  .card{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:11px 12px;display:flex;flex-direction:column;gap:6px}
  .card .c-top{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--muted)}
  .card .c-title{font-size:13px;font-weight:600;color:var(--text)}
  .card .c-snippet{font-size:12px;color:var(--label);line-height:1.5}
  .card .c-actions{display:flex;gap:6px;margin-top:2px}
  .c-btn{font-size:11px;padding:4px 10px;border-radius:7px;border:1px solid var(--border);cursor:pointer;background:var(--surface);color:var(--label)}
  .c-btn.ok{background:var(--green);color:#06281c;border-color:transparent}
  .c-btn:hover{filter:brightness(1.1)}
  .todo-item{display:flex;align-items:flex-start;gap:9px;font-size:13px;color:var(--text)}
  .todo-item input{margin-top:3px}

  /* ── Modais ── */
  .modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:50}
  .modal-overlay.open{display:flex}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;width:440px;max-width:92vw;max-height:88vh;overflow-y:auto;display:flex;flex-direction:column;gap:14px}
  .modal h3{font-size:15px;font-weight:600}
  .modal .field{display:flex;flex-direction:column;gap:6px}
  .modal label{font-size:12px;color:var(--label);font-weight:500}
  .modal .hint{font-size:11px;color:var(--muted);line-height:1.5}
  .modal select,.modal input[type=text],.modal input[type=time],.modal textarea{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:9px 11px;color:var(--text);font-size:13px;font-family:'Inter',sans-serif;outline:none;resize:vertical}
  .modal select:focus,.modal input:focus,.modal textarea:focus{border-color:var(--accent)}
  .sec{border-top:1px solid var(--border);padding-top:12px;display:flex;flex-direction:column;gap:10px}
  .sec-title{font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)}
  .times{display:flex;flex-wrap:wrap;gap:6px}
  .time-pill{display:flex;align-items:center;gap:6px;background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:3px 6px 3px 11px;font-size:12px;font-family:'JetBrains Mono',monospace}
  .time-pill button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px}
  .time-pill button:hover{color:var(--red)}
  .add-time{display:flex;gap:6px}
  .modal-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:4px}
  .btn-cancel{background:var(--surface2);border:1px solid var(--border);color:var(--label);padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px}
  .btn-ok{background:var(--accent);border:none;color:#fff;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px}
  .btn-sub{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;cursor:pointer;font-size:12px}
  .btn-sub:hover{background:var(--border)}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>🤵 Mordomo do Conhecimento</h1>
    <span class="vault-badge" id="vault-badge">—</span>
  </div>
  <button class="gear" onclick="openSettings()" title="Configurações">⚙️</button>
</header>

<div class="layout">
  <!-- Assistente -->
  <aside class="sidebar">
    <div class="side-head">Assistente</div>
    <div class="chat" id="chat">
      <div class="chat-empty" id="chat-empty">Converse com o agente, importe arquivos/URLs ou gere conteúdo a partir do vault.</div>
    </div>
    <div class="side-actions">
      <button class="chip" onclick="document.getElementById('file-input').click()">📎 Arquivo</button>
      <button class="chip" onclick="promptURL()">🔗 URL</button>
      <input type="file" id="file-input" style="display:none" onchange="if(this.files[0]){uploadFile(this.files[0]);this.value=''}">
    </div>
    <div class="input-wrap">
      <textarea id="input" rows="1" placeholder="Pergunte ao agente…" oninput="autoGrow(this)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMessage()}"></textarea>
      <button class="send-btn" id="send-btn" onclick="sendMessage()">➤</button>
      <button class="send-btn" id="stop-btn" onclick="stopAgent()">■</button>
    </div>
  </aside>

  <!-- Dashboard -->
  <main class="main">
    <section class="tile">
      <div class="tile-head"><div class="tile-ico email">📧</div><h2>Email</h2><span class="tile-count" id="email-count">0</span></div>
      <div class="tile-body" id="email-body">
        <div class="tile-empty"><div class="big">📧</div>Rascunhos que o agente preparar a partir dos seus emails aparecem aqui, prontos para você revisar e liberar o envio.<br><span class="soon">Microsoft Graph — em breve</span></div>
      </div>
    </section>

    <section class="tile">
      <div class="tile-head"><div class="tile-ico teams">💬</div><h2>Chat (Teams)</h2><span class="tile-count" id="chat-count">0</span></div>
      <div class="tile-body" id="chat-body">
        <div class="tile-empty"><div class="big">💬</div>Respostas sugeridas para conversas do Microsoft Teams aparecem aqui como rascunhos, para você aprovar e enviar.<br><span class="soon">Microsoft Graph — em breve</span></div>
      </div>
    </section>

    <section class="tile">
      <div class="tile-head"><div class="tile-ico todo">✅</div><h2>To-dos</h2><span class="tile-count" id="todo-count">0</span></div>
      <div class="tile-body" id="todo-body">
        <div class="tile-empty"><div class="big">✅</div>Tarefas que o agente extrair dos seus emails e conversas aparecem aqui como uma lista de afazeres.<br><span class="soon">Microsoft Graph — em breve</span></div>
      </div>
    </section>
  </main>
</div>


<!-- Modal: Configurações -->
<div class="modal-overlay" id="settings-modal">
  <div class="modal">
    <h3>⚙️ Configurações</h3>

    <div class="field">
      <label>Modelo (LLM)</label>
      <select id="set-model">
        <option value="sonnet">Claude Sonnet — equilíbrio (padrão)</option>
        <option value="opus">Claude Opus — máxima capacidade</option>
        <option value="haiku">Claude Haiku — rápido e econômico</option>
      </select>
      <div class="hint">Usa sua assinatura Claude Pro. Modelos mais fortes consomem mais da sua cota.</div>
    </div>

    <div class="field">
      <label>Coletor — intervalo de verificação (segundos)</label>
      <input type="text" id="set-poll" placeholder="300">
      <div class="hint">De quanto em quanto tempo o coletor de email busca mensagens novas. Padrão 300 (5 min). Mínimo 30.</div>
    </div>

    <div class="field">
      <label>Filtro do coletor — ignorar (um por linha)</label>
      <textarea id="set-skip" rows="4" placeholder="trecho de remetente ou assunto, ex:&#10;newsletter&#10;iqviafan@iqvia.com&#10;promo"></textarea>
      <div class="hint">Emails cujo remetente ou assunto contenham qualquer um destes trechos são ignorados (sem gastar LLM). Vazio = processa tudo. RH, learning, convites e reuniões passam a menos que você os adicione aqui.</div>
    </div>

    <div class="sec">
      <div class="sec-title">Manutenção do Obsidian</div>
      <div class="hint">Nos horários abaixo, roda a manutenção de garantia do vault (sem custo de LLM): <b>correlaciona</b> as notas (links "Notas Relacionadas") e <b>regenera os índices</b> (00-meta/indices). A busca semântica (ChromaDB) já é mantida em dia pelo watcher — isto <b>não</b> rebuilda o ChromaDB. Horários em hora do servidor.</div>
      <div class="field">
        <label>Horários agendados</label>
        <div class="times" id="times"></div>
        <div class="add-time">
          <input type="time" id="new-time" value="03:00">
          <button class="btn-sub" onclick="addTime()">+ Adicionar</button>
        </div>
      </div>
      <button class="btn-sub" onclick="maintenanceNow()">🧹 Rodar manutenção agora</button>
    </div>

    <div class="sec">
      <div class="sec-title">ChromaDB — Avançado</div>
      <div class="hint">O índice semântico é mantido em dia automaticamente pelo watcher. Reconstrua do zero só em manutenção (ex.: troca de modelo de embeddings) ou recuperação.</div>
      <button class="btn-sub" onclick="reindexFull()">🔄 Reindexar ChromaDB (completo)</button>
    </div>

    <div class="sec">
      <div class="sec-title">Organização (00-meta/config/instructions.md)</div>
      <div class="hint">Reorganiza as notas nas pastas conforme a taxonomia do seu instructions.md. O ChromaDB roteia pela categoria mais próxima e o LLM só desempata. <b>Simule primeiro</b> para revisar os movimentos antes de aplicar (aplicar move as notas e reescreve os wiki links).</div>
      <button class="btn-sub" onclick="reorgSim()">🗂 Organizar vault — simular</button>
      <button class="btn-sub" onclick="reorgApply()">✅ Organizar vault — aplicar</button>
    </div>

    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeSettings()">Cancelar</button>
      <button class="btn-ok" onclick="saveSettings()">Salvar</button>
    </div>
  </div>
</div>

<script>
  var VAULT = '';
  var _times = [];

  function fmtTime(){var d=new Date();return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0');}
  function getRoot(){return VAULT;}
  function autoGrow(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px';}
  function setBusy(on){document.getElementById('send-btn').style.display=on?'none':'flex';document.getElementById('stop-btn').style.display=on?'flex':'none';document.getElementById('input').disabled=on;}

  async function loadVault(){
    try{
      var r=await fetch('/api/vaults');var d=await r.json();
      VAULT=(d.vaults&&d.vaults[0])||'';
      document.getElementById('vault-badge').textContent=VAULT||'(sem vault)';
    }catch(e){document.getElementById('vault-badge').textContent='(erro)';}
  }

  // ── Chat ──
  function appendMsg(role,content,sources){
    var chat=document.getElementById('chat');var emp=document.getElementById('chat-empty');if(emp)emp.remove();
    var div=document.createElement('div');div.className='msg '+role;
    var b=document.createElement('div');b.className='bubble';b.textContent=content;div.appendChild(b);
    if(sources&&sources.length){var s=document.createElement('div');s.className='sources';sources.forEach(function(x){var t=document.createElement('span');t.className='src-tag';t.textContent=x.split('/').pop();t.title=x;s.appendChild(t);});div.appendChild(s);}
    chat.appendChild(div);chat.scrollTop=chat.scrollHeight;return div;
  }
  function appendTyping(){var c=document.getElementById('chat');var e=document.getElementById('chat-empty');if(e)e.remove();var d=document.createElement('div');d.className='msg agent';d.id='typing';d.innerHTML='<div class="typing-wrap"><span></span><span></span><span></span></div>';c.appendChild(d);c.scrollTop=c.scrollHeight;}
  function removeTyping(){var e=document.getElementById('typing');if(e)e.remove();}
  function createProgPanel(){var c=document.getElementById('chat');var e=document.getElementById('chat-empty');if(e)e.remove();var d=document.createElement('div');d.className='msg agent';d.id='prog-panel';var l=document.createElement('div');l.className='prog-list';d.appendChild(l);c.appendChild(d);c.scrollTop=c.scrollHeight;return l;}
  function addProgItem(l,tool,detail){var i=document.createElement('div');i.className='prog-item';i.innerHTML='<span class="t-name">'+tool+'</span><span class="t-detail">'+(detail||'')+'</span>';l.appendChild(i);document.getElementById('chat').scrollTop=99999;}
  function removeProgPanel(){var e=document.getElementById('prog-panel');if(e)e.remove();}

  async function sendMessage(){
    var input=document.getElementById('input');var q=input.value.trim();if(!q)return;
    input.value='';input.style.height='auto';setBusy(true);appendMsg('user',q);
    var body={question:q};if(getRoot())body.vault=getRoot();
    var progList=null;
    try{
      var resp=await fetch('/api/ask/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!resp.ok)throw new Error('http '+resp.status);
      progList=createProgPanel();
      var reader=resp.body.getReader();var dec=new TextDecoder();var buf='';
      while(true){var ch=await reader.read();if(ch.done)break;buf+=dec.decode(ch.value,{stream:true});var lines=buf.split('\\n');buf=lines.pop();
        for(var i=0;i<lines.length;i++){var line=lines[i];if(!line.startsWith('data: '))continue;
          try{var evt=JSON.parse(line.slice(6));
            if(evt.type==='progress'){addProgItem(progList,evt.tool,evt.detail);}
            else if(evt.type==='done'){removeProgPanel();appendMsg('agent',evt.answer,evt.sources);}
            else if(evt.type==='error'){removeProgPanel();appendMsg('agent','❌ '+evt.message);}
          }catch(e){}}}
    }catch(e){removeProgPanel();removeTyping();appendMsg('agent','❌ Erro de conexão com o agente.');}
    setBusy(false);input.focus();
  }

  async function uploadFile(file){
    appendMsg('user',(file.name.endsWith('.zip')?'🗜 ':'📄 ')+'Importando: '+file.name);appendTyping();setBusy(true);
    var form=new FormData();form.append('file',file);if(getRoot())form.append('vault',getRoot());
    try{var r=await fetch('/api/upload',{method:'POST',body:form});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro ao importar.');}
    setBusy(false);
  }
  async function promptURL(){
    var url=prompt('Cole a URL do artigo ou página:');if(!url||!url.startsWith('http'))return;
    appendMsg('user','🔗 '+url);appendTyping();setBusy(true);
    try{var r=await fetch('/api/upload-url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url,vault:getRoot()})});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro ao importar URL.');}
    setBusy(false);
  }
  async function runAction(a){appendMsg('user','📅 Resumo semanal');appendTyping();setBusy(true);
    try{var r=await fetch('/api/'+a,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vault:getRoot()})});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro ao executar ação.');}setBusy(false);}
  async function normalizeTags(){appendMsg('user','🏷️ Normalizar tags');appendTyping();setBusy(true);
    try{var r=await fetch('/api/normalize-tags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vault:getRoot()})});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro ao normalizar tags.');}setBusy(false);}
  async function reindex(){appendMsg('user','🔄 Reindexar ChromaDB');appendTyping();setBusy(true);
    try{var r=await fetch('/api/reindex',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vault:getRoot()})});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro ao reindexar.');}setBusy(false);}
  async function correlate(){appendMsg('user','🔗 Correlacionar notas');appendTyping();setBusy(true);
    try{var r=await fetch('/api/correlate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vault:getRoot()})});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro ao correlacionar.');}setBusy(false);}
  async function stopAgent(){try{await fetch('/api/stop',{method:'POST'});}catch(e){}setBusy(false);removeTyping();removeProgPanel();appendMsg('agent','⛔ Operação interrompida.');}

  // ── Settings ──
  function renderTimes(){
    var w=document.getElementById('times');w.innerHTML='';
    if(!_times.length){w.innerHTML='<span class="hint">Nenhum horário agendado.</span>';}
    _times.forEach(function(t,idx){var p=document.createElement('span');p.className='time-pill';p.innerHTML=t+' <button onclick="rmTime('+idx+')">×</button>';w.appendChild(p);});
  }
  function addTime(){var t=document.getElementById('new-time').value;if(t&&_times.indexOf(t)<0){_times.push(t);_times.sort();renderTimes();}}
  function rmTime(i){_times.splice(i,1);renderTimes();}
  async function openSettings(){
    try{var r=await fetch('/api/settings');var d=await r.json();document.getElementById('set-model').value=d.model||'sonnet';document.getElementById('set-poll').value=d.poll_seconds||300;document.getElementById('set-skip').value=(d.skip_patterns||[]).join('\\n');_times=(d.reindex_times||[]).slice();}
    catch(e){_times=[];}
    renderTimes();document.getElementById('settings-modal').classList.add('open');
  }
  function closeSettings(){document.getElementById('settings-modal').classList.remove('open');}
  async function saveSettings(){
    var skip=document.getElementById('set-skip').value.split('\\n').map(function(s){return s.trim();}).filter(function(s){return s;});
    var body={model:document.getElementById('set-model').value,reindex_times:_times,poll_seconds:parseInt(document.getElementById('set-poll').value)||300,skip_patterns:skip};
    try{await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(e){}
    closeSettings();appendMsg('agent','⚙️ Configurações salvas. Modelo: '+body.model+(body.reindex_times.length?(' · reindex: '+body.reindex_times.join(', ')):''));
  }
  async function maintenanceNow(){
    closeSettings();appendMsg('user','🧹 Manutenção do Obsidian (correlacionar + índices)');appendTyping();setBusy(true);
    try{var r=await fetch('/api/maintenance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vault:getRoot()})});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro na manutenção.');}setBusy(false);
  }
  async function reindexFull(){closeSettings();reindex();}
  async function _reorg(apply){
    closeSettings();
    appendMsg('user', apply ? '✅ Organizar vault (aplicar)' : '🗂 Organizar vault (simular)');
    appendTyping(); setBusy(true);
    try{var r=await fetch('/api/reorg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vault:getRoot(),apply:apply})});var d=await r.json();removeTyping();appendMsg('agent',d.answer,d.sources);}
    catch(e){removeTyping();appendMsg('agent','❌ Erro ao organizar.');}
    setBusy(false);
  }
  function reorgSim(){ _reorg(false); }
  function reorgApply(){ if(confirm('Aplicar a organização? Isso move as notas e reescreve os wiki links. Recomendo simular antes.')) _reorg(true); }

  document.addEventListener('keydown',function(e){if(e.key==='Escape'){closeSettings();}});
  loadVault();
</script>
</body>
</html>
"""


# ── Rotas ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/vaults", methods=["GET"])
def api_vaults():
    vaults = get_vaults()
    log.info(f"api_vaults: {vaults}")
    return jsonify({"vaults": vaults})


@app.route("/api/vault-stats", methods=["GET"])
def api_vault_stats():
    """Diagnóstico: contagem de notas no disco por vault."""
    vault = request.args.get("vault") or None
    try:
        vaults = [vault] if vault else vault_fs.get_vaults()
        out = {}
        for v in vaults:
            ids = vault_fs.list_notes(v)
            out[v] = {"active_notes": len(ids), "active_note_ids": ids}
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Endpoint síncrono (compatibilidade). Retorna JSON quando o agente termina."""
    data        = request.get_json()
    question    = data.get("question", "").strip()
    collections = data.get("collections")
    vault       = data.get("vault") or None
    if not question:
        return jsonify({"error": "question obrigatório"}), 400
    result = ask(question, collections, vault=vault)
    return jsonify(result)


@app.route("/api/ask/stream", methods=["POST"])
def api_ask_stream():
    """Endpoint SSE: emite eventos de progresso enquanto o agente trabalha."""
    data        = request.get_json()
    question    = (data or {}).get("question", "").strip()
    collections = (data or {}).get("collections")
    vault       = (data or {}).get("vault") or None

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
            detail = f"vault={args.get('vault', '')}"
        else:
            detail = preview[:120] if preview else ""

        q.put({"type": "progress", "tool": tool, "detail": detail})

    def _run_agent():
        try:
            result = ask(question, collections, vault=vault, on_progress=_on_progress)
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
    vault    = request.form.get("vault") or None
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
            result = agent_ask(instruction, vault=vault, on_progress=_on_progress)
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


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_agent()
    return jsonify({"status": "stopped"})


@app.route("/api/normalize-tags", methods=["POST"])
def api_normalize_tags():
    data  = request.get_json(silent=True) or {}
    vault = data.get("vault") or None
    return jsonify(normalize_tags(vault=vault))


@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    """Reconstrói os índices ChromaDB do zero (re-embeda todas as notas)."""
    data  = request.get_json(silent=True) or {}
    vault = data.get("vault") or None
    return jsonify(reindex_vault(vault=vault))


@app.route("/api/correlate", methods=["POST"])
def api_correlate():
    """Analisa correlações via ChromaDB e adiciona ## Notas Relacionadas nas notas."""
    data  = request.get_json(silent=True) or {}
    vault = data.get("vault") or None
    return jsonify(vault_review(vault=vault))


@app.route("/api/weekly", methods=["POST"])
def api_weekly():
    data = request.get_json(silent=True) or {}
    return jsonify(weekly_summary(vault=data.get("vault") or None))



@app.route("/api/purge", methods=["POST"])
def api_purge():
    data        = request.get_json(silent=True) or {}
    keep_prefix = data.get("keep_prefix") or None
    vault       = data.get("vault") or None
    return jsonify(purge_vault(keep_prefix=keep_prefix, db=vault))


@app.route("/api/meeting", methods=["POST"])
def api_meeting():
    data    = request.get_json()
    note_id = data.get("note_id", "").strip()
    vault   = data.get("vault") or None
    if not note_id:
        return jsonify({"error": "note_id obrigatório"}), 400
    return jsonify(summarize_meeting(note_id, vault=vault))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "arquivo obrigatório"}), 400
    f     = request.files["file"]
    vault = request.form.get("vault") or None
    content = f.read()
    if not content:
        return jsonify({"error": "arquivo vazio"}), 400
    if f.filename.lower().endswith(".zip"):
        return jsonify(ingest_zip(content, vault=vault))
    return jsonify(ingest_file(f.filename, content, vault=vault))


@app.route("/api/upload-url", methods=["POST"])
def api_upload_url():
    data = request.get_json()
    url   = (data or {}).get("url", "").strip()
    vault = (data or {}).get("vault") or None
    if not url:
        return jsonify({"error": "url obrigatória"}), 400
    result = ingest_url(url, vault=vault)
    return jsonify(result)


@app.route("/api/notes/tree")
def api_notes_tree():
    """Retorna a árvore de arquivos do vault como JSON (lida do disco)."""
    vault = request.args.get("vault") or None
    try:
        note_ids = vault_fs.list_notes(vault) if vault else (
            vault_fs.list_notes(vault_fs.get_vaults()[0]) if vault_fs.get_vaults() else []
        )

        def _build(node, path=""):
            items = []
            for name, children in sorted(node.items()):
                full = (path + "/" + name).lstrip("/")
                if children is None:
                    items.append({"name": name, "type": "file", "id": full})
                else:
                    items.append({"name": name, "type": "folder", "children": _build(children, full)})
            return items

        tree: dict = {}
        for nid in note_ids:
            parts = nid.split("/")
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = None

        return jsonify({"tree": _build(tree)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/note/read")
def api_note_read():
    """Lê o conteúdo de uma nota pelo seu ID."""
    note_id = request.args.get("note_id", "").strip()
    vault   = request.args.get("vault") or None
    if not note_id:
        return jsonify({"error": "note_id obrigatório"}), 400
    content = tool_read_note(note_id, db=vault)
    return jsonify({"note_id": note_id, "content": content})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data     = request.get_json(silent=True) or {}
    topic    = data.get("topic", "").strip()
    template = data.get("template", "relatorio")
    vault    = data.get("vault") or None
    if not topic:
        return jsonify({"error": "topic obrigatório"}), 400
    return jsonify(generate_output(topic, template, vault=vault))


# Chave para os endpoints máquina-a-máquina (coletor de email/Teams). Como o
# agente fica exposto na internet, estes endpoints exigem a chave configurada.
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")


def _machine_auth():
    """(ok, resposta_de_erro). Endpoints máquina-a-máquina exigem AGENT_API_KEY."""
    if not AGENT_API_KEY:
        return False, (jsonify({"error": "AGENT_API_KEY não configurada no servidor"}), 503)
    if request.headers.get("X-Api-Key") != AGENT_API_KEY:
        return False, (jsonify({"error": "unauthorized"}), 401)
    return True, None


@app.route("/api/processed", methods=["GET"])
def api_processed():
    """IDs já processados de uma fonte (o coletor usa p/ mandar só os novos)."""
    ok, err = _machine_auth()
    if not ok:
        return err
    from core import state
    source = request.args.get("source", "outlook")
    since  = request.args.get("since") or None
    return jsonify({"ids": state.processed_ids(source, since=since)})


@app.route("/api/collector-config", methods=["GET"])
def api_collector_config():
    """Config que o coletor lê (intervalo editável nas Settings do agente)."""
    ok, err = _machine_auth()
    if not ok:
        return err
    s = app_settings.get_settings()
    return jsonify({"poll_seconds": s.get("poll_seconds", 300),
                    "backfill_days": s.get("backfill_days", 0),
                    "skip_patterns": s.get("skip_patterns", [])})


@app.route("/api/ingest-email", methods=["POST"])
def api_ingest_email():
    ok, err = _machine_auth()
    if not ok:
        return err
    data = request.get_json(silent=True) or {}
    if not (data.get("subject") or data.get("body")):
        return jsonify({"error": "subject/body obrigatórios"}), 400
    # Apenas enfileira (rápido) — o worker destila em background, sem timeout de proxy.
    res = ingest_email(
        subject=data.get("subject", ""), sender=data.get("from", ""),
        date=data.get("date", ""), body=data.get("body", ""),
        msg_id=data.get("msg_id", ""), web_link=data.get("web_link", ""),
        vault=data.get("vault") or None,
    )
    return jsonify(res), 202


@app.route("/api/queue", methods=["GET"])
def api_queue():
    ok, err = _machine_auth()
    if not ok:
        return err
    from core import state
    return jsonify({"pending": state.queue_len()})


@app.route("/api/rebuild-indices", methods=["POST"])
def api_rebuild_indices():
    """Atualiza SÓ os índices MOC do Obsidian (não mexe no ChromaDB)."""
    data  = request.get_json(silent=True) or {}
    vault = data.get("vault") or None
    return jsonify(rebuild_indices(vault=vault))


@app.route("/api/maintenance", methods=["POST"])
def api_maintenance():
    """Manutenção do Obsidian: correlaciona notas + regenera índices (zero LLM, sem rebuild do Chroma)."""
    data  = request.get_json(silent=True) or {}
    vault = data.get("vault") or None
    return jsonify(obsidian_maintenance(vault=vault))


@app.route("/api/reorg", methods=["POST"])
def api_reorg():
    """Organiza as pastas conforme 00-meta/config/instructions.md (Chroma roteia, LLM desempata).
    apply=false → simula; apply=true → move e reescreve os wiki links."""
    import organizer
    data   = request.get_json(silent=True) or {}
    vault  = data.get("vault") or None
    apply_ = bool(data.get("apply"))
    from agent import _vault as _resolve_vault
    return jsonify(organizer.reorg(_resolve_vault(vault), apply=apply_))


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        patch = request.get_json(silent=True) or {}
        return jsonify(app_settings.save_settings(patch))
    return jsonify(app_settings.get_settings())


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ── Scheduler de reindexação do ChromaDB ─────────────────────────────────────
def _indices_scheduler():
    """Atualiza os índices do Obsidian (00-meta/indices) nos horários HH:MM configurados.
    NÃO rebuilda o ChromaDB — o watcher mantém o índice em dia continuamente."""
    import time as _t
    last_fired = None
    while True:
        try:
            now = _t.strftime("%H:%M")
            if now != last_fired and now in set(app_settings.get_settings().get("reindex_times", [])):
                last_fired = now
                log.info(f"[indices-scheduler] manutenção do Obsidian disparada às {now}")
                try:
                    obsidian_maintenance()
                except Exception as e:
                    log.error(f"[indices-scheduler] erro: {e}")
        except Exception as e:
            log.warning(f"[indices-scheduler] erro: {e}")
        _t.sleep(20)


def _start_scheduler():
    t = threading.Thread(target=_indices_scheduler, daemon=True, name="indices-scheduler")
    t.start()
    log.info("[indices-scheduler] iniciado")


# ── Worker de ingestão assíncrona (destila a fila de emails/mensagens) ───────────
import agent as _agent_mod

WORKER_PAUSE        = int(os.environ.get("AGENT_WORKER_PAUSE", "1800"))  # pausa no limite do LLM
WORKER_MAX_ATTEMPTS = int(os.environ.get("AGENT_WORKER_MAX_ATTEMPTS", "3"))


def _ingest_worker():
    import time as _t
    from core import state
    while True:
        try:
            item = state.queue_peek()
            if not item:
                _t.sleep(3)
                continue
            p = item["payload"]
            try:
                r = process_email_payload(**p)
            except _agent_mod.sdk_llm.LLMRateLimited:
                log.warning(f"[worker] limite de uso do LLM — pausando {WORKER_PAUSE // 60} min (item fica na fila)")
                _t.sleep(WORKER_PAUSE)
                continue
            except Exception as e:
                state.queue_bump(item["id"])
                if item["attempts"] + 1 >= WORKER_MAX_ATTEMPTS:
                    state.mark_processed("outlook", item["item_id"],
                                         item_date=p.get("date", ""), status="skip")
                    state.queue_delete(item["id"])
                    log.error(f"[worker] desistindo de {item['item_id']} após {WORKER_MAX_ATTEMPTS} tentativas: {e}")
                else:
                    log.warning(f"[worker] erro (tentativa {item['attempts'] + 1}): {e}")
                    _t.sleep(5)
                continue
            path = r.get("path")
            state.mark_processed("outlook", item["item_id"], item_date=p.get("date", ""),
                                 note_path=path or "", status="ok" if path else "skip")
            state.queue_delete(item["id"])
            log.info(f"[worker] processado {item['item_id']} — {path or '(sem nota)'}  | fila: {state.queue_len()}")
        except Exception as e:
            log.error(f"[worker] erro no loop: {e}")
            _t.sleep(5)


def _start_worker():
    t = threading.Thread(target=_ingest_worker, daemon=True, name="ingest-worker")
    t.start()
    log.info("[ingest-worker] iniciado")


_start_scheduler()
_start_worker()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("AGENT_PORT", 8090)), debug=False)
