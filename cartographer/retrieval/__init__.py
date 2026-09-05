from .base import Context, Issue, RepoRef, Retriever, Snippet
from .embedding_retriever import EmbeddingRetriever
from .graph_retriever import GraphRetriever

MODES: dict[str, type] = {
    GraphRetriever.mode: GraphRetriever,
    EmbeddingRetriever.mode: EmbeddingRetriever,
}


def build_retriever(mode: str, **kwargs) -> Retriever:
    try:
        cls = MODES[mode]
    except KeyError:
        raise ValueError(
            f"unknown retrieval mode {mode!r}; expected one of {sorted(MODES)}"
        ) from None
    return cls(**kwargs)


__all__ = [
    "Context",
    "EmbeddingRetriever",
    "GraphRetriever",
    "Issue",
    "MODES",
    "RepoRef",
    "Retriever",
    "Snippet",
    "build_retriever",
]
