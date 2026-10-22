from fastapi import APIRouter, HTTPException, Request

import lg_thinq

router = APIRouter(prefix="/api/lg_thinq", tags=["LG ThinQ"])


@router.get("/status")
async def status():
    return lg_thinq.monitor.public_status()


@router.post("/credentials")
async def save_credentials(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"token", "country_code"}:
            raise ValueError("Invalid LG connection settings")
        credentials = lg_thinq.prepare_credentials(
            body.get("token"), body.get("country_code", "US")
        )
    except (ValueError, TypeError):
        raise HTTPException(
            400, "Enter a valid LG ThinQ token and two-letter account country"
        ) from None
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            api = lg_thinq.make_api(session, credentials)
            devices = await api.async_get_device_list(timeout=8)
            if not isinstance(devices, list):
                raise lg_thinq.ThinQUnavailable("Invalid LG device list")
    except Exception as error:
        raise HTTPException(400, lg_thinq.safe_error(error)) from None
    try:
        lg_thinq.save_credentials(credentials["token"], credentials["country_code"])
    except OSError:
        raise HTTPException(500, "Could not save encrypted LG credentials") from None
    await lg_thinq.monitor.refresh(force_list=True)
    return lg_thinq.monitor.public_status()


@router.post("/refresh")
async def refresh():
    await lg_thinq.monitor.refresh(force_list=True)
    return lg_thinq.monitor.public_status()


@router.delete("/credentials")
async def clear_credentials():
    try:
        lg_thinq.clear_credentials()
    except OSError:
        raise HTTPException(500, "Could not remove LG credentials") from None
    return lg_thinq.monitor.public_status()
