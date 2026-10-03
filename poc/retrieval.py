"""Permission-aware hybrid retrieval.

The security filter is built by server code from the user's groups and applied INSIDE the search query.
The model never builds, sees or changes it. Unauthorized chunks are never returned.
"""
from poc import config
from poc.identity import User, build_security_filter
from poc.telemetry import span

SELECT = ["chunk_id", "document_id", "title", "section", "content", "language", "department",
          "version", "effective_date", "source_url"]


def embed_query(text: str) -> list[float]:
    resp = config.openai_client().embeddings.create(model=config.embed_deployment(), input=[text])
    return resp.data[0].embedding


def search_policies(query: str, user: User, top: int | None = None) -> list[dict]:
    from azure.search.documents.models import VectorizedQuery

    top = top or config.TOP_K
    flt = build_security_filter(user.groups)          # fail closed if groups are missing/invalid
    with span("retrieval.search", {"user": user.pseudonym, "top": top}) as sp:
        vq = VectorizedQuery(vector=embed_query(query), k_nearest_neighbors=20, fields="content_vector")
        kwargs = dict(search_text=query, vector_queries=[vq], filter=flt, select=SELECT, top=top,
                      search_fields=["title", "content", "content_ar"])
        if config.SEMANTIC_RANKER:
            kwargs.update(query_type="semantic", semantic_configuration_name="sem")
        results = []
        for r in config.search_client().search(**kwargs):
            rerank = r.get("@search.reranker_score")
            if config.SEMANTIC_RANKER and rerank is not None and rerank < config.MIN_RERANKER_SCORE:
                continue
            item = {k: r.get(k) for k in SELECT}
            item["score"] = r.get("@search.score")
            item["reranker_score"] = rerank
            results.append(item)
        sp.set_attribute("retrieved_doc_ids", ",".join(sorted({x["document_id"] for x in results})))
        sp.set_attribute("result_count", len(results))
    return results
