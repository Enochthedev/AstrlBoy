"""
Skills endpoints — list and inspect registered skills.
"""

from fastapi import APIRouter, HTTPException

from core.exceptions import SkillNotFound
from skills.registry import skill_registry

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("")
async def list_skills() -> list[dict]:
    """List all registered skills."""
    skills = await skill_registry.list_all()
    return [
        {"name": s.name, "description": s.description, "version": s.version}
        for s in skills
    ]


@router.get("/{name}")
async def get_skill(name: str) -> dict:
    """Get details for a specific skill."""
    try:
        skill = await skill_registry.get(name)
        return {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "schema": skill.get_schema(),
        }
    except SkillNotFound:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
