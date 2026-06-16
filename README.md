# Obsidian MI Agent

RAG agent para análise do vault Obsidian via CouchDB + ChromaDB + OpenRouter.

## Deploy via Portainer (Git)

### 1. Stacks → Add stack → Repository

| Campo | Valor |
|-------|-------|
| Repository URL | `https://github.com/lucascbd/obsidian` |
| Branch | `main` |
| Compose path | `docker-compose.yml` |

### 2. Adicionar variáveis de ambiente

Na seção **Environment variables**, adicione:

| Variável | Valor |
|----------|-------|
| `COUCHDB_USER` | `admin` |
| `COUCHDB_PASSWORD` | senha forte |
| `COUCHDB_DB` | `obsidian-vault` |
| `OPENROUTER_API_KEY` | sua chave do OpenRouter |
| `OPENROUTER_MODEL` | `nousresearch/hermes-3-llama-3.1-70b` |
| `FLASK_SECRET_KEY` | string aleatória |

### 3. Deploy the stack

Clique em **Deploy the stack** — o Portainer vai buildar e subir tudo.

---

## Inicializar CouchDB (primeira vez)

```bash
curl -X POST http://admin:SENHA@<IP-PROXMOX>:5984/_cluster_setup \
  -H "Content-Type: application/json" \
  -d '{"action":"enable_single_node","bind_address":"0.0.0.0","username":"admin","password":"SENHA"}'
```

---

## Configurar LiveSync no Obsidian

1. Instala o plugin **Self-hosted LiveSync**
2. Settings → Remote Database:
   - URI: `http://<IP-PROXMOX>:5984`
   - Database name: `obsidian-vault`
   - Username / Password: conforme variáveis acima
3. Test → Apply → ativa sync contínuo

---

## Acessar o agente

```
http://<IP-PROXMOX>:8080
```

## Atualizar após mudanças no código

Portainer → Stacks → obsidian-agent → **Pull and redeploy**
