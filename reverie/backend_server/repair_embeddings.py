#!/usr/bin/env python3
"""Repair a persona's corrupted embeddings.json by re-embedding every node
from nodes.json via the LM Studio embedding server (192.168.2.4:1234)."""
import json
import os
import sys
import requests

EMB_URL = "http://192.168.2.4:1234/v1/embeddings"
EMB_MODEL = "text-embedding-nomic-embed-text-v1.5"


def embed(text):
    resp = requests.post(EMB_URL, json={"model": EMB_MODEL, "input": text},
                         timeout=60)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def repair(persona_dir):
    mem_dir = os.path.join(persona_dir, "bootstrap_memory", "associative_memory")
    nodes_path = os.path.join(mem_dir, "nodes.json")
    emb_path = os.path.join(mem_dir, "embeddings.json")
    nodes = json.load(open(nodes_path))
    print(f"{os.path.basename(persona_dir)}: {len(nodes)} nodes")
    embeddings = {}
    for count in range(len(nodes)):
        node_id = f"node_{count + 1}"
        nd = nodes[node_id]
        key = nd.get("embedding_key")
        if not key:
            continue
        if nd.get("type") == "thought":
            text = nd.get("description") or nd.get("subject", "")
        else:
            text = f"{nd.get('subject','')} {nd.get('predicate','')} {nd.get('object','')}".strip()
        if not text:
            embeddings[key] = [0.0] * 768
            continue
        try:
            embeddings[key] = embed(text)
        except Exception as e:
            print(f"  embed fail {node_id}: {e}")
            embeddings[key] = [0.0] * 768
    tmp = emb_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(embeddings, f)
    os.replace(tmp, emb_path)
    print(f"  wrote {len(embeddings)} embeddings -> {emb_path}")


if __name__ == "__main__":
    repair(sys.argv[1])
