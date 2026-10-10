from fastapi import APIRouter
from redis import Redis
import json

router = APIRouter()
redis = Redis(host="localhost", port=6379, db=0)

@router.get("/state/{room_id}")
async def get_agent_state(room_id: str):
    state = redis.hgetall(f"state:graph:{room_id}")
    return {"status": "success", "state": {k.decode(): json.loads(v.decode()) for k, v in state.items()}}

@router.get("/memory/{room_id}")
async def get_agent_memory(room_id: str):
    memory = redis.hgetall(f"memory:graph:{room_id}")
    return {"status": "success", "memory": {k.decode(): json.loads(v.decode()) for k, v in memory.items()}}

