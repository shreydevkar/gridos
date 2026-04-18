"""/cloud/status — the one endpoint that's always on, OSS and SaaS alike.

The frontend hits this on bootstrap to decide whether to show login UI,
workbook-list UI, upgrade prompts, etc. It's intentionally cheap + unauth'd.
"""
from fastapi import APIRouter

from cloud import config

router = APIRouter(prefix="/cloud", tags=["cloud"])


@router.get("/status")
async def cloud_status():
    return config.snapshot()
