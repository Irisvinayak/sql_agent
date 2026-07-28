import logging
import os
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import src.config as config
from api.routes import health, query, voice

# Attach a handler directly so logs always appear in the uvicorn terminal
# regardless of whether uvicorn already configured the root logger.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(levelname)s  [%(name)s]  %(message)s"))
for _name in ("query", "voice", "startup"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.INFO)
    if not _lg.handlers:
        _lg.addHandler(_handler)
    _lg.propagate = False

# Loud, unmissable startup banner: which embedding folder is actually active.
# This exact mismatch (server silently running against the wrong
# EMBEDDING_DIR after a restart) caused multiple rounds of "why doesn't my
# fix show up" debugging — this print makes it visible in every server log
# instead of requiring a fresh diagnostic session each time.
_startup_log = logging.getLogger("startup")
_qa_index_path = f"{config.EMBEDDING_DIR}/qa_index.faiss"
_qa_index_exists = os.path.exists(_qa_index_path)
_startup_log.info("=" * 70)
_startup_log.info("EMBEDDING_DIR = %s", config.EMBEDDING_DIR)
_startup_log.info("qa_index.faiss present: %s (%s)", _qa_index_exists, _qa_index_path)
if not _qa_index_exists:
    _startup_log.warning(
        "No qa_index.faiss here — the direct-match / strong-grounding tiers "
        "are DISABLED for this run. Every query will go through full "
        "retrieval + LLM generation, even exact repeats of known questions."
    )
_startup_log.info("=" * 70)


app = FastAPI(
    title="SQL Query Generator API",
    description="Natural language → SQL pipeline with Oracle DB execution",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(query.router, prefix="/api")
app.include_router(voice.router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["api", "src"],   # only watch api/ and src/ — not embedding_building/
    )
