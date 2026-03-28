from fastapi import FastAPI
from pydantic import BaseModel
from pymongo import MongoClient

app = FastAPI()

# MongoDB setup
client = MongoClient("YOUR_MONGO_URL")
db = client["botdb"]
users = db["users"]

class VerifyRequest(BaseModel):
    user_id: str
    referral: str | None = None

@app.post("/verify")
def verify(data: VerifyRequest):
    user_id = data.user_id
    referral = data.referral

    user = users.find_one({"_id": user_id})

    if not user:
        # New user
        users.insert_one({
            "_id": user_id,
            "balance": 0,
            "ref_count": 0,
            "verified": False
        })
        # Referral logic
        if referral and referral != user_id:
            ref_user = users.find_one({"_id": referral})
            if ref_user:
                users.update_one({"_id": referral}, {"$inc": {"balance": 1, "ref_count": 1}})

    # Mark verified
    users.update_one({"_id": user_id}, {"$set": {"verified": True}})

    return {"status": "success", "message": "User Verified ✅"}
