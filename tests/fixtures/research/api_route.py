from fastapi import APIRouter
from app.models import Item

router = APIRouter(prefix="/api")
@router.api_route(path="/items", methods=["get", "POST"], response_model=Item)
async def items():
    pass
