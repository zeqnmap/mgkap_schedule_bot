from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Request

from config import SHARED_SECRET


def register_internal_api(runtime) -> APIRouter:
    router = APIRouter()

    def authorize(secret: str | None) -> None:
        if not secret or not hmac.compare_digest(secret, SHARED_SECRET):
            raise HTTPException(status_code=401, detail="Invalid internal token")

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/internal/broadcast-pdf")
    async def broadcast_pdf(request: Request, audience: str, filename: str = "Расписание.pdf", caption: str = "", x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
        authorize(x_bot_secret)
        if audience not in {"student", "teacher", "all"}:
            raise HTTPException(status_code=400, detail="Audience must be student, teacher or all")
        content = await request.body()
        if not content or not content.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="Body must be a non-empty PDF")
        if len(content) > 49 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF is too large")
        job_id = runtime.repository.enqueue_pdf(audience, filename, caption, content)
        return {"job_id": job_id, "queued": len(runtime.repository.recipients(audience)), "delivered": 0, "failed": 0}

    @router.get("/internal/broadcast/{job_id}")
    async def broadcast_status(job_id: int, x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
        authorize(x_bot_secret)
        return runtime.repository.job_stats(job_id)

    return router
