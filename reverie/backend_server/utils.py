"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: utils.py
Description: Configuration for the generative agents simulation.

Modernized (hermes-gadget fork): LLM calls route through the OpenCode Go
gateway (OpenAI-compatible, https://opencode.ai/zen/go/v1) instead of the
legacy OpenAI API. The API key is read from ~/.opencode-go.key (or the
OPENCODE_GO_API_KEY environment variable). Embeddings come from the local
LM Studio embedding server (AI box at 192.168.2.4).
"""
import os


def load_api_key():
    """Load the OpenCode Go API key from ~/.opencode-go.key or the env.

    Never store the key in this repo -- the key file is outside the
    repository tree on purpose.
    """
    key_file = os.path.expanduser("~/.opencode-go.key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            key = f.read().strip()
        if key:
            return key
    key = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    if key:
        return key
    raise RuntimeError(
        "No OpenCode Go API key found. Put it in ~/.opencode-go.key or "
        "set the OPENCODE_GO_API_KEY environment variable."
    )


# ---------------------------------------------------------------------------
# OpenCode Go (LLM gateway) configuration
# ---------------------------------------------------------------------------
openai_api_key = load_api_key()          # kept name for compatibility
key_owner = "Ben"

# OpenAI-compatible gateway endpoint + model.
# deepseek-v4-flash is used for EVERY path in the simulation, with its
# thinking mode fully enabled and a very generous token budget (64k) -- this
# pipeline was designed for small 2023 models; modern reasoning models get
# room to think properly. The on-page monitor shows the real token burn.
openai_base_url = "https://opencode.ai/zen/go/v1"
llm_model = "deepseek-v4-flash"
llm_max_tokens = 64000
# Hard wall-clock deadline for a single LLM call attempt (seconds).
# Guards against gateway responses that trickle forever (read timeouts
# reset per chunk). The retry loop gets a fresh attempt afterwards.
llm_hard_timeout = 300

# ---------------------------------------------------------------------------
# Local embedding server (LM Studio on the AI box).
# Override with EMBEDDING_BASE_URL where the AI box isn't directly reachable
# (e.g. the VPS uses the reverse SSH tunnel: http://127.0.0.1:1234/v1).
# ---------------------------------------------------------------------------
embedding_base_url = os.environ.get(
    "EMBEDDING_BASE_URL", "http://192.168.2.4:1234/v1")
embedding_model = "text-embedding-nomic-embed-text-v1.5"

# ---------------------------------------------------------------------------
# Simulation paths (unchanged from upstream)
# ---------------------------------------------------------------------------
maze_assets_loc = "../../environment/frontend_server/static_dirs/assets"
env_matrix = f"{maze_assets_loc}/the_ville/matrix"
env_visuals = f"{maze_assets_loc}/the_ville/visuals"

fs_storage = "../../environment/frontend_server/storage"
fs_temp_storage = "../../environment/frontend_server/temp_storage"

# Live token-usage snapshot, written by the backend on every LLM call and
# served to the Django frontend (/get_token_usage) for the on-page monitor.
# token_usage.db is the cumulative, queryable store (one row per call).
token_usage_file = f"{fs_temp_storage}/token_usage.json"
token_usage_db = f"{fs_temp_storage}/token_usage.db"

collision_block_id = "32125"

# Verbose
debug = True
