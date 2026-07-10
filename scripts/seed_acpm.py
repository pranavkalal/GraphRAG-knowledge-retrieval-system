"""
Pipeline to extract structural data from the 2025 Australian Cotton Production Manual.

Usage: python -m scripts.seed_acpm
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from app.infrastructure.gemini_client import GeminiClient
from app.infrastructure.neo4j_client import Neo4jClient
from app.infrastructure.graph_repository import GraphRepository
from app.services.extraction import ExtractionService

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Note: These line ranges are approximate and based on standard chapter lengths.
# For production, we would chunk the document dynamically based on headers.
ACPM_PATH = Path("data/Parsed/OpenDataLoader/Markdown/2025 Australian Cotton Production Manual_interactive_sml.md")

async def extract_and_seed_acpm(
    extraction_svc: ExtractionService,
    repo: GraphRepository,
) -> None:
    
    if not ACPM_PATH.exists():
        logger.error(f"File not found: {ACPM_PATH}")
        return

    with open(ACPM_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # The entire document is ~9000 lines. 
    # We will grab a large chunk covering "Variety selection" and "Managing pests in-crop" / "Weeds"
    # To avoid token limits and save time, we will extract from a subset of the document.
    
    # Example ranges (these would need to be tuned based on exact content)
    # Let's just pass the first 3000 lines for now to find Varieties and Weeds.
    # In a real run, we'd split by chapter.
    chunk_text = "".join(lines[:3000])

    logger.info("Extracting Varieties...")
    try:
        variety_res = await extraction_svc.extract_varieties(chunk_text)
        for v in variety_res.varieties:
            await repo.merge_variety(v.name, v.company, v.crop_type, source="ACPM 2025")
            for r in v.suited_regions:
                await repo.merge_suited_to(v.name, r)
            for t in v.traits:
                await repo.merge_has_trait(v.name, t.name, t.description)
        logger.info(f"Merged {len(variety_res.varieties)} varieties.")
    except Exception as e:
        logger.error(f"Failed to extract varieties: {e}")

    logger.info("Extracting Weeds...")
    try:
        weed_res = await extraction_svc.extract_weeds(chunk_text)
        for w in weed_res.weeds:
            await repo.merge_weed(w.name, w.scientific_name, w.weed_type, source="ACPM 2025")
            for c in w.controlled_by:
                await repo.merge_chemical(type("obj", (object,), {"name": c, "trade_names": [], "chemical_type": "Herbicide", "model_dump": lambda self: {"name": c, "trade_names": [], "chemical_type": "Herbicide"}})())
                await repo.merge_weed_controlled_by(w.name, c)
        logger.info(f"Merged {len(weed_res.weeds)} weeds.")
    except Exception as e:
        logger.error(f"Failed to extract weeds: {e}")

    logger.info("Extracting Crop Stages...")
    try:
        stage_res = await extraction_svc.extract_crop_stages(chunk_text)
        for s in stage_res.stages:
            await repo.merge_crop_stage(s.name, s.phase, source="ACPM 2025")
            for ns in s.precedes:
                await repo.merge_crop_stage(ns, None, source="ACPM 2025")
                await repo.merge_precedes(s.name, ns)
        logger.info(f"Merged {len(stage_res.stages)} crop stages.")
    except Exception as e:
        logger.error(f"Failed to extract crop stages: {e}")


async def main():
    load_dotenv()
    
    neo4j_client = Neo4jClient(
        uri=os.environ["NEO4J_URI"],
        username=os.environ["NEO4J_USERNAME"],
        password=os.environ["NEO4J_PASSWORD"],
        database=os.environ.get("NEO4J_DATABASE", "neo4j"),
    )
    await neo4j_client.connect()
    
    gemini_client = GeminiClient(
        api_key=os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    )
    
    repo = GraphRepository(neo4j_client)
    extraction_svc = ExtractionService(gemini_client)
    
    try:
        await extract_and_seed_acpm(extraction_svc, repo)
    finally:
        await neo4j_client.close()

if __name__ == "__main__":
    asyncio.run(main())
