# Generative Agents: Interactive Simulacra of Human Behavior

<p align="center" width="100%"><img src="cover.png" alt="Smallville" style="width: 80%; min-width: 300px; display: block; margin: auto;"></p>

This repository accompanies our research paper, [“Generative Agents: Interactive Simulacra of Human Behavior”](https://arxiv.org/abs/2304.03442). It contains the Smallville simulation, its generative-agent backend, and the browser environment used to inspect and replay simulations.

## Modern architecture

The modernized fork has two local processes:

1. The Django environment server in `environment/frontend_server` serves the map, simulator UI, demo/replay pages, and AJAX endpoints on `http://localhost:8000`.
2. The Reverie simulation server in `reverie/backend_server` runs the agents and writes simulation state into the frontend server’s storage directories.

The backend sends chat completions through the OpenCode Go OpenAI-compatible gateway (`https://opencode.ai/zen/go/v1`) using `deepseek-v4-flash`. Embeddings are requested from the local LM Studio server at `192.168.2.4:1234`. `base.html` polls `/get_token_usage/` every two seconds and displays live LLM and embedding usage on every page.

## Setup

### 0. Configure the API key

Create `~/.opencode-go.key` containing your OpenCode Go key on one line. The key file is outside this repository and must never be committed. Alternatively set `OPENCODE_GO_API_KEY`; the loader strips surrounding whitespace and fails clearly if neither source is available.

### 1. Install Python dependencies

Python 3.12 is the supported baseline. From the repository root:

```bash
uv venv --python 3.12 .venv
uv pip install -r requirements.txt --python .venv/bin/python
```

With standard `venv`/`pip`: `python3.12 -m venv .venv`, `source .venv/bin/activate`, then `pip install -r requirements.txt`. Runtime dependencies are intentionally small: NumPy and Requests for Reverie, plus Django 5.2 LTS, Gunicorn, and Pillow for the frontend.

## Configuration reference

Runtime configuration lives in the local, ignored `reverie/backend_server/utils.py` file. The modern defaults are:

| Constant | Value |
| --- | --- |
| `llm_model` | `deepseek-v4-flash` |
| `llm_max_tokens` | `64000` |
| `openai_base_url` | `https://opencode.ai/zen/go/v1` |
| `embedding_base_url` | `http://192.168.2.4:1234/v1` |
| `embedding_model` | `text-embedding-nomic-embed-text-v1.5` |
| `token_usage_file` | `../../environment/frontend_server/temp_storage/token_usage.json` |

If your checkout does not already have the ignored runtime file, obtain the modernized `utils.py` from the project’s runtime checkout before starting Reverie; do not commit an API key or machine-specific replacement.

## Run a simulation

Start Django in one terminal:

```bash
cd environment/frontend_server
python manage.py runserver
```

Start Reverie in a second:

```bash
cd reverie/backend_server
python reverie.py
```

When prompted for the fork, enter `base_the_ville_isabella_maria_klaus`. Give the new simulation any name, then open [http://localhost:8000/simulator_home](http://localhost:8000/simulator_home). At the `Enter option:` prompt, run steps such as `run 100`. Each step advances the simulated world by ten seconds. Use `fin` to save and exit, or `exit` to leave without saving.

The token monitor is visible on `/simulator_home`, landing, demo, replay, and other pages extending `base.html`; it reports zeroes until the backend writes its first usage snapshot.

## Demo and replay

With Django running, visit `http://localhost:8000/replay/<simulation-name>/<starting-time-step>/` for a saved replay, or `http://localhost:8000/demo/<simulation-name>/<starting-time-step>/<speed>/` for a compressed demo. Speed is normally `1`–`6`. A bundled example is [July1_the_ville_isabella_maria_klaus-step-3-20](http://localhost:8000/replay/July1_the_ville_isabella_maria_klaus-step-3-20/1/). Uncompressed saves are under `environment/frontend_server/storage`; compressed demo assets are under `compressed_storage`.

## Troubleshooting

- **LLM/API errors:** confirm `~/.opencode-go.key` or `OPENCODE_GO_API_KEY`, verify the gateway URL/model in `utils.py`, and restart Reverie after changes. Empty thinking-model completions are retried automatically.
- **Embedding errors:** verify LM Studio is running and serving `text-embedding-nomic-embed-text-v1.5` on `192.168.2.4:1234`; check reachability from the Reverie host.
- **Parse or empty-completion retries:** transient gateway failures and malformed/empty responses are retried. If retries are exhausted, inspect the backend traceback and reduce the run size while diagnosing the response.
- **Token monitor stays at zero:** expected before a successful call. Confirm `temp_storage/token_usage.json` is writable and both servers use the same checkout/storage tree.
- **Simulator home says the backend is not running:** start `reverie.py` first, then reload `/simulator_home`; the frontend reads current-simulation and step markers from `temp_storage`.

## FAQ

**Do I need the OpenAI Python SDK?** No. The modernized client uses Requests against OpenCode Go.

**Can I run without LM Studio?** Not for a normal agent run; memory retrieval depends on embeddings.

**Can I change the model or endpoints?** Yes. Change the corresponding constants in the ignored `reverie/backend_server/utils.py`, keep secrets outside Git, and restart Reverie.

## Authors and citation

**Authors:** Joon Sung Park, Joseph C. O’Brien, Carrie J. Cai, Meredith Ringel Morris, Percy Liang, Michael S. Bernstein

Please cite the paper if you use the code or data:

```bibtex
@inproceedings{Park2023GenerativeAgents,
  author = {Park, Joon Sung and O'Brien, Joseph C. and Cai, Carrie J. and Morris, Meredith Ringel and Liang, Percy and Bernstein, Michael S.},
  title = {Generative Agents: Interactive Simulacra of Human Behavior}, year = {2023},
  publisher = {Association for Computing Machinery}, address = {New York, NY, USA},
  booktitle = {In the 36th Annual ACM Symposium on User Interface Software and Technology (UIST '23)},
  keywords = {Human-AI interaction, agents, generative AI, large language models}, location = {San Francisco, CA, USA}, series = {UIST '23}
}
```

## Acknowledgements

We thank PixyMoon, LimeZu, and ぴぽ for the game assets, and Lindsay Popowski, Philip Guo, Michael Terry, and the CASBS community for their insights and support. Smallville locations are inspired by places Joon Sung Park frequented as an undergraduate and graduate student.
# CI trigger test
