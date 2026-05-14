"""Seed the few-shot store from eval/data/edit_pairs.jsonl via the HTTP API.

Requires a running stack (make up) and at least one seeded document.

Usage:
    LITTUP_BASE_URL=http://localhost:8000 python scripts/synthesize_edits.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

BASE_URL = os.getenv("LITTUP_BASE_URL", "http://localhost:8000")
EDIT_PAIRS = Path(__file__).parent.parent / "eval" / "data" / "edit_pairs.jsonl"


async def main() -> None:
    pairs = [json.loads(ln) for ln in EDIT_PAIRS.read_text().splitlines() if ln.strip()]

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # Discover available documents
        resp = await client.get("/api/documents")
        resp.raise_for_status()
        docs = resp.json().get("documents", [])
        ready_docs = [d for d in docs if d.get("status") == "ready"]
        if not ready_docs:
            print("No ready documents found. Run `make seed` first.", file=sys.stderr)
            sys.exit(1)

        doc_id = ready_docs[0]["id"]
        template_id = pairs[0]["template_id"]

        # Create a draft to attach edits to
        print(f"Creating draft for template={template_id} doc={doc_id}")
        resp = await client.post(
            "/api/drafts",
            json={"template_id": template_id, "document_ids": [doc_id]},
        )
        resp.raise_for_status()
        draft_id = resp.json()["id"]
        print(f"  draft created: {draft_id}")

        # Submit one edit per pair
        for i, pair in enumerate(pairs):
            user_output = {pair["field_or_section"]: pair["user"]}
            resp = await client.post(
                f"/api/drafts/{draft_id}/edit",
                json={"user_output": user_output},
            )
            if resp.status_code == 200:
                print(f"  [{i+1}/{len(pairs)}] edit saved: {pair['field_or_section']}")
            else:
                print(
                    f"  [{i+1}/{len(pairs)}] WARN {resp.status_code}: {pair['field_or_section']}",
                    file=sys.stderr,
                )

    print(f"Done. {len(pairs)} edit pairs submitted.")


if __name__ == "__main__":
    asyncio.run(main())
