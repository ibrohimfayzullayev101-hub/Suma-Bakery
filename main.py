
import os
import hmac
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel, Field

# --------------------------------------------------
# НАСТРОЙКИ
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8983760334:AAGktyGzDaAicC9RsacmAP4xgV0yxkhVGl4").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-5322734246").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

COOKIE_NAME = "suma_admin_session"
serializer = URLSafeTimedSerializer(SECRET_KEY or "temporary-local-secret")

app = FastAPI(title="SUMA BAKERY")


# --------------------------------------------------
# МОДЕЛИ
# --------------------------------------------------

class OrderItem(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=99)


class OrderRequest(BaseModel):
    customer_name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=6, max_length=30)
    comment: str = Field(default="", max_length=500)
    language: Literal["ru", "uz"] = "ru"
    items: list[OrderItem] = Field(min_length=1, max_length=20)


class LoginRequest(BaseModel):
    username: str
    password: str


class ProductCreate(BaseModel):
    name_ru: str = Field(min_length=1, max_length=120)
    name_uz: str = Field(min_length=1, max_length=120)
    price: int = Field(gt=0)
    image_url: str = ""
    description_ru: str = ""
    description_uz: str = ""
    category: str = "dessert"
    active: bool = True


class ProductUpdate(BaseModel):
    name_ru: Optional[str] = None
    name_uz: Optional[str] = None
    price: Optional[int] = Field(default=None, gt=0)
    image_url: Optional[str] = None
    description_ru: Optional[str] = None
    description_uz: Optional[str] = None
    category: Optional[str] = None
    active: Optional[bool] = None


# --------------------------------------------------
# SUPABASE
# --------------------------------------------------

async def supabase_request(method: str, table: str, **kwargs):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(
            status_code=503,
            detail="Supabase не настроен. Проверь переменные окружения."
        )

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

    extra_headers = kwargs.pop("headers", {})
    headers.update(extra_headers)

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                **kwargs
            )

        if not response.is_success:
            print("SUPABASE ERROR:", response.status_code, response.text)
            raise HTTPException(
                status_code=502,
                detail="Ошибка при обращении к базе данных."
            )

        if not response.content:
            return None

        return response.json()

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Не удалось подключиться к Supabase."
        )


# --------------------------------------------------
# АВТОРИЗАЦИЯ АДМИНИСТРАТОРА
# --------------------------------------------------

def require_admin(request: Request):
    if not ADMIN_USERNAME or not ADMIN_PASSWORD or not SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Настройки администратора не заполнены."
        )

    token = request.cookies.get(COOKIE_NAME)

    if not token:
        raise HTTPException(status_code=401, detail="Нужен вход.")

    try:
        username = serializer.loads(token, max_age=60 * 60 * 24)

        if username != ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="Нет доступа.")

        return username

    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Сессия закончилась.")


# --------------------------------------------------
# СТРАНИЦЫ
# --------------------------------------------------

@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/admin")
async def admin_page():
    return FileResponse(BASE_DIR / "admin.html")


# --------------------------------------------------
# ПУБЛИЧНЫЙ КАТАЛОГ
# --------------------------------------------------

@app.get("/api/products")
async def get_products():
    products = await supabase_request(
        "GET",
        "products",
        params={
            "select": "*",
            "active": "eq.true",
            "order": "id.asc"
        }
    )
    return {"products": products or []}


# --------------------------------------------------
# ВХОД И ВЫХОД
# --------------------------------------------------

@app.post("/api/admin/login")
async def admin_login(data: LoginRequest, request: Request):
    if not ADMIN_USERNAME or not ADMIN_PASSWORD or not SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Сначала настрой ADMIN_USERNAME, ADMIN_PASSWORD и SECRET_KEY."
        )

    username_ok = hmac.compare_digest(data.username, ADMIN_USERNAME)
    password_ok = hmac.compare_digest(data.password, ADMIN_PASSWORD)

    if not username_ok or not password_ok:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль.")

    token = serializer.dumps(ADMIN_USERNAME)

    response = {
        "ok": True,
        "message": "Вход выполнен."
    }

    from fastapi.responses import JSONResponse

    result = JSONResponse(response)
    result.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=bool(os.getenv("VERCEL")),
        samesite="lax",
        max_age=60 * 60 * 24,
        path="/"
    )
    return result


@app.post("/api/admin/logout")
async def admin_logout():
    from fastapi.responses import JSONResponse

    result = JSONResponse({"ok": True})
    result.delete_cookie(COOKIE_NAME, path="/")
    return result


@app.get("/api/admin/me")
async def admin_me(admin=Depends(require_admin)):
    return {"ok": True, "username": admin}


