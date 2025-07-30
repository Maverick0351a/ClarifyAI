import os
import json
import logging
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from json_repair import repair_json
from openai import OpenAI
from supabase import create_client

# --- Configuration and clients ----------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "").split(",")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable is not set")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != [''] else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

# --- Data models ------------------------------------------------------------

class RepairRequest(BaseModel):
    broken_json: str

# --- Helper functions -------------------------------------------------------

async def perform_repair(broken_json: str):
    try:
        repaired = repair_json(broken_json)
        valid_json = json.loads(repaired)
        return valid_json, "heuristic"
    except Exception as tier1_err:
        logger.info(f"Tier 1 failed: {tier1_err}. Trying Tier 2 (LLM)…")
        prompt = (
            "The following JSON string is broken. Please repair it. "
            "Only output the repaired JSON object without any explanation or formatting.\n"
            f"Broken JSON:\n{broken_json}"
        )
        completion = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a JSON repair expert."},
                {"role": "user", "content": prompt}
            ]
        )
        repaired = completion.choices[0].message.content
        valid_json = json.loads(repaired)
        return valid_json, "llm"

def get_profile_by_api_key(api_key: str):
    """
    Fetch the profile {id, credits} matching api_key, or None if not found.
    """
    try:
        resp = (
            supabase
              .from_("profiles")
              .select("id,credits")
              .eq("api_key", api_key)
              .limit(1)
              .execute()
        )
    except Exception as e:
        logger.error("Error querying Supabase profiles: %s", e)
        return None

    # If no data or empty list, key is invalid
    if not resp.data or len(resp.data) == 0:
        return None

    return resp.data[0]

def decrement_credit(profile_id: str, current_credits: int):
    new_credits = current_credits - 1
    try:
        supabase.from_("profiles").update({"credits": new_credits}).eq("id", profile_id).execute()
    except Exception as e:
        logger.error("Failed to decrement credits for %s: %s", profile_id, e)
    return new_credits

# --- Routes -----------------------------------------------------------------

@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}

@app.get("/", tags=["test"])
def read_root():
    return {"message": "Welcome to the Clarify AI backend"}

@app.post("/repair/demo", tags=["repair"])
async def repair_demo(request: RepairRequest):
    if len(request.broken_json) > 5000:
        raise HTTPException(status_code=413, detail="Input too large for demo")
    try:
        repaired, tier = await perform_repair(request.broken_json)
        return {"repaired_json": repaired, "tier": tier}
    except Exception:
        logger.exception("Demo repair failed")
        raise HTTPException(status_code=500, detail="Repair failed")

@app.post("/repair", tags=["repair"])
async def repair_endpoint(
    request: RepairRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key missing")

    profile = get_profile_by_api_key(x_api_key)
    if profile is None:
        raise HTTPException(status_code=403, detail="Invalid API key")
    if profile["credits"] <= 0:
        raise HTTPException(status_code=402, detail="Insufficient credits")
    if len(request.broken_json) > 50000:
        raise HTTPException(status_code=413, detail="Input too large")

    try:
        repaired, tier = await perform_repair(request.broken_json)
        credits_left = decrement_credit(profile["id"], profile["credits"])
        return {
            "repaired_json": repaired,
            "tier": tier,
            "credits_left": credits_left
        }
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Repair produced invalid JSON")
    except Exception:
        logger.exception("Repair failed")
        raise HTTPException(status_code=500, detail="Repair failed")



