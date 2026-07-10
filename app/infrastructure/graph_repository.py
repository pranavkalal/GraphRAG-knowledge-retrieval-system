"""Data access layer for Neo4j IPM graph with fuzzy entity resolution."""

from app.infrastructure.neo4j_client import Neo4jClient
from app.models.ontology import Pest, Chemical, MoAGroup


# ── Fuzzy String Similarity Utilities ────────────────────────────────────────

def levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate the Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
        
    return previous_row[-1]


def string_similarity(s1: str, s2: str) -> float:
    """Calculate normalized similarity between two strings (0.0 to 1.0)."""
    s1_clean = s1.lower().strip()
    s2_clean = s2.lower().strip()
    if not s1_clean or not s2_clean:
        return 0.0
    if s1_clean == s2_clean:
        return 1.0
    dist = levenshtein_distance(s1_clean, s2_clean)
    max_len = max(len(s1_clean), len(s2_clean))
    return 1.0 - (dist / max_len)


# ── Repository ───────────────────────────────────────────────────────────────

class GraphRepository:
    """Repository mapping domain objects to Neo4j Cypher queries with fuzzy matching."""

    def __init__(self, neo4j_client: Neo4jClient):
        self._db = neo4j_client
        self._entity_cache = {}  # Cache structure: {label: {canonical_name: set([aliases])}}

    async def ensure_constraints(self) -> None:
        """Create required uniqueness constraints."""
        queries = [
            "CREATE CONSTRAINT pest_name IF NOT EXISTS FOR (p:Pest) REQUIRE p.name IS UNIQUE",
            "CREATE CONSTRAINT chemical_name IF NOT EXISTS FOR (c:Chemical) REQUIRE c.name IS UNIQUE",
            "CREATE CONSTRAINT moa_code IF NOT EXISTS FOR (m:MoAGroup) REQUIRE m.group_code IS UNIQUE",
        ]
        for query in queries:
            await self._db.run_query(query)

    # ── Fuzzy Entity Resolution & Caching Utilities ──────────────────────────

    async def _populate_cache_for_label(self, label: str) -> None:
        """Fetch all existing nodes of a label and populate the in-memory cache."""
        id_field = "canonical_term" if label == "Term" else "acronym" if label == "Acronym" else "name"
        query = f"MATCH (n:{label}) RETURN n.{id_field} AS name, n.aliases AS aliases"
        
        try:
            records = await self._db.run_query(query)
            label_cache = {}
            for record in records:
                name = record.get("name")
                if not name:
                    continue
                aliases = record.get("aliases") or []
                label_cache[name] = set(a.lower().strip() for a in aliases)
            self._entity_cache[label] = label_cache
        except Exception:
            # Fallback in case of database connectivity issues during initialization
            self._entity_cache[label] = {}

    async def resolve_entity_name(self, label: str, name: str, threshold: float = 0.85) -> str:
        """
        Check if an entity of a given label exists under a similar name or alias in Neo4j.
        If a similar entity is found, returns its canonical name.
        Otherwise, returns the input name.
        """
        if not name or not isinstance(name, str):
            return name
        
        name_clean = name.lower().strip()
        if not name_clean:
            return name

        # 1. Ensure cache is populated for this label
        if label not in self._entity_cache:
            await self._populate_cache_for_label(label)

        label_cache = self._entity_cache[label]

        # 2. Check for exact match or alias match (O(1) lookups)
        for canonical, aliases in label_cache.items():
            if canonical.lower().strip() == name_clean or name_clean in aliases:
                return canonical

        # 3. Check for fuzzy match using Levenshtein similarity
        best_match = None
        highest_similarity = 0.0

        for canonical, aliases in label_cache.items():
            sim = string_similarity(canonical, name)
            if sim > highest_similarity:
                highest_similarity = sim
                best_match = canonical

            for alias in aliases:
                sim = string_similarity(alias, name)
                if sim > highest_similarity:
                    highest_similarity = sim
                    best_match = canonical

        if highest_similarity >= threshold and best_match:
            # We found a close match! Register the new spelling as an alias in memory
            label_cache[best_match].add(name_clean)
            return best_match

        # 4. No match found: Register this as a new canonical entity in the cache
        label_cache[name] = set()
        return name

    async def add_alias_if_new(self, label: str, canonical_name: str, alias: str) -> None:
        """Append an alias to the aliases property of a node if it is not already there."""
        if not canonical_name or not alias or canonical_name.lower().strip() == alias.lower().strip():
            return
        
        id_field = "canonical_term" if label == "Term" else "acronym" if label == "Acronym" else "name"
        
        # Pure Cypher alias appending (idempotent duplicate removal)
        query = f"""
        MATCH (n:{label} {{{id_field}: $canonical_name}})
        WITH n, coalesce(n.aliases, []) + [$alias] AS all_aliases
        UNWIND all_aliases AS a
        WITH n, collect(DISTINCT a) AS unique_aliases
        SET n.aliases = unique_aliases
        """
        try:
            await self._db.run_query(query, {"canonical_name": canonical_name, "alias": alias})
        except Exception:
            pass  # Fail gracefully if database write fails

    # ── Merges with Fuzzy Resolution ─────────────────────────────────────────

    async def merge_pest(self, pest: Pest) -> None:
        """Idempotent upsert of a Pest node with fuzzy entity resolution."""
        original_name = pest.name
        resolved_name = await self.resolve_entity_name("Pest", original_name)
        pest.name = resolved_name

        query = """
        MERGE (p:Pest {name: $name})
        SET p.scientific_name = $scientific_name,
            p.pest_type = $pest_type,
            p.category = $category
        """
        await self._db.run_query(query, pest.model_dump())

        if resolved_name != original_name:
            await self.add_alias_if_new("Pest", resolved_name, original_name)

    async def merge_chemical(self, chemical: Chemical) -> None:
        """Idempotent upsert of a Chemical node with fuzzy entity resolution."""
        original_name = chemical.name
        resolved_name = await self.resolve_entity_name("Chemical", original_name)
        chemical.name = resolved_name

        query = """
        MERGE (c:Chemical {name: $name})
        SET c.trade_names = $trade_names,
            c.chemical_type = $chemical_type
        """
        await self._db.run_query(query, chemical.model_dump())

        if resolved_name != original_name:
            await self.add_alias_if_new("Chemical", resolved_name, original_name)

    async def merge_moa_group(self, moa: MoAGroup) -> None:
        """Idempotent upsert of a Mode of Action Group node."""
        query = """
        MERGE (m:MoAGroup {group_code: $group_code})
        SET m.group_name = $group_name
        """
        await self._db.run_query(query, moa.model_dump())

    async def merge_controlled_by(
        self,
        pest_name: str,
        chemical_name: str,
        resistance_status: str | None = None,
        beneficial_impact: str | None = None,
        max_applications: str | None = None,
        source: str | None = None
    ) -> None:
        """Link a Pest to a Chemical via CONTROLLED_BY, resolving entities first."""
        resolved_pest = await self.resolve_entity_name("Pest", pest_name)
        resolved_chem = await self.resolve_entity_name("Chemical", chemical_name)

        query = """
        MATCH (p:Pest {name: $pest_name})
        MATCH (c:Chemical {name: $chemical_name})
        MERGE (p)-[r:CONTROLLED_BY]->(c)
        SET r.resistance_status = $resistance_status,
            r.beneficial_impact = $beneficial_impact,
            r.max_applications = $max_applications,
            r.source = $source
        """
        await self._db.run_query(query, {
            "pest_name": resolved_pest,
            "chemical_name": resolved_chem,
            "resistance_status": resistance_status,
            "beneficial_impact": beneficial_impact,
            "max_applications": max_applications,
            "source": source
        })

    async def merge_belongs_to_moa(self, chemical_name: str, group_code: str) -> None:
        """Link a Chemical to its MoAGroup, resolving chemical name first."""
        resolved_chem = await self.resolve_entity_name("Chemical", chemical_name)

        query = """
        MATCH (c:Chemical {name: $chemical_name})
        MATCH (m:MoAGroup {group_code: $group_code})
        MERGE (c)-[:BELONGS_TO]->(m)
        """
        await self._db.run_query(query, {"chemical_name": resolved_chem, "group_code": group_code})

    # ── Disease ─────────────────────────────────────────────────────────────

    async def merge_disease(self, name: str, pathogen: str | None = None,
                            symptoms: str | None = None,
                            favoured_by: str | None = None,
                            management: str | None = None,
                            source: str | None = None) -> None:
        """Idempotent upsert of a Disease node with fuzzy entity resolution."""
        resolved_name = await self.resolve_entity_name("Disease", name)
        
        query = """
        MERGE (d:Disease {name: $name})
        SET d.pathogen = $pathogen,
            d.symptoms = $symptoms,
            d.favoured_by = $favoured_by,
            d.management = $management,
            d.source = $source
        """
        await self._db.run_query(query, {
            "name": resolved_name, "pathogen": pathogen, "symptoms": symptoms,
            "favoured_by": favoured_by, "management": management, "source": source
        })

        if resolved_name != name:
            await self.add_alias_if_new("Disease", resolved_name, name)

    async def merge_affects_crop(self, disease_name: str, crop: str = "cotton") -> None:
        """Link a Disease to the crop it affects, resolving disease name first."""
        resolved_disease = await self.resolve_entity_name("Disease", disease_name)
        
        query = """
        MERGE (c:Crop {name: $crop})
        WITH c
        MATCH (d:Disease {name: $disease_name})
        MERGE (d)-[:AFFECTS]->(c)
        """
        await self._db.run_query(query, {"disease_name": resolved_disease, "crop": crop})

    # ── Beneficial ──────────────────────────────────────────────────────────

    async def merge_beneficial(self, name: str, scientific_name: str | None = None,
                               beneficial_type: str | None = None,
                               source: str | None = None) -> None:
        """Idempotent upsert of a Beneficial node with fuzzy entity resolution."""
        resolved_name = await self.resolve_entity_name("Beneficial", name)

        query = """
        MERGE (b:Beneficial {name: $name})
        SET b.scientific_name = $scientific_name,
            b.beneficial_type = $beneficial_type,
            b.source = $source
        """
        await self._db.run_query(query, {
            "name": resolved_name, "scientific_name": scientific_name,
            "beneficial_type": beneficial_type, "source": source
        })

        if resolved_name != name:
            await self.add_alias_if_new("Beneficial", resolved_name, name)

    async def merge_predates(self, beneficial_name: str, pest_name: str) -> None:
        """Link a Beneficial to a Pest via PREDATES, resolving names first."""
        resolved_beneficial = await self.resolve_entity_name("Beneficial", beneficial_name)
        resolved_pest = await self.resolve_entity_name("Pest", pest_name)

        query = """
        MATCH (b:Beneficial {name: $beneficial_name})
        MATCH (p:Pest {name: $pest_name})
        MERGE (b)-[:PREDATES]->(p)
        """
        await self._db.run_query(query, {
            "beneficial_name": resolved_beneficial, "pest_name": resolved_pest
        })

    # ── Defoliant ───────────────────────────────────────────────────────────

    async def merge_defoliant(self, name: str, product_type: str,
                              trade_names: list[str] | None = None,
                              key_notes: str | None = None,
                              source: str | None = None) -> None:
        """Idempotent upsert of a harvest aid Chemical node with fuzzy entity resolution."""
        resolved_name = await self.resolve_entity_name("Chemical", name)

        query = """
        MERGE (c:Chemical {name: $name})
        SET c.chemical_type = $product_type,
            c.trade_names = $trade_names,
            c.key_notes = $key_notes,
            c.source = $source
        """
        await self._db.run_query(query, {
            "name": resolved_name, "product_type": product_type,
            "trade_names": trade_names or [], "key_notes": key_notes, "source": source
        })

        if resolved_name != name:
            await self.add_alias_if_new("Chemical", resolved_name, name)

    # ── Variety ─────────────────────────────────────────────────────────────

    async def merge_variety(self, name: str, company: str | None = None, crop_type: str | None = None, source: str | None = None) -> None:
        """Idempotent upsert of a Variety node with fuzzy entity resolution."""
        resolved_name = await self.resolve_entity_name("Variety", name)

        query = """
        MERGE (v:Variety {name: $name})
        SET v.company = $company,
            v.crop_type = $crop_type,
            v.source = $source
        """
        await self._db.run_query(query, {"name": resolved_name, "company": company, "crop_type": crop_type, "source": source})

        if resolved_name != name:
            await self.add_alias_if_new("Variety", resolved_name, name)

    async def merge_suited_to(self, variety_name: str, region_name: str) -> None:
        """Link a Variety to a Region via SUITED_TO, resolving names first."""
        resolved_variety = await self.resolve_entity_name("Variety", variety_name)
        resolved_region = await self.resolve_entity_name("Region", region_name)

        query = """
        MERGE (r:Region {name: $region_name})
        WITH r
        MATCH (v:Variety {name: $variety_name})
        MERGE (v)-[:SUITED_TO]->(r)
        """
        await self._db.run_query(query, {"variety_name": resolved_variety, "region_name": resolved_region})

    async def merge_has_trait(self, variety_name: str, trait_name: str, description: str | None = None) -> None:
        """Link a Variety to a Trait via HAS_TRAIT, resolving names first."""
        resolved_variety = await self.resolve_entity_name("Variety", variety_name)
        resolved_trait = await self.resolve_entity_name("Trait", trait_name)

        query = """
        MERGE (t:Trait {name: $trait_name})
        ON CREATE SET t.description = $description
        WITH t
        MATCH (v:Variety {name: $variety_name})
        MERGE (v)-[:HAS_TRAIT]->(t)
        """
        await self._db.run_query(query, {"variety_name": resolved_variety, "trait_name": resolved_trait, "description": description})

    # ── Weed ────────────────────────────────────────────────────────────────

    async def merge_weed(self, name: str, scientific_name: str | None = None, weed_type: str | None = None, source: str | None = None) -> None:
        """Idempotent upsert of a Weed node with fuzzy entity resolution."""
        resolved_name = await self.resolve_entity_name("Weed", name)

        query = """
        MERGE (w:Weed {name: $name})
        SET w.scientific_name = $scientific_name,
            w.weed_type = $weed_type,
            w.source = $source
        """
        await self._db.run_query(query, {"name": resolved_name, "scientific_name": scientific_name, "weed_type": weed_type, "source": source})

        if resolved_name != name:
            await self.add_alias_if_new("Weed", resolved_name, name)

    async def merge_weed_controlled_by(self, weed_name: str, chemical_name: str) -> None:
        """Link a Weed to a Chemical via CONTROLLED_BY, resolving entities first."""
        resolved_weed = await self.resolve_entity_name("Weed", weed_name)
        resolved_chem = await self.resolve_entity_name("Chemical", chemical_name)

        query = """
        MATCH (w:Weed {name: $weed_name})
        MATCH (c:Chemical {name: $chemical_name})
        MERGE (w)-[:CONTROLLED_BY]->(c)
        """
        await self._db.run_query(query, {"weed_name": resolved_weed, "chemical_name": resolved_chem})

    # ── Crop Stage ──────────────────────────────────────────────────────────

    async def merge_crop_stage(self, name: str, phase: str | None = None, source: str | None = None) -> None:
        """Idempotent upsert of a CropStage node with fuzzy entity resolution."""
        resolved_name = await self.resolve_entity_name("CropStage", name)

        query = """
        MERGE (cs:CropStage {name: $name})
        SET cs.phase = $phase,
            cs.source = $source
        """
        await self._db.run_query(query, {"name": resolved_name, "phase": phase, "source": source})

        if resolved_name != name:
            await self.add_alias_if_new("CropStage", resolved_name, name)

    async def merge_precedes(self, stage_name: str, next_stage_name: str) -> None:
        """Link two CropStages via PRECEDES, resolving stages first."""
        resolved_stage = await self.resolve_entity_name("CropStage", stage_name)
        resolved_next = await self.resolve_entity_name("CropStage", next_stage_name)

        query = """
        MATCH (cs1:CropStage {name: $stage_name})
        MATCH (cs2:CropStage {name: $next_stage_name})
        MERGE (cs1)-[:PRECEDES]->(cs2)
        """
        await self._db.run_query(query, {"stage_name": resolved_stage, "next_stage_name": resolved_next})

    # ── Stats & Reporting ───────────────────────────────────────────────────

    async def get_graph_stats(self) -> dict[str, int]:
        """Return counts of nodes and relationships."""
        node_query = "MATCH (n) RETURN count(n) as count"
        rel_query = "MATCH ()-[r]->() RETURN count(r) as count"
        
        nodes = await self._db.run_query(node_query)
        rels = await self._db.run_query(rel_query)
        
        return {
            "nodes": nodes[0]["count"],
            "relationships": rels[0]["count"]
        }

    async def get_label_counts(self) -> list[dict]:
        """Return node counts by label for reporting."""
        query = "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC"
        return await self._db.run_query(query)
