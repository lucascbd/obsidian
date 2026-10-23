"""core — biblioteca compartilhada entre o agente e o watcher.

Armazenamento do vault em disco (vault_fs), estado de sincronização em SQLite
(state), utilitários de texto/frontmatter/chunking (text) e indexação no
ChromaDB (indexer). Fonte única de verdade para ambos os serviços.
"""
