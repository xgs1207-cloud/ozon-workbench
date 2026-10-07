"""Shared, database-backed image prompt templates (no paid operations)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from pipeline.prompt_library import delete_prompt, list_prompts, save_prompt

router = APIRouter(prefix="/api/workbench/image-prompts")


class PromptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)


@router.get("")
def listing_prompts():
    import api
    return {"ok": True, "items": list_prompts(api.MARKET_DB_PATH)}


@router.post("")
def save_listing_prompt(request: PromptRequest):
    import api
    try:
        item = save_prompt(api.MARKET_DB_PATH, request.name, request.prompt)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"ok": True, **item, "item": item, "model_calls": 0}


@router.delete("/{prompt_id}")
def delete_listing_prompt(prompt_id: str):
    import api
    if not delete_prompt(api.MARKET_DB_PATH, prompt_id):
        raise HTTPException(404, "提示词不存在")
    return {"ok": True, "deleted": True, "model_calls": 0}
