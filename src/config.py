EMBED_MODEL = "BAAI/bge-large-en"

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

TOP_K_TABLES = 3
TOP_K_COLUMNS = 5

MINIMAL_MULTIPART_NUM_PREDICT = 300

# Ollama settings
OLLAMA_URL = "http://3.109.51.228/OllamaProxy/api/generate"
# Set the active Ollama-served model here. Change only this value to swap models
# without modifying src/sql_generator.py.
OLLAMA_MODEL = "hf.co/defog/sqlcoder-7b-2:Q5_K_M"

MODEL_PROFILES = {
    "gpt-oss:120b-cloud": {
        "prompt_style": "rules",
        "dialect_hint": "Oracle",
        "supports_full_ruleset": True,
        "temperature": 0.0,
        "num_predict": 512,
    },
        
    "qwen2.5:7b": {
        "prompt_style": "rules",
        "dialect_hint": "Oracle",
        "supports_full_ruleset": True,
        "temperature": 0.0,
        "num_predict": 512,
    },
    "llama3.1:latest": {
        "prompt_style": "minimal",
        "dialect_hint": "Oracle",
        "supports_full_ruleset": False,
        "temperature": 0.0,
        "num_predict": 128,
    },
    "hf.co/defog/sqlcoder-7b-2:Q5_K_M": {
        "prompt_style": "minimal",
        "dialect_hint": "Oracle",
        "supports_full_ruleset": False,
        "temperature": 0.0,
        "num_predict": 128,
    },
    "hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q5_K_M": {
        "prompt_style": "minimal",
        "dialect_hint": "Oracle",
        "supports_full_ruleset": False,
        "temperature": 0.0,
        "num_predict": 128,
    },
}

# Oracle DB connection settings — fill these in before running
DB_HOST     = "3.6.209.141"
DB_PORT     = 1521
DB_SERVICE  = "XE"          # or SID, e.g. "xe"
DB_USER     = "SOUTHINDIANBANK"
DB_PASSWORD = "southindianbank1123"
DB_MAX_ROWS = 100              # max rows to fetch per query

# Sarvam AI — Speech-to-Text + Translation
# Get your key at: https://dashboard.sarvam.ai
SARVAM_API_KEY ="sk_dan14ij9_Nq6ivwxYVv5tsmVCRRYS5MoE"