"""Context-recipe realization: renders the architect's context recipe.

Extracted from context_compiler/context_views to honor the 500-LOC cap.
"""
from __future__ import annotations

from typing import Any

from .context_views import (
    _RECENT_RECIPE_SELECTORS,
    _SUPPORTED_EXACT_RECIPE_SELECTORS,
    item_count,
    last_failures,
    queryable_receipt_meta,
    queryable_section_meta,
    receipt_inline_view,
    recent_receipts,
)
from .ledger import ExecutionLedger


def apply_recipe(
compiled: Any,
ledger: ExecutionLedger,
recipe: Any,
available: dict[str, Any],
mode: str,
) -> dict[str, Any]:
    packet: dict[str, Any] = {"automatic_memory_available": True}
    if mode == "retrieval_augmented":
        packet["automatic_memory_guidance"] = (
            "Memory repeat interception is automatic. When prior evidence matches a proposed read, command, check, or overwrite, "
            "use that surfaced evidence, narrow the target, justify the repeat, or change strategy."
        )

    selected: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    queryable_not_inline: list[dict[str, Any]] = []

    for field_name in recipe.unsupported_fields:
        rejected.append({
            "field": field_name,
            "reason": "unsupported_recipe_field",
        })

    queryable_requested = tuple(dict.fromkeys(recipe.make_queryable_not_inline))
    exact_requested = tuple(dict.fromkeys(recipe.always_include + recipe.preserve_exact))
    exact_preserved = set(recipe.preserve_exact)
    queryable_requested_set = set(queryable_requested)
    selected_keys: set[str] = set()

    for selector in exact_requested:
        if selector not in _SUPPORTED_EXACT_RECIPE_SELECTORS:
            rejected.append({
                "selector": selector,
                "reason": "unsupported_selector",
                "selector_type": "exact",
            })
            continue
        if selector in queryable_requested_set and selector not in exact_preserved:
            meta = queryable_section_meta(selector, available.get(selector))
            queryable_not_inline.append(meta)
            omitted.append({
                "selector": selector,
                "reason": "queryable_not_inline",
            })
            continue
        if selector in queryable_requested_set and selector in exact_preserved:
            omitted.append({
                "selector": selector,
                "reason": "queryable_request_overridden_by_preserve_exact",
            })
        if selector not in available:
            omitted.append({
                "selector": selector,
                "reason": "no_data",
            })
            continue
        packet[selector] = available[selector]
        selected_keys.add(selector)
        selected.append({
            "selector": selector,
            "section": selector,
            "item_count": item_count(available[selector]),
            "preserved_exact": selector in exact_preserved,
        })

    for item in recipe.include_recent:
        selector = item.selector
        count = max(0, int(item.count))
        if selector not in _RECENT_RECIPE_SELECTORS:
            rejected.append({
                "selector": selector,
                "reason": "unsupported_selector",
                "selector_type": "recent",
                "requested_count": count,
            })
            continue
        if count <= 0:
            omitted.append({
                "selector": selector,
                "reason": "nonpositive_count",
                "requested_count": count,
            })
            continue
        receipts = recent_receipts(selector, ledger, count)
        if selector in queryable_requested_set:
            queryable_not_inline.append(queryable_receipt_meta(selector, receipts, count))
            omitted.append({
                "selector": selector,
                "reason": "queryable_not_inline",
                "requested_count": count,
                "matching_count": len(receipts),
            })
            continue
        if not receipts:
            omitted.append({
                "selector": selector,
                "reason": "no_matching_receipts",
                "requested_count": count,
            })
            continue
        packet[selector] = [receipt_inline_view(receipt) for receipt in receipts]
        selected_keys.add(selector)
        selected.append({
            "selector": selector,
            "section": selector,
            "requested_count": count,
            "included_count": len(receipts),
            "receipt_ids": [receipt.receipt_id for receipt in receipts],
        })

    if recipe.include_last_failure > 0:
        failures = last_failures(ledger, recipe.include_last_failure)
        selector = "last_failures"
        if selector in queryable_requested_set:
            queryable_not_inline.append(queryable_receipt_meta(selector, failures, recipe.include_last_failure))
            omitted.append({
                "selector": selector,
                "reason": "queryable_not_inline",
                "requested_count": recipe.include_last_failure,
                "matching_count": len(failures),
            })
        elif failures:
            packet[selector] = [receipt_inline_view(receipt) for receipt in failures]
            selected.append({
                "selector": selector,
                "section": selector,
                "requested_count": recipe.include_last_failure,
                "included_count": len(failures),
                "receipt_ids": [receipt.receipt_id for receipt in failures],
            })
            selected_keys.add(selector)
        else:
            omitted.append({
                "selector": selector,
                "reason": "no_matching_receipts",
                "requested_count": recipe.include_last_failure,
            })

    for selector in queryable_requested:
        if selector in selected_keys:
            continue
        if any(item.get("selector") == selector for item in queryable_not_inline):
            continue
        if (
            selector not in _SUPPORTED_EXACT_RECIPE_SELECTORS
            and selector not in _RECENT_RECIPE_SELECTORS
            and selector != "last_failures"
        ):
            if not any(item.get("selector") == selector for item in rejected):
                rejected.append({
                    "selector": selector,
                    "reason": "unsupported_selector",
                    "selector_type": "queryable_not_inline",
                })
            continue
        if selector in _SUPPORTED_EXACT_RECIPE_SELECTORS:
            queryable_not_inline.append(queryable_section_meta(selector, available.get(selector)))
        elif selector in _RECENT_RECIPE_SELECTORS:
            queryable_not_inline.append(
                queryable_receipt_meta(
                    selector,
                    recent_receipts(selector, ledger, len(ledger.all_receipts())),
                    0,
                )
            )
        elif selector == "last_failures":
            queryable_not_inline.append(
                queryable_receipt_meta(
                    selector,
                    last_failures(ledger, len(ledger.all_receipts())),
                    0,
                )
            )
        if not any(item.get("selector") == selector for item in queryable_not_inline):
            omitted.append({
                "selector": selector,
                "reason": "queryable_not_inline_unselected",
            })
        else:
            omitted.append({
                "selector": selector,
                "reason": "queryable_not_inline",
            })

    realization = {
        "enabled": True,
        "mode_fallback": mode,
        "declared": {
            "always_include": list(recipe.always_include),
            "include_recent": [
                {"selector": item.selector, "count": int(item.count)}
                for item in recipe.include_recent
            ],
            "include_last_failure": int(recipe.include_last_failure),
            "preserve_exact": list(recipe.preserve_exact),
            "make_queryable_not_inline": list(recipe.make_queryable_not_inline),
            "unsupported_fields": list(recipe.unsupported_fields),
        },
        "selected": selected,
        "omitted": omitted,
        "rejected": rejected,
        "queryable_not_inline": queryable_not_inline,
        "counts": {
            "selected": len(selected),
            "omitted": len(omitted),
            "rejected": len(rejected),
            "queryable_not_inline": len(queryable_not_inline),
        },
    }
    packet["context_recipe_realization"] = realization
    return packet
