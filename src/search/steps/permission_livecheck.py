"""Remove candidates that fail a live permission-ledger decision."""

from src.search.core import StepOutcome


def handle(context):
    ports = context.get("ports")
    if ports is None:
        return None
    results = context.get("results", [])
    kept = [
        result
        for result in results
        if ports.permissions.can_read_document(
            context["principal"], result["document_id"]
        )
    ]
    context["results"] = kept
    return StepOutcome(detail={"removed": len(results) - len(kept)})
