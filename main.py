from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from datetime import datetime, timedelta
from pydantic import BaseModel
from sqlalchemy import text
import models
import random
import string
import uvicorn
import os
import base64
import json
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI(title="河神電子序號管理系統")

MINING_SPREADSHEET_ID = os.environ.get("MINING_SPREADSHEET_ID", "1-rEh7Ss4pewD9_bj6xtB8F2chsW2cMT600QDe5jYZE4")
MEMBER_SHEET_NAME = os.environ.get("MEMBER_SHEET_NAME", "會員資料")
MEMBER_ACCOUNT_COLUMN_INDEX = max(int(os.environ.get("MEMBER_ACCOUNT_COLUMN_INDEX", "5")) - 1, 0)
MEMBER_CACHE_SECONDS = int(os.environ.get("MEMBER_CACHE_SECONDS", "300"))
MEMBER_AUTH_REQUIRED = os.environ.get("MEMBER_AUTH_REQUIRED", "1").strip().lower() not in ("0", "false", "no", "off")
_GS_CLIENT = None
_MEMBER_CACHE = {"loaded_at": 0.0, "accounts": set()}


def _normalize_member_account(account: str) -> str:
    return (account or "").strip().lower()


def _service_account_info():
    raw_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_b64:
        return json.loads(base64.b64decode(raw_b64).decode("utf-8"))
    if raw_json:
        return json.loads(raw_json)
    raise RuntimeError("Missing Google service account credentials")


def _get_google_client():
    global _GS_CLIENT
    if _GS_CLIENT is None:
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(_service_account_info(), scopes)
        _GS_CLIENT = gspread.authorize(creds)
    return _GS_CLIENT


def _load_member_accounts(force: bool = False):
    now_ts = time.time()
    if (
        not force
        and _MEMBER_CACHE["accounts"]
        and now_ts - _MEMBER_CACHE["loaded_at"] < MEMBER_CACHE_SECONDS
    ):
        return _MEMBER_CACHE["accounts"]

    client = _get_google_client()
    worksheet = client.open_by_key(MINING_SPREADSHEET_ID).worksheet(MEMBER_SHEET_NAME)
    rows = worksheet.get_all_values()[1:]
    accounts = set()
    for row in rows:
        if len(row) > MEMBER_ACCOUNT_COLUMN_INDEX:
            account = _normalize_member_account(row[MEMBER_ACCOUNT_COLUMN_INDEX])
            if account:
                accounts.add(account)

    _MEMBER_CACHE["loaded_at"] = now_ts
    _MEMBER_CACHE["accounts"] = accounts
    return accounts


def validate_member_account(member_account: str):
    normalized = _normalize_member_account(member_account)
    if not normalized:
        return False, "請輸入會員帳號", normalized

    try:
        accounts = _load_member_accounts()
    except Exception as exc:
        print("Member account validation error:", exc)
        if MEMBER_AUTH_REQUIRED:
            return False, "會員資料驗證尚未設定，請聯絡管理員", normalized
        return True, "會員資料驗證略過", normalized

    if normalized not in accounts:
        return False, "查無此會員帳號，請確認會員帳號是否正確", normalized

    return True, "會員帳號驗證成功", normalized


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 自動補資料庫欄位與初始管理員
try:
    with models.engine.connect() as conn:
        # 確保表已建立 (SQLAlchemy create_all 通常已處理，這裡做欄位補強)
        conn.execute(text("ALTER TABLE heshen_licenses ADD COLUMN IF NOT EXISTS owner_username VARCHAR"))
        conn.commit()
except Exception as e:
    print("Database migration/init info:", e)

@app.on_event("startup")
async def startup_event():
    # 啟動時確保最高管理員是 rg，密碼 123456
    db = models.SessionLocal()
    try:
        admin = db.query(models.AdminUser).filter(models.AdminUser.username == "rg").first()
        if not admin:
            admin = models.AdminUser(username="rg", hashed_password="123456", is_superuser=True)
            db.add(admin)
            db.commit()
            print("Created superuser 'rg'")
        
        # 確保一般管理員 gt5889 也存在
        gt_admin = db.query(models.AdminUser).filter(models.AdminUser.username == "gt5889").first()
        if not gt_admin:
            gt_admin = models.AdminUser(username="gt5889", hashed_password="123456", is_superuser=False)
            db.add(gt_admin)
            db.commit()
            print("Created regular user 'gt5889'")
    except Exception as e:
        print("Startup admin creation error:", e)
    finally:
        db.close()


@app.post("/api/admin/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    db = models.SessionLocal()
    try:
        admin = db.query(models.AdminUser).filter(models.AdminUser.username == username).first()

        # 初始後門 (如果資料庫剛建立)
        if not admin and username == "rg" and password == "123456":
            admin = models.AdminUser(username="rg", hashed_password="123456", is_superuser=True)
            db.add(admin)
            db.commit()
            return {
                "access_token": "local_token_success",
                "token_type": "bearer",
                "is_superuser": True,
                "username": "rg"
            }

        if admin and admin.hashed_password == password:
            return {
                "access_token": "local_token_success",
                "token_type": "bearer",
                "is_superuser": admin.is_superuser,
                "username": admin.username
            }

        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    finally:
        db.close()


@app.get("/api/admin/stats")
async def get_stats(username: str = "rg"):
    db = models.SessionLocal()
    try:
        admin = db.query(models.AdminUser).filter(models.AdminUser.username == username).first()

        q = db.query(models.License)
        # 如果不是超級管理員，只顯示自己的數據
        if admin and not admin.is_superuser:
            q = q.filter(models.License.owner_username == username)

        total = q.count()
        unused = q.filter(models.License.status == "unused").count()
        active = q.filter(models.License.status == "active").count()
        expired = q.filter(models.License.status.in_(["expired", "disabled", "used_once"])).count()

        return {"total": total, "unused": unused, "active": active, "expired": expired}
    finally:
        db.close()


@app.get("/api/admin/licenses")
async def list_licenses(username: str = "rg"):
    db = models.SessionLocal()
    try:
        admin = db.query(models.AdminUser).filter(models.AdminUser.username == username).first()

        q = db.query(models.License)
        # 如果不是超級管理員，只顯示自己的數據
        if admin and not admin.is_superuser:
            q = q.filter(models.License.owner_username == username)

        licenses = q.order_by(models.License.created_at.desc()).all()

        return [
            {
                "id": l.id,
                "serial_code": l.serial_code,
                "type": l.type,
                "status": l.status,
                "note": l.note,
                "activation_date": l.activation_date.isoformat() if l.activation_date else None,
                "expiry_date": l.expiry_date.isoformat() if l.expiry_date else None,
                "created_at": l.created_at.isoformat() if l.created_at else None,
                "last_login_ip": l.last_login_ip,
                "owner_username": l.owner_username
            }
            for l in licenses
        ]
    finally:
        db.close()


@app.post("/api/admin/generate")
async def generate_batch(
    count: int,
    days: int,
    note: str = "",
    username: str = Form("rg")
):
    db = models.SessionLocal()
    try:
        chars = string.ascii_letters + string.digits
        new_codes = []

        for _ in range(count):
            code = "-".join("".join(random.choices(chars, k=4)) for _ in range(3))

            license_type = "一次性登入" if days == 0 else f"{days} 天"

            db.add(models.License(
                serial_code=code,
                type=license_type,
                status="unused",
                note=note,
                owner_username=username
            ))

            new_codes.append(code)

        db.commit()
        return {"message": f"成功生成 {count} 組序號", "codes": new_codes}
    finally:
        db.close()


@app.get("/api/admin/users")
async def list_admin_users():
    db = models.SessionLocal()
    try:
        users = db.query(models.AdminUser).all()
        return [
            {"id": u.id, "username": u.username, "is_superuser": u.is_superuser}
            for u in users
        ]
    finally:
        db.close()


@app.post("/api/admin/users/add")
async def add_admin_user(
    username: str = Form(...),
    password: str = Form(...),
    is_superuser: bool = Form(False)
):
    db = models.SessionLocal()
    try:
        exists = db.query(models.AdminUser).filter(models.AdminUser.username == username).first()
        if exists:
            raise HTTPException(status_code=400, detail="帳號已存在")

        db.add(models.AdminUser(
            username=username,
            hashed_password=password,
            is_superuser=is_superuser
        ))

        db.commit()
        return {"message": "新增帳號成功"}
    finally:
        db.close()


@app.post("/api/admin/users/delete")
async def delete_admin_user(user_id: int):
    db = models.SessionLocal()
    try:
        user = db.query(models.AdminUser).filter(models.AdminUser.id == user_id).first()

        if not user:
            raise HTTPException(status_code=404, detail="帳號不存在")

        db.delete(user)
        db.commit()

        return {"message": "刪除帳號成功"}
    finally:
        db.close()


@app.post("/api/admin/action")
async def take_action(serial_id: int, action: str, days: int = None):
    db = models.SessionLocal()
    try:
        lic = db.query(models.License).filter(models.License.id == serial_id).first()

        if not lic:
            raise HTTPException(status_code=404, detail="序號不存在")

        if action == "disable":
            lic.status = "disabled"
        elif action == "enable":
            if lic.type == "一次性登入":
                lic.status = "unused"
                lic.activation_date = None
                lic.expiry_date = None
            else:
                lic.status = "active" if lic.activation_date else "unused"
        elif action == "delete":
            db.delete(lic)
        elif action == "update_days" and days is not None:
            lic.type = "一次性登入" if days == 0 else f"{days} 天"
            if lic.status == "active" and lic.activation_date:
                lic.expiry_date = None if days == 0 else lic.activation_date + timedelta(days=days)

        db.commit()
        return {"message": "操作成功"}
    finally:
        db.close()


@app.get("/api/verify")
async def verify_serial(code: str, request: Request, member_account: str = ""):
    db = models.SessionLocal()
    try:
        lic = db.query(models.License).filter(models.License.serial_code == code).first()

        if not lic:
            return {"valid": False, "message": "序號無效"}

        if lic.status == "disabled":
            return {"valid": False, "message": "此序號已被停用"}
        if lic.status == "used_once":
            return {"valid": False, "message": "此一次性序號已使用完畢"}

        member_valid, member_message, normalized_member = validate_member_account(member_account)
        if not member_valid:
            return {
                "valid": False,
                "message": member_message,
                "member_valid": False
            }

        now = datetime.utcnow()

        if lic.status == "active":
            if lic.expiry_date and now > lic.expiry_date:
                lic.status = "expired"
                db.commit()
                return {"valid": False, "message": f"序號已於 {lic.expiry_date.strftime('%Y-%m-%d')} 過期"}

            days_left = (lic.expiry_date - now).days + 1 if lic.expiry_date else None

            return {
                "valid": True,
                "days_left": days_left,
                "expiry": lic.expiry_date.strftime("%Y-%m-%d") if lic.expiry_date else "永久",
                "member_valid": True,
                "member_account": normalized_member
            }

        if lic.status == "unused":
            if lic.type == "一次性登入":
                lic.status = "used_once"
                lic.activation_date = now
                lic.expiry_date = now
                lic.last_login_ip = request.client.host
                db.commit()
                return {
                    "valid": True,
                    "days_left": 0,
                    "expiry": "一次性登入已使用",
                    "first_time": True,
                    "one_time": True,
                    "member_valid": True,
                    "member_account": normalized_member
                }

            try:
                days = int(lic.type.split(" ")[0])
            except Exception:
                days = 7

            lic.status = "active"
            lic.activation_date = now
            lic.expiry_date = now + timedelta(days=days)
            lic.last_login_ip = request.client.host

            db.commit()

            return {
                "valid": True,
                "days_left": days,
                "expiry": lic.expiry_date.strftime("%Y-%m-%d"),
                "first_time": True,
                "member_valid": True,
                "member_account": normalized_member
            }

        return {"valid": False, "message": "狀態錯誤"}
    finally:
        db.close()

class ImportSerial(BaseModel):
    code: str
    days: int = 30
    note: str = ""


@app.post("/api/admin/import")
async def import_serial(data: ImportSerial):
    db = models.SessionLocal()
    try:
        existing = db.query(models.License).filter(models.License.serial_code == data.code).first()

        if existing:
            return {"message": "已存在", "skipped": True}

        db.add(models.License(
            serial_code=data.code,
            type="一次性登入" if data.days == 0 else f"{data.days} 天",
            status="unused",
            note=data.note,
            owner_username="gt5889"
        ))

        db.commit()
        return {"message": "匯入成功", "code": data.code}
    finally:
        db.close()


script_dir = os.path.dirname(os.path.realpath(__file__))
static_dir = os.path.join(script_dir, "static")

def get_html_path(filename):
    # 優先找 static 資料夾，找不到就找根目錄
    static_path = os.path.join(static_dir, filename)
    if os.path.exists(static_path):
        return static_path
    return os.path.join(script_dir, filename)

@app.get("/")
@app.get("/admin")
@app.get("/admin.html")
async def get_admin():
    return FileResponse(get_html_path("admin.html"))

@app.get("/login")
@app.get("/login.html")
async def get_login():
    return FileResponse(get_html_path("login.html"))

@app.get("/index.html")
async def get_index():
    return FileResponse(get_html_path("index.html"))

if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.post("/api/admin/fix-owner-to-gt5889")
async def fix_owner_to_gt5889():
    db = models.SessionLocal()
    try:
        count = db.query(models.License).update({
            models.License.owner_username: "gt5889"
        })
        db.commit()
        return {"message": f"已將 {count} 組序號指定給 gt5889"}
    finally:
        db.close()
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
