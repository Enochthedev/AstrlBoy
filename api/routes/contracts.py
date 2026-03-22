"""
Contract CRUD endpoints.
"""

from fastapi import APIRouter, HTTPException

from contracts.schema import ContractCreate, ContractResponse
from contracts.service import contracts_service
from core.exceptions import ContractNotFound

router = APIRouter(prefix="/contracts", tags=["contracts"])


@router.post("", response_model=ContractResponse, status_code=201)
async def create_contract(data: ContractCreate) -> ContractResponse:
    """Create a new contract (onboard new client)."""
    contract = await contracts_service.create_contract(data)
    return ContractResponse.model_validate(contract)


@router.get("", response_model=list[ContractResponse])
async def list_contracts() -> list[ContractResponse]:
    """List all contracts."""
    contracts = await contracts_service.get_active_contracts()
    return [ContractResponse.model_validate(c) for c in contracts]


@router.get("/{slug}", response_model=ContractResponse)
async def get_contract(slug: str) -> ContractResponse:
    """Get contract details by slug."""
    try:
        contract = await contracts_service.get_contract(slug)
        return ContractResponse.model_validate(contract)
    except ContractNotFound:
        raise HTTPException(status_code=404, detail=f"Contract '{slug}' not found")


@router.patch("/{slug}/pause", response_model=ContractResponse)
async def pause_contract(slug: str) -> ContractResponse:
    """Pause a contract."""
    try:
        contract = await contracts_service.pause_contract(slug)
        return ContractResponse.model_validate(contract)
    except ContractNotFound:
        raise HTTPException(status_code=404, detail=f"Contract '{slug}' not found")


@router.patch("/{slug}/resume", response_model=ContractResponse)
async def resume_contract(slug: str) -> ContractResponse:
    """Resume a paused contract."""
    try:
        # Re-activate by setting status back to active
        contract = await contracts_service.get_contract(slug)
        # Direct update for resume
        from sqlalchemy import update as sql_update
        from db.base import async_session_factory
        from db.models.contracts import Contract as ContractModel

        async with async_session_factory() as session:
            await session.execute(
                sql_update(ContractModel)
                .where(ContractModel.client_slug == slug)
                .values(status="active")
            )
            await session.commit()
            contract = await contracts_service.get_contract(slug)

        return ContractResponse.model_validate(contract)
    except ContractNotFound:
        raise HTTPException(status_code=404, detail=f"Contract '{slug}' not found")
