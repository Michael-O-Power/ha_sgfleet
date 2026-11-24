"""Read-only To-Do list platform exposing SG Fleet active & recent reimbursement claims."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SgfleetConfigEntry
from .const import PENDING_REIMBURSEMENT_STATUSES
from .coordinator import SgfleetDataUpdateCoordinator
from .entity import SgfleetVehicleEntity


def _parse_claim_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        text = str(raw).strip()
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_claim_pending(claim: dict[str, Any]) -> bool:
    status = str(claim.get("status") or "").strip().lower()
    action = str(claim.get("proposedActionText") or "").strip().lower()
    return status in PENDING_REIMBURSEMENT_STATUSES or action in PENDING_REIMBURSEMENT_STATUSES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SgfleetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SG Fleet reimbursement claims list entities."""
    coordinator = entry.runtime_data
    entities: list[TodoListEntity] = []
    if coordinator.data:
        for vehicle_guid in coordinator.data.vehicles:
            entities.append(SgfleetReimbursementClaimsList(coordinator, vehicle_guid))
    async_add_entities(entities)


class SgfleetReimbursementClaimsList(SgfleetVehicleEntity, TodoListEntity):
    """Read-only Home Assistant To-Do list showing active & recent SG Fleet claims."""

    _attr_translation_key = "reimbursement_claims_list"
    _attr_name = "Reimbursement Claims"
    _attr_icon = "mdi:clipboard-list-outline"
    _attr_supported_features = TodoListEntityFeature(0)

    def __init__(
        self,
        coordinator: SgfleetDataUpdateCoordinator,
        vehicle_guid: str,
    ) -> None:
        super().__init__(
            coordinator,
            vehicle_guid,
            "reimbursement_claims_list",
            device_group="reimbursements",
        )

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return active claims as NEEDS_ACTION and resolved claims as COMPLETED."""
        vdata = self.vehicle_data
        if vdata is None:
            return None

        items: list[TodoItem] = []
        for idx, claim in enumerate(vdata.reimbursement_history):
            ref = claim.get("referenceNumber") or f"CLAIM-{idx + 1}"
            category = claim.get("category") or "Expense"
            amount = float(claim.get("amount") or 0.0)
            status_text = claim.get("status") or "Unknown"
            proposed = claim.get("proposedActionText")
            created_date = _parse_claim_date(claim.get("createdDate"))
            pending = _is_claim_pending(claim)

            summary = f"{ref} — {category} (${amount:,.2f})"
            if not pending:
                summary = f"{summary} [{status_text}]"

            desc_lines = [f"Status: {status_text}"]
            if proposed:
                desc_lines.append(f"Action: {proposed}")
            if created_date:
                desc_lines.append(f"Submitted: {created_date.isoformat()}")

            items.append(
                TodoItem(
                    uid=str(claim.get("reimbursementId") or ref),
                    summary=summary,
                    status=TodoItemStatus.NEEDS_ACTION if pending else TodoItemStatus.COMPLETED,
                    due=created_date,
                    description="\n".join(desc_lines),
                )
            )
        return items