# --------------------------------------------------
# УПРАВЛЕНИЕ ТОВАРАМИ
# --------------------------------------------------

@app.get("/api/admin/products")
async def admin_get_products(admin=Depends(require_admin)):
    products = await supabase_request(
        "GET",
        "products",
        params={"select": "*", "order": "id.asc"}
    )
    return {"products": products or []}


@app.post("/api/admin/products")
async def admin_add_product(
    product: ProductCreate,
    admin=Depends(require_admin)
):
    created = await supabase_request(
        "POST",
        "products",
        json=product.model_dump(),
        headers={"Prefer": "return=representation"}
    )
    return {"ok": True, "product": created}


@app.patch("/api/admin/products/{product_id}")
async def admin_update_product(
    product_id: int,
    product: ProductUpdate,
    admin=Depends(require_admin)
):
    changes = product.model_dump(exclude_unset=True)

    if not changes:
        raise HTTPException(status_code=400, detail="Нет изменений.")

    updated = await supabase_request(
        "PATCH",
        "products",
        params={"id": f"eq.{product_id}"},
        json=changes,
        headers={"Prefer": "return=representation"}
    )

    return {"ok": True, "product": updated}


@app.delete("/api/admin/products/{product_id}")
async def admin_delete_product(
    product_id: int,
    admin=Depends(require_admin)
):
    # Не удаляем запись навсегда, а скрываем товар.
    await supabase_request(
        "PATCH",
        "products",
        params={"id": f"eq.{product_id}"},
        json={"active": False}
    )
    return {"ok": True}


# --------------------------------------------------
# ИСТОРИЯ ЗАКАЗОВ
# --------------------------------------------------

@app.get("/api/admin/orders")
async def admin_get_orders(admin=Depends(require_admin)):
    orders = await supabase_request(
        "GET",
        "orders",
        params={
            "select": "*",
            "order": "created_at.desc",
            "limit": "100"
        }
    )
    return {"orders": orders or []}


# --------------------------------------------------
# ОФОРМЛЕНИЕ ЗАКАЗА
# --------------------------------------------------

def format_sum(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " сум"


@app.post("/api/order")
async def create_order(order: OrderRequest):
    name = " ".join(order.customer_name.split())
    phone = " ".join(order.phone.split())
    comment = " ".join(order.comment.split())

    total = 0
    order_lines = []
    saved_items = []

    for item in order.items:
        rows = await supabase_request(
            "GET",
            "products",
            params={
                "select": "*",
                "id": f"eq.{item.product_id}",
                "active": "eq.true",
                "limit": "1"
            }
        )

        if not rows:
            raise HTTPException(
                status_code=400,
                detail="Товар не найден или больше недоступен."
            )

        product = rows[0]
        price = int(product["price"])
        line_total = price * item.quantity
        total += line_total

        product_name = (
            product["name_uz"]
            if order.language == "uz"
            else product["name_ru"]
        )

        order_lines.append(
            f"• {product_name} × {item.quantity} — {format_sum(line_total)}"
        )

        saved_items.append({
            "product_id": item.product_id,
            "name": product_name,
            "quantity": item.quantity,
            "price": price,
            "line_total": line_total
        })

    now = datetime.now(
        ZoneInfo("Asia/Tashkent")
    ).strftime("%d.%m.%Y, %H:%M:%S")

    # Сохраняем заказ в Supabase.
    await supabase_request(
        "POST",
        "orders",
        json={
            "customer_name": name,
            "phone": phone,
            "comment": comment,
            "language": order.language,
            "items": saved_items,
            "total_price": total,
            "status": "new"
        },
        headers={"Prefer": "return=minimal"}
    )

    message = (
        "🧁 НОВЫЙ ЗАКАЗ — SUMA BAKERY\n\n"
        f"🕒 Время: {now}\n"
        f"👤 Клиент: {name}\n"
        f"📞 Телефон: {phone}\n\n"
        "🛒 ТОВАРЫ:\n"
        + "\n".join(order_lines)
        + "\n\n"
        f"💰 ИТОГО: {format_sum(total)}\n\n"
        f"💬 Комментарий: {comment or 'Нет'}"
    )

    telegram_sent = False

    if BOT_TOKEN and CHAT_ID:
        telegram_url = (
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        )

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    telegram_url,
                    json={"chat_id": CHAT_ID, "text": message}
                )

            result = response.json()
            telegram_sent = response.is_success and result.get("ok", False)

        except (httpx.HTTPError, ValueError):
            telegram_sent = False

    return {
        "ok": True,
        "message": "Заказ сохранён.",
        "telegram_sent": telegram_sent
    }
