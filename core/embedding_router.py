"""
Semantic embedding router using FastEmbed (ONNX/onnxruntime, no PyTorch).

Fast path: embeds the query with bge-small-en-v1.5 (33 MB), finds the
nearest example phrase via HNSWlib cosine search.  Latency: ~1 ms after
warm-up vs 200-800 ms for the LLM router.

Falls back to the LLM router when max cosine similarity < CONFIDENCE_THRESHOLD,
ensuring the LLM handles ambiguous queries.

Model auto-downloads to the HuggingFace cache (~/.cache/huggingface) on first
use (~33 MB on the wire; ~75 MB unpacked).
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
        "what is the weather today", "weather today", "what's the weather",
        "how is the weather", "will it rain tomorrow", "is it going to rain",
        "is it raining now", "how hot is it today", "temperature today",
        "is it sunny", "weather forecast", "tomorrow's weather",
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
        "should I water my crops today", "do I need to irrigate",
        "when should I sow", "best time to harvest onions",
        "leaf curl treatment", "yellow leaves on my plants",
        "white spots on pomegranate leaves", "humidity and wind today",
        "fertilizer recommendation for my soil", "what crop suits this season",
        "how much rain did we get this week", "spray schedule for this week",
        "is the soil too dry", "pest attack on sugarcane",
        # Indian English phrasings
        "today rain will come or not", "is it ok to spray today",
        "how much rain has fallen", "my pomegranate is having some disease",
        "which fertilizer is good for my crop", "when should I do harvesting",
        "how is the weather today", "crop is healthy or not",
        "today temperature is how much",
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
        "how much money do I have left", "what's my account balance",
        "I paid 1200 for diesel", "record a purchase of 800",
        "track my expenses", "where is my money going",
        "set a budget for fuel", "am I within my budget",
        "show me last month's spending", "how much did I earn this month",
        "loan repayment status", "EMI due this month",
        "what's my net worth", "did I save enough this month",
        "paid 500 for electricity", "pay 1000 for the bill",
        "spent 300 on diesel", "paid the electricity bill",
        "spent 500 on seeds", "bought fertilizer for 800",
        # Indian English phrasings
        "how much I spent today", "today how much expense",
        "what is my balance now", "I spent 500 rupees on diesel",
        "this month how much I saved", "I want to see my expenses",
        "two lakh goal for tractor", "I paid 1000 rupees for the fertilizer bill",
        "give me my finance summary",
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
        "record my blood pressure as 130 over 90", "my weight is 68 kg today",
        "how many steps did I take", "log my fasting sugar",
        "did I sleep well last night", "track my heart rate",
        "show my fitness summary", "what's my average blood pressure",
        "am I getting enough sleep", "my sugar reading is 95",
        "log 8 glasses of water", "weekly health report",
        # Indian English phrasings
        "my BP is how much", "today I walked how many steps",
        "I am having sugar problem", "my sugar reading is 110",
        "note my weight as 70 kg", "I slept only 5 hours",
        "show my BP since one week", "how is my health now",
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
        "make today's journal entry", "did I write my diary yet",
        "show me last week's diary", "recap my week",
        "generate my photo diary", "finalize this week's diary",
        "review my diary drafts", "journal about today",
        "my week in pictures", "summarize what I did this week",
        # Indian English phrasings
        "what all I did this week", "kindly write today's diary",
        "show me this week's summary", "did I write my diary or not",
        "make my photo diary", "this week what all happened",
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
        "google the latest msp news", "search online for fertilizer subsidy",
        "what's in the news today", "find recent articles about farming",
        "look it up on the web", "search the internet for",
        "what's the latest update on", "any news about the monsoon",
        "find out about the crop insurance scheme", "latest headlines today",
        "what's happening in the world", "current affairs update",
        # Indian English phrasings
        "kindly search about MSP", "search and tell me",
        "find out about the PM kisan scheme", "what all schemes are there for farmers",
        "do one search for fertilizer subsidy", "any update on monsoon news",
        "tell me the latest news please",
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
        "how much disk space is left", "is the system under load",
        "what processes are running", "is now a good time for heavy tasks",
        "when is the next idle window", "show the load heatmap",
        "are there any anomalies", "vulnerability scan results",
        "cpu temperature", "is the machine free right now",
        "show the background task schedule", "how much RAM is used",
        # Indian English phrasings
        "is the system free now", "system is busy or not",
        "is it ok to run heavy task now", "kindly show system status",
        "memory is how much used", "any problem in the system",
    ],
    "general": [
        "hello", "hi there", "good morning", "thanks", "thank you",
        "who are you", "what can you do", "what can you do for me",
        "what can you help me with", "how are you", "tell me a joke",
        "what is your name", "help", "what are your features",
        "how do you work", "nice to meet you",
        "good evening", "good night", "what's up",
        "can you help me", "tell me about yourself",
        "introduce yourself", "what are you capable of",
        "appreciate it", "thanks so much", "hey there",
        "are you there", "what should I call you",
        # Indian English phrasings
        "namaste", "good morning ji", "thank you so much",
        "kindly help me", "what all you can do",
        "tell me about you", "what is your good name", "how are you doing",
    ],
    "todo": [
        "show todo list", "show my todos", "list my todos",
        "what are my tasks", "what's on my todo list", "todo list",
        "my task list", "pending tasks", "what do I need to do",
        "things to do", "show my to-do list", "open tasks",
        "add a todo", "add a task", "add to my todo list",
        "remind me to do something", "new todo", "create a task",
        "mark a todo done", "complete a todo", "finish a task",
        "tick off a todo", "mark task as done",
        "delete a todo", "remove a task", "drop a todo",
        "what's pending on my list", "anything due on my list",
        "add a reminder", "cross off a task",
        "what should I work on next", "show my checklist",
        "what's left to do", "what tasks are open",
        "mark it as complete", "i finished a task",
        "take this off my list", "what's outstanding on my list",
        # Imperative "add/remind" phrasings with domain-ish objects — anchor these
        # to todo so a noun like "fertilizer"/"loan"/"doctor" doesn't pull the whole
        # query into farming/finance/health. The verb is the real signal here.
        "add todo buy fertilizer", "remind me to pay the electricity bill",
        "remind me to buy a phone charger", "remind me to call the bank",
        "add a task to repair the pump", "add todo book a doctor appointment",
        "remind me to water the plants tomorrow", "add todo submit the loan papers",
        # Indian English phrasings (neutral objects — keep domain nouns out)
        "what all is pending", "show my pending works",
        "add one todo", "kindly add a task to my list",
        "remind me to do one thing", "what all I have to do",
        "note down one task", "add this to my list",
        "mark that work as done",
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
        "do a code review", "check the code quality",
        "find bugs in the code", "how maintainable is this code",
        "run a static analysis", "show me the largest source files",
        "explain what this script does", "scan the repository",
        "code health report", "list the modules in this project",
        "how many functions are there", "find code smells",
        # Indian English phrasings
        "kindly review my code", "what all files are there in the project",
        "check my code quality", "any bug in the code",
        "explain this module to me", "how many lines are there in the code",
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
            # hnswlib's "ip" space returns a DISTANCE of (1 - inner_product), not
            # the similarity itself. On unit vectors that's (1 - cosine), so convert
            # back before thresholding — otherwise good matches look far away.
            best_sim  = 1.0 - float(dists[0][0])

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


def warmup(timeout: float = 300.0) -> bool:
    """Build the embedding index now and BLOCK until it's ready.

    The index normally builds in a background daemon thread — fine for a
    long-running server, but useless in a one-shot script: the process exits and
    kills the thread mid-download, so the model never finishes downloading. Call
    this from setup/warm-up scripts so the ~33 MB download actually completes.

    Returns True if the router became ready within ``timeout`` seconds.
    """
    import time
    router = get_router()                       # kicks off the background build
    deadline = time.monotonic() + timeout
    while not router._ready and time.monotonic() < deadline:
        time.sleep(0.5)
    return router._ready
