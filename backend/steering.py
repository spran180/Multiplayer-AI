from fastapi import APIRouter
from redis import Redis
import json

router = APIRouter()
redis = Redis(host='localhost', port=6379, db=0)

@router.post("/steering/pause")
async def toggle_pause(room_id: str = None):
    current_state = redis.get(f"steering:pause:{room_id}")
    new_state = not bool(current_state)
    redis.set(f"steering:pause:{room_id}", json.dumps(new_state))
    return {"status": "success", "new_state": new_state}

@router.post("/steering/interrupt")
async def interrupt(room_id: str = None):
    redis.set(f"steering:interrupt:{room_id}", json.dumps(True))
    return {"status": "success", "message": "Interrupt signal sent"}