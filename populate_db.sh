#!/bin/bash
# Exit on any error
set -e

echo "=========================================================="
echo "  CRDC Knowledge Graph — Multi-Source Ingestion Pipeline   "
echo "=========================================================="
echo "Starting data ingestion with fuzzy entity resolution..."

# Ensure PYTHONPATH includes the current directory
export PYTHONPATH=.

echo -e "\n🔥 [1/3] Step 1: Ingesting CPMG 2025 Insect Control Tables..."
venv/bin/python -m scripts.seed_graph

echo -e "\n🌿 [2/3] Step 2: Ingesting Diseases, Beneficials, and Defoliants..."
venv/bin/python -m scripts.seed_graph_docs

echo -e "\n🌾 [3/3] Step 3: Ingesting ACPM Varieties, Weeds, and Crop Stages..."
venv/bin/python -m scripts.seed_acpm

echo -e "\n=========================================================="
echo "  Success! Your Neo4j Knowledge Graph is now populated.   "
echo "=========================================================="
