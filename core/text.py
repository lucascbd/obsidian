"""text.py — utilitários de texto do vault (sem I/O nem rede).

Frontmatter + extração de entidades, chunking por seção, wiki links e
normalização de tags. Fonte única compartilhada por agente e watcher.
"""

import re
import unicodedata


# ── Frontmatter ───────────────────────────────────────────────────────────────

def parse_frontmatter(content: str) -> dict:
    """Extrai campos YAML do frontmatter para metadata do ChromaDB.
    Inclui title/date/source, tags e entidades (people/companies/projects/related)."""
    meta: dict[str, str] = {}
    if not content.startswith("---"):
        return meta
    end = content.find("\n---", 3)
    if end == -1:
        return meta
    fm = content[3:end]

    for key, pat in [("title",  r"^title:\s*(.+)$"),
                     ("date",   r"^date:\s*(.+)$"),
                     ("source", r"^source:\s*(.+)$")]:
        m = re.search(pat, fm, re.MULTILINE)
        if m:
            meta[key] = m.group(1).strip().strip("\"'")

    m = re.search(r"^tags:(.*?)(?=\n\S|\Z)", fm, re.MULTILINE | re.DOTALL)
    if m:
        block = m.group(1)
        tags = re.findall(r"[\-\*]\s*(\S+)", block)
        if not tags:
            tags = [t.strip().strip("\"'") for t in block.strip().strip("[]").split(",") if t.strip()]
        if tags:
            meta["tags"] = ",".join(tags[:10])

    for entity_key in ("people", "companies", "projects", "related"):
        m = re.search(rf"^{entity_key}:(.*?)(?=\n\S|\Z)", fm, re.MULTILINE | re.DOTALL)
        if m:
            block = m.group(1)
            items = re.findall(r"[\-\*]\s*(.+)", block)
            if not items:
                items = [t.strip().strip("\"'") for t in block.strip().strip("[]").split(",") if t.strip()]
            if items:
                meta[entity_key] = ",".join(i.strip() for i in items[:20])
    return meta


# ── Chunking por seção ──────────────────────────────────────────────────────────

def chunk_by_sections(content: str, max_words: int = 350) -> list[str]:
    """Chunking por seções Markdown, preservando contexto hierárquico.
    Cada chunk inclui frontmatter + breadcrumb de headings pais + conteúdo da seção."""
    fm = ""
    body = content
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm = content[:end + 4].strip()
            body = content[end + 4:].strip()

    segments = re.split(r'(?m)(?=^#{1,3} )', body)
    chunks: list[str] = []
    h_stack: list[str] = []

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        first_line = seg.split('\n')[0]
        hm = re.match(r'^(#{1,3}) (.+)', first_line)
        if hm:
            level = len(hm.group(1))
            h_stack = h_stack[:level - 1]
            h_stack.append(hm.group(2).strip())

        ctx_parts = []
        if fm:
            ctx_parts.append(fm)
        if len(h_stack) > 1:
            ctx_parts.append("Contexto: " + " › ".join(h_stack[:-1]))
        ctx = "\n\n".join(ctx_parts)
        full_seg = (ctx + "\n\n" + seg).strip() if ctx else seg

        words = full_seg.split()
        if len(words) <= max_words:
            if full_seg:
                chunks.append(full_seg)
        else:
            heading_line = seg.split('\n')[0]
            body_words = '\n'.join(seg.split('\n')[1:]).split()
            step = max(50, max_words - len((ctx + "\n\n" + heading_line).split()) - 30)
            for i in range(0, max(1, len(body_words)), step):
                sub = " ".join(body_words[i:i + step])
                sub_chunk = ((ctx + "\n\n" + heading_line + "\n" + sub) if ctx else (heading_line + "\n" + sub)).strip()
                if sub_chunk:
                    chunks.append(sub_chunk)

    if not chunks:
        words = content.split()
        size, overlap, i = 400, 50, 0
        while i < len(words):
            chunks.append(" ".join(words[i:i + size]))
            i += size - overlap

    return [c for c in chunks if c.strip()]


# ── Wiki links ────────────────────────────────────────────────────────────────

def extract_wiki_links(content: str) -> list[str]:
    """Extrai note_ids de wiki links [[caminho/nota|alias]] ou [[caminho/nota]]."""
    raw = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content)
    seen, links = set(), []
    for r in raw:
        r = r.strip()
        if not r or r.startswith("http"):
            continue
        if not r.endswith(".md"):
            r += ".md"
        r = r.lower()
        if r not in seen:
            seen.add(r)
            links.append(r)
    return links


# ── Normalização de tags ────────────────────────────────────────────────────────

def normalize_tag(tag: str) -> str:
    """Lowercase, sem acentos, espaços/underscores → hífens, remove chars especiais."""
    tag = unicodedata.normalize("NFD", tag)
    tag = "".join(c for c in tag if unicodedata.category(c) != "Mn")
    tag = tag.lower().strip()
    tag = re.sub(r"[\s_]+", "-", tag)
    tag = re.sub(r"[^a-z0-9\-]", "", tag)
    tag = re.sub(r"-+", "-", tag).strip("-")
    return tag


def fix_codeblock_frontmatter(content: str) -> str:
    """Converte ```yaml\\n...\\n``` no início da nota para frontmatter --- correto."""
    m = re.match(r"^```(?:yaml)?\n(.*?)\n```\s*\n?", content, re.DOTALL)
    if m:
        yaml_body = m.group(1)
        rest = content[m.end():]
        return f"---\n{yaml_body}\n---\n{rest.lstrip()}"
    return content


def fix_tags_in_frontmatter(fm_body: str) -> str:
    """Normaliza o bloco de tags dentro do frontmatter (lista YAML ou array inline)."""
    def _replace(m):
        block = m.group(0)
        inline = re.match(r'^tags:\s*\[(.+)\]\s*$', block.strip(), re.DOTALL)
        if inline:
            raw_tags = re.findall(r'"([^"]+)"|\'([^\']+)\'|([^,\[\]\s]+)', inline.group(1))
            tags = [normalize_tag(next(p for p in t if p)) for t in raw_tags]
            tags = [t for t in tags if t]
            return "tags:\n" + "\n".join(f"  - {t}" for t in tags)

        lines = block.split("\n")
        out = []
        for line in lines:
            item = re.match(r"^(\s*[-*]\s*)(.+)$", line)
            if item:
                raw = item.group(2).strip().strip("\"'")
                norm = normalize_tag(raw)
                if norm:
                    out.append(f"{item.group(1)}{norm}")
            else:
                out.append(line)
        return "\n".join(out)

    return re.sub(r"^tags:.*?(?=\n\w|\Z)", _replace, fm_body, flags=re.MULTILINE | re.DOTALL)
