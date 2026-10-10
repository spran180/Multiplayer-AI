from fastapi import APIRouter
from redis import Redis

router = APIRouter()
redis = Redis(host='localhost', port=6379, db=0)

@router.post("/room/create")
async def create_room():
    room_id = str(uuid.uuid4())
    redis.sadd("rooms:active", room_id)
    return {"status": "success", "room_id": room_id}

@router.get("/room/join")
async def join_room(room_id: str):
    if redis.sismember("rooms:active", room_id):
        return {"status": "success", "message": f"Joined room {room_id}"}
    else:
        return {"status": "error", "message": "Room not found"}