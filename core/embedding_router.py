"""
Semantic embedding router using FastEmbed (ONNX/onnxruntime, no PyTorch).

Fast path: embeds the query with bge-small-en-v1.5 (33 MB), finds the
nearest example phrase via HNSWlib cosine search.  Latency: ~1 ms after
warm-up vs 200-800 ms for the LLM router.

Falls back to the LLM router when max cosine similarity < CONFIDENCE_THRESHOLD,
ensuring the LLM handles ambiguous queries.

Model auto-downloads to ~/.cache/fastembed/ on first use (~33 MB).
"""

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.68  # below this → fall back to LLM router
_MODEL_NAME = "BAAI/bge-small-en-v1.5"   # 33 MB, 384-dim, fast CPU

# Example phrases per module — embed these at startup to build the index.
# More phrases = better coverage; aim for 10–15 per module.
_MODULE_EXAMPLES: dict[str, list[str]] = {
    "farming": [
        "show my farm weather", "is it safe to spray tomorrow",
        "mandi price pomegranate today", "rainfall last 30 days",
        "soil data for my farm", "crop disease pomegranate",
        "spray log this week", "7 day forecast barloni",
        "sugarcane pest management", "onion price solapur",
        "add spray log copper fungicide", "show plots",
        "soil pH and nitrogen", "kharif season advisory",
        "what is the best spray for aphids",
        "NDVI crop health barloni", "show crop health index",
        "soil moisture trend", "is my crop healthy",
        "show farm NDVI", "vegetation index for my plot",
        "rainfall history barloni", "spray safe check",
        "add my north field 3 acres", "pomegranate harvest forecast",
    ],
    "finance": [
        "log expense 500 groceries", "log income 20000",
        "monthly summary", "show budget status",
        "spending breakdown", "add goal tractor",
        "how much did I spend", "financial goals",
        "income vs expenses", "budget for groceries",
        "total expenses this month", "savings",
        "how much have I saved", "show all transactions",
        "spent 200 on medicine", "earned 15000 from harvest",
        "do I have enough for tractor loan", "finance report",
    ],
    "health": [
        "log BP 120/80", "blood pressure today",
        "steps today 8000", "log weight 72",
        "show health trends", "sleep 7 hours",
        "blood sugar fasting", "log health reading",
        "BP trend last 2 weeks", "health summary",
        "my health history", "daily steps goal",
        "how is my health", "log blood sugar 110",
        "I slept 7 hours last night", "I walked 5000 steps",
        "show BP history", "weight trend",
    ],
    "diary": [
        "write diary from my photos", "show diary draft",
        "weekly summary", "what did I do this week",
        "approve diary", "list diary drafts",
        "week in review", "diary for 2025-08-03",
        "auto draft from queries", "diary history",
        "show this week's diary entry",
        "write my diary", "create diary entry from photos",
        "show last week journal", "approve my diary draft",
        "write journal from pictures", "diary from uploads",
    ],
    "search": [
        "search for farming subsidies", "latest news agriculture",
        "today news india", "recent msp announcement",
        "web search drip irrigation", "news about wheat prices",
        "search pomegranate export", "latest farming news",
        "what is the current policy on", "find information about",
        "look up PM-KISAN scheme", "what is urea subsidy",
        "recent news about onion prices", "search for drip irrigation cost",
        "who is agriculture minister", "latest government scheme for farmers",
    ],
    "system": [
        "system status", "show system load",
        "cpu usage", "memory usage",
        "disk space", "uptime",
        "server load", "show processes",
        "guardian status", "security schedule",
        "how busy is the system", "RAM and CPU",
        "show security alerts", "when did guardian last run",
        "is the server idle", "load average",
    ],
    "code": [
        "analyze this project", "analyze the codebase",
        "how many lines of code", "show complex functions",
        "security scan the project", "what does this codebase do",
        "explain the farming module", "show code complexity",
        "scan for vulnerabilities in code", "analyze dashboard directory",
        "how big is this project", "show file breakdown",
        "code analysis", "review the code",
        "analyze security module", "what files are in this project",
    ],
}


class EmbeddingRouter:
    """Semantic router: embeds the query and finds the nearest example phrase.

    The index is built in a background thread so import/startup is non-blocking;
    until it is ready, route() returns None and the caller uses the LLM router.
    """

    def __init__(self):
        """Start building the embedding index in a background daemon thread."""
        self._model = None
        self._index = None
        self._labels: list[str] = []   # module name for each indexed vector
        self._lock   = threading.Lock()
        self._ready  = False
        # Build index in background thread — doesn't block server startup
        threading.Thread(target=self._build, daemon=True, name="embed-router").start()

    def _build(self):
        """Embed all module example phrases and build the HNSW cosine index (background)."""
        try:
            import hnswlib
            from fastembed import TextEmbedding

            log.info("embedding router: loading %s …", _MODEL_NAME)
            model = TextEmbedding(model_name=_MODEL_NAME)

            phrases: list[str] = []
            labels:  list[str] = []
            for module, examples in _MODULE_EXAMPLES.items():
                for ex in examples:
                    phrases.append(ex)
                    labels.append(module)

            vecs = np.array(list(model.embed(phrases)), dtype=np.float32)
            # L2-normalise for cosine similarity via inner product
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs  = vecs / np.clip(norms, 1e-10, None)

            dim = vecs.shape[1]
            idx = hnswlib.Index(space="ip", dim=dim)   # inner product == cosine on unit vecs
            idx.init_index(max_elements=len(phrases), ef_construction=200, M=16)
            idx.add_items(vecs, list(range(len(phrases))))
            idx.set_ef(50)

            with self._lock:
                self._model  = model
                self._index  = idx
                self._labels = labels
                self._ready  = True

            log.info("embedding router: ready (%d phrases, %d dims)", len(phrases), dim)

        except Exception as e:
            log.warning("embedding router build failed (%s) — LLM router will be used", e)

    def route(self, query: str) -> Optional[tuple[str, float]]:
        """
        Return (module_name, confidence) if confident, else None.
        None → caller should fall back to LLM router.
        """
        if not self._ready:
            return None
        try:
            with self._lock:
                model = self._model
                index = self._index
                labels = list(self._labels)

            vec = np.array(list(model.embed([query])), dtype=np.float32)[0]
            vec = vec / max(float(np.linalg.norm(vec)), 1e-10)

            ids, dists = index.knn_query(vec.reshape(1, -1), k=3)
            best_id   = ids[0][0]
            best_sim  = float(dists[0][0])  # inner product (cosine on unit vecs)

            module = labels[best_id]
            log.debug("embed-route: q=%r → %s (%.3f)", query[:60], module, best_sim)

            if best_sim >= CONFIDENCE_THRESHOLD:
                return module, best_sim
            return None

        except Exception as e:
            log.debug("embed-route error: %s", e)
            return None


# Module-level singleton — created once when first imported
_router: Optional[EmbeddingRouter] = None
_init_lock = threading.Lock()


def get_router() -> EmbeddingRouter:
    """Return the process-wide EmbeddingRouter singleton (created on first call)."""
    global _router
    if _router is None:
        with _init_lock:
            if _router is None:
                _router = EmbeddingRouter()
    return _router


def fast_route(query: str) -> Optional[str]:
    """
    Public API: returns module name string if confident, else None.
    Thread-safe.  Returns in ~1 ms after warm-up.
    """
    result = get_router().route(query)
    return result[0] if result else None
