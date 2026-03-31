from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_db

router = APIRouter(prefix="/api/presets", tags=["presets"])


class PresetCreate(BaseModel):
    module: str
    name: str
    mr_ids: str


class PresetUpdate(BaseModel):
    name: str
    mr_ids: str


@router.get("/{module}")
def list_presets(module: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM saved_presets WHERE module = ? ORDER BY created_at DESC",
        (module,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("")
def create_preset(req: PresetCreate):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO saved_presets (module, name, mr_ids) VALUES (?, ?, ?)",
        (req.module, req.name, req.mr_ids),
    )
    conn.commit()
    preset_id = cur.lastrowid
    row = conn.execute("SELECT * FROM saved_presets WHERE id = ?", (preset_id,)).fetchone()
    conn.close()
    return dict(row)


@router.put("/{preset_id}")
def update_preset(preset_id: int, req: PresetUpdate):
    conn = get_db()
    conn.execute(
        "UPDATE saved_presets SET name = ?, mr_ids = ? WHERE id = ?",
        (req.name, req.mr_ids, preset_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@router.delete("/{preset_id}")
def delete_preset(preset_id: int):
    conn = get_db()
    conn.execute("DELETE FROM saved_presets WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()
    return {"ok": True}
