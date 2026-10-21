from __future__ import annotations

import hmac
import io
import json
import zipfile

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

    @router.post("/internal/group-schedule")
    async def group_schedule(request: Request, group_number: int, filename: str = "Расписание группы.pdf", caption: str = "", x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
        authorize(x_bot_secret)
        if group_number < 1:
            raise HTTPException(status_code=400, detail="Некорректный номер группы")
        content = await request.body()
        if not content or not content.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="Body must be a non-empty PDF")
        if len(content) > 49 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF is too large")
        runtime.repository.save_group_schedule(group_number, filename, caption, content)
        return {"group_number": group_number}

    @router.post("/internal/store-pdf")
    async def store_pdf(request: Request, audience: str, filename: str = "Расписание.pdf", caption: str = "", x_bot_secret: str | None = Header(default=None)) -> dict[str, str]:
        authorize(x_bot_secret)
        if audience not in {"student", "teacher"}:
            raise HTTPException(status_code=400, detail="Audience must be student or teacher")
        content = await request.body()
        if not content or not content.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="Body must be a non-empty PDF")
        if len(content) > 49 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF is too large")
        runtime.repository.save_schedule(audience, filename, caption, content)
        return {"status": "stored"}

    @router.post("/internal/broadcast-student-groups")
    async def broadcast_student_groups(publication_id: str | None = None, x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
        authorize(x_bot_secret)
        job_id = runtime.repository.enqueue_student_group_schedules(publication_id)
        return {"job_id": job_id, "queued": runtime.repository.job_stats(job_id)["pending"]}

    @router.post("/internal/publish-student-package")
    async def publish_student_package(request: Request, x_bot_secret: str | None = Header(default=None)) -> dict[str, int | str]:
        authorize(x_bot_secret)
        content = await request.body()
        if not content or len(content) > 100 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Publication package is missing or too large")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as package:
                manifest = json.loads(package.read("manifest.json"))
                group_numbers = [int(number) for number in manifest["groups"]]
                if not group_numbers or len(group_numbers) != len(set(group_numbers)) or any(number < 1 for number in group_numbers):
                    raise ValueError("Publication groups must be unique positive numbers")
                filename = str(manifest.get("filename") or "Расписание.pdf")
                caption = str(manifest.get("caption") or "")
                full_pdf = package.read("student.pdf")
                group_pdfs = {number: (f"Расписание группы {number}.pdf", package.read(f"groups/{number}.pdf")) for number in group_numbers}
        except (KeyError, TypeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid publication package") from exc
        try:
            publication_id, job_id = runtime.repository.publish_student_package(filename, caption, full_pdf, group_pdfs, group_numbers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        stats = runtime.repository.job_stats(job_id)
        return {**stats, "publication_id": publication_id, "job_id": job_id, "queued": stats["pending"]}

    @router.post("/internal/publish-pdf")
    async def publish_pdf(request: Request, audience: str, filename: str = "Расписание.pdf", caption: str = "", x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
        authorize(x_bot_secret)
        if audience not in {"student", "teacher"}:
            raise HTTPException(status_code=400, detail="Audience must be student or teacher")
        content = await request.body()
        if not content or not content.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="Body must be a non-empty PDF")
        if len(content) > 49 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF is too large")
        try:
            job_id = runtime.repository.publish_single_schedule(audience, filename, caption, content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        stats = runtime.repository.job_stats(job_id)
        return {**stats, "job_id": job_id, "queued": stats["pending"]}

    @router.get("/internal/broadcast/{job_id}")
    async def broadcast_status(job_id: int, x_bot_secret: str | None = Header(default=None)) -> dict[str, int]:
        authorize(x_bot_secret)
        return runtime.repository.job_stats(job_id)

    return router
