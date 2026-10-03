"""Ingestion pipeline (PoC): discover → parse → chunk → enrich → ACL → embed → index.

Usage:  python -m poc.ingest [--recreate]

The PoC reads curated Markdown files with metadata front-matter. In production the same steps run
against SharePoint Online / Azure Storage (Document Intelligence for PDF/DOCX/XLSX parsing) with
ACLs read from the source system.
"""
import argparse
import pathlib
import re

import yaml

from poc import config

DOCS_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "docs"
REQUIRED_META = ["document_id", "title", "department", "language", "owner", "classification", "allowed_groups"]


def parse_markdown(path: pathlib.Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path.name}: missing metadata front-matter")
    meta = yaml.safe_load(m.group(1))
    missing = [k for k in REQUIRED_META if not meta.get(k)]
    if missing:
        raise ValueError(f"{path.name}: missing metadata {missing} — document excluded (fail closed)")
    return meta, m.group(2)


def chunk_by_section(meta: dict, body: str) -> list[dict]:
    """Structure-aware chunking: one chunk per '##' section, prefixed with the document title."""
    chunks = []
    parts = re.split(r"^## +", body, flags=re.M)
    for i, part in enumerate(parts[1:], start=1):
        heading, _, content = part.partition("\n")
        content = content.strip()
        if not content:
            continue
        lang = meta["language"]
        text = f"{meta['title']} — {heading.strip()}\n{content}"
        chunks.append({
            "chunk_id": f"{meta['document_id']}-c{i:02d}",
            "document_id": meta["document_id"],
            "title": meta["title"],
            "section": heading.strip(),
            "content": text,
            "content_ar": text if lang == "ar" else "",
            "language": lang,
            "department": meta["department"],
            "owner": meta["owner"],
            "classification": meta["classification"],
            "version": str(meta.get("version", "")),
            "effective_date": str(meta.get("effective_date", "")),
            "is_current": bool(meta.get("is_current", True)),
            "source_url": meta.get("source_url", ""),
            "allowed_groups": list(meta["allowed_groups"]),   # ← permission metadata travels with every chunk
        })
    return chunks


def embed(texts: list[str]) -> list[list[float]]:
    client = config.openai_client()
    out = []
    for i in range(0, len(texts), 16):
        resp = client.embeddings.create(model=config.embed_deployment(), input=texts[i:i + 16])
        out.extend(d.embedding for d in resp.data)
    return out


def build_index():
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration, SearchableField, SearchField, SearchFieldDataType, SearchIndex,
        SemanticConfiguration, SemanticField, SemanticPrioritizedFields, SemanticSearch, SimpleField,
        VectorSearch, VectorSearchProfile,
    )
    S = SearchFieldDataType
    fields = [
        SimpleField(name="chunk_id", type=S.String, key=True, filterable=True),
        SimpleField(name="document_id", type=S.String, filterable=True),
        SearchableField(name="title", type=S.String),
        SimpleField(name="section", type=S.String),
        SearchableField(name="content", type=S.String, analyzer_name="standard.lucene"),
        SearchableField(name="content_ar", type=S.String, analyzer_name="ar.microsoft"),
        SearchField(name="content_vector", type=S.Collection(S.Single), searchable=True,
                    vector_search_dimensions=config.EMBED_DIM, vector_search_profile_name="vprofile"),
        SimpleField(name="language", type=S.String, filterable=True, facetable=True),
        SimpleField(name="department", type=S.String, filterable=True, facetable=True),
        SimpleField(name="owner", type=S.String, filterable=True),
        SimpleField(name="classification", type=S.String, filterable=True),
        SimpleField(name="version", type=S.String),
        SimpleField(name="effective_date", type=S.String, filterable=True, sortable=True),
        SimpleField(name="is_current", type=S.Boolean, filterable=True),
        SimpleField(name="source_url", type=S.String),
        SimpleField(name="allowed_groups", type=S.Collection(S.String), filterable=True),  # security field
    ]
    vector = VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
                          profiles=[VectorSearchProfile(name="vprofile", algorithm_configuration_name="hnsw")])
    semantic = SemanticSearch(configurations=[SemanticConfiguration(
        name="sem", prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"), content_fields=[SemanticField(field_name="content")]))])
    return SearchIndex(name=config.SEARCH_INDEX, fields=fields, vector_search=vector, semantic_search=semantic)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recreate", action="store_true", help="delete and recreate the index")
    args = ap.parse_args()

    chunks = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        meta, body = parse_markdown(path)
        c = chunk_by_section(meta, body)
        chunks.extend(c)
        print(f"  parsed {meta['document_id']:<16} lang={meta['language']} groups={meta['allowed_groups']} chunks={len(c)}")

    idx_client = config.search_index_client()
    if args.recreate:
        try:
            idx_client.delete_index(config.SEARCH_INDEX)
            print(f"  deleted index {config.SEARCH_INDEX}")
        except Exception:
            pass
    idx_client.create_or_update_index(build_index())
    print(f"  index {config.SEARCH_INDEX} ready")

    vectors = embed([c["content"] for c in chunks])
    for c, v in zip(chunks, vectors):
        c["content_vector"] = v
    result = config.search_client().upload_documents(documents=chunks)
    ok = sum(1 for r in result if r.succeeded)
    print(f"  uploaded {ok}/{len(chunks)} chunks")


if __name__ == "__main__":
    main()
