from fastapi import APIRouter
from redis import Redis

router = APIRouter()
redis = Redis(host='localhost', port=6379, db=0)

@router.post("/control/request")
async def request_control(room_id: str, user_id: str):
    if redis.setnx(f"steering:lock:{room_id}", user_id):
        return {"status": "success", "message": "Control acquired"}
    else:
        current_user = redis.get(f"steering:lock:{room_id}")
        return {"status": "error", "message": f"Control held by {current_user.decode()}"}

@router.post("/control/release")
async def release_control(room_id: str, user_id: str):
    current_user = redis.get(f"steering:lock:{room_id}")
    if current_user.decode() == user_id:
        redis.delete(f"steering:lock:{room_id}")
        return {"status": "success", "message": "Control released"}
    else:
        return {"status": "error", "message": "Not authorized to release control"}