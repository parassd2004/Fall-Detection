"""
IoT Fall Detection System
Backend API — FastAPI + SQL Server
Pokhara University Final Year Project

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Environment variables required:
    GMAIL_APP_PASSWORD   — Gmail app password for email alerts
    SENDER_EMAIL         — Sender Gmail address (optional, has default)
"""

import os
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

import pyodbc
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr

from email_service import send_otp_email, send_fall_alert_email

# ── App setup ────────────────────────────────────────────────
app = FastAPI(
    title="IoT Fall Detection API",
    description="Backend for ESP32-based fall detection system",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500", "http://localhost:5500"],          # Tighten to your domain before production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Password hashing ─────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Database connection factory ──────────────────────────────
DB_CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost;"
    "DATABASE=iot_system;"
    "Trusted_Connection=yes;"
)

@contextmanager
def get_db():
    """
    Context manager for database connections.
    Each request gets its own connection — no shared state.
    """
    conn   = pyodbc.connect(DB_CONN_STR)
    cursor = conn.cursor()
    try:
        yield conn, cursor
    finally:
        cursor.close()
        conn.close()

# ── In-memory OTP stores (keyed by email) ───────────────────
# Structure: { email: { "otp": "123456", "time": float, "password": str } }
reg_otp_store   = {}
reset_otp_store = {}

# ── Simple session store ─────────────────────────────────────
# Structure: { token: { "email": str, "user_id": int, "expires": float } }
sessions = {}

OTP_EXPIRE_SECONDS   = 120   # 2 minutes
SESSION_EXPIRE_HOURS = 24

# ── Helpers ──────────────────────────────────────────────────
def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email))

def generate_otp() -> str:
    return str(random.randint(100000, 999999))

def generate_token() -> str:
    import secrets
    return secrets.token_hex(32)

def get_current_user(authorization: Optional[str] = Header(None)):
    """
    Dependency — validates session token from Authorization header.
    Usage: user = Depends(get_current_user)
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.split(" ", 1)[1]
    session = sessions.get(token)

    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if time.time() > session["expires"]:
        del sessions[token]
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")

    return session   # { "email": ..., "user_id": ..., "expires": ... }

# ── Pydantic models ──────────────────────────────────────────
class RegisterRequest(BaseModel):
    email:    str
    password: str
    full_name: Optional[str] = None

class OTPVerifyRequest(BaseModel):
    email: str
    otp:   str

class LoginRequest(BaseModel):
    email:    str
    password: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    email:        str
    otp:          str
    new_password: str

class SensorReading(BaseModel):
    acc:  float
    gyro: float
    fall: int   # 0 or 1 from ESP32

class ResendOTPRequest(BaseModel):
    email: str

# ════════════════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════════════════

@app.post("/register", tags=["Auth"])
def register(req: RegisterRequest):
    """
    Step 1 of registration.
    Validates email, checks for duplicates, sends OTP.
    """
    if not is_valid_email(req.email):
        raise HTTPException(400, "Invalid email format")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    with get_db() as (conn, cursor):
        cursor.execute("SELECT id FROM users WHERE email = ?", (req.email,))
        if cursor.fetchone():
            raise HTTPException(400, "An account with this email already exists")

    otp = generate_otp()
    reg_otp_store[req.email] = {
        "otp":       otp,
        "password":  req.password,
        "full_name": req.full_name or "",
        "time":      time.time()
    }

    sent = send_otp_email(req.email, otp, purpose="account verification")
    if not sent:
        del reg_otp_store[req.email]
        raise HTTPException(500, "Failed to send OTP email. Check server email config.")

    return {"message": "OTP sent to your email. Valid for 2 minutes."}


@app.post("/verify-otp", tags=["Auth"])
def verify_otp(req: OTPVerifyRequest):
    """
    Step 2 of registration.
    Validates OTP and creates the user account with hashed password.
    """
    if req.email not in reg_otp_store:
        raise HTTPException(400, "No OTP requested for this email. Please register first.")

    stored = reg_otp_store[req.email]

    if time.time() - stored["time"] > OTP_EXPIRE_SECONDS:
        del reg_otp_store[req.email]
        raise HTTPException(400, "OTP expired. Please register again.")

    if stored["otp"] != req.otp:
        raise HTTPException(400, "Incorrect OTP")

    hashed_password = pwd_context.hash(stored["password"])

    with get_db() as (conn, cursor):
        cursor.execute(
            "INSERT INTO users (email, password, full_name, is_verified) VALUES (?, ?, ?, 1)",
            (req.email, hashed_password, stored["full_name"])
        )
        conn.commit()

    del reg_otp_store[req.email]
    return {"message": "Account verified successfully. You can now log in."}


@app.post("/resend-otp", tags=["Auth"])
def resend_otp(req: ResendOTPRequest):
    """Resend registration OTP."""
    if req.email not in reg_otp_store:
        raise HTTPException(400, "No pending registration for this email. Please register first.")

    otp = generate_otp()
    reg_otp_store[req.email]["otp"]  = otp
    reg_otp_store[req.email]["time"] = time.time()

    sent = send_otp_email(req.email, otp, purpose="account verification")
    if not sent:
        raise HTTPException(500, "Failed to resend OTP. Check email configuration.")

    return {"message": "New OTP sent to your email."}


@app.post("/login", tags=["Auth"])
def login(req: LoginRequest):
    """
    Authenticates user and returns a session token.
    Token must be sent in Authorization: Bearer <token> header.
    """
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT id, email, password, is_verified, full_name FROM users WHERE email = ?",
            (req.email,)
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(404, "No account found with this email")
    if not row[3]:
        raise HTTPException(403, "Account not verified. Please check your email for the OTP.")
    if not pwd_context.verify(req.password, row[2]):
        raise HTTPException(401, "Incorrect password")

    # Update last_login
    with get_db() as (conn, cursor):
        cursor.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now(), row[0])
        )
        conn.commit()

    token = generate_token()
    sessions[token] = {
        "email":   row[1],
        "user_id": row[0],
        "name":    row[4] or "",
        "expires": time.time() + (SESSION_EXPIRE_HOURS * 3600)
    }

    return {
        "message":  "Login successful",
        "token":    token,
        "email":    row[1],
        "name":     row[4] or "",
        "user_id":  row[0]
    }


@app.post("/logout", tags=["Auth"])
def logout(user=Depends(get_current_user), authorization: Optional[str] = Header(None)):
    """Invalidates the session token."""
    if authorization:
        token = authorization.split(" ", 1)[1]
        sessions.pop(token, None)
    return {"message": "Logged out successfully"}


@app.post("/forgot-password", tags=["Auth"])
def forgot_password(req: ForgotPasswordRequest):
    """Sends password reset OTP to registered email."""
    with get_db() as (conn, cursor):
        cursor.execute("SELECT id FROM users WHERE email = ?", (req.email,))
        if not cursor.fetchone():
            raise HTTPException(404, "No account found with this email")

    otp = generate_otp()
    reset_otp_store[req.email] = {"otp": otp, "time": time.time()}

    sent = send_otp_email(req.email, otp, purpose="password reset")
    if not sent:
        raise HTTPException(500, "Failed to send reset OTP.")

    return {"message": "Password reset OTP sent to your email."}


@app.post("/reset-password", tags=["Auth"])
def reset_password(req: ResetPasswordRequest):
    """Validates reset OTP and updates password."""
    if req.email not in reset_otp_store:
        raise HTTPException(400, "No password reset requested for this email.")

    stored = reset_otp_store[req.email]
    if time.time() - stored["time"] > OTP_EXPIRE_SECONDS:
        del reset_otp_store[req.email]
        raise HTTPException(400, "OTP expired. Please request a new one.")
    if stored["otp"] != req.otp:
        raise HTTPException(400, "Incorrect OTP")
    if len(req.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    hashed = pwd_context.hash(req.new_password)

    with get_db() as (conn, cursor):
        cursor.execute(
            "UPDATE users SET password = ? WHERE email = ?",
            (hashed, req.email)
        )
        conn.commit()

    del reset_otp_store[req.email]
    return {"message": "Password updated successfully. You can now log in."}


# ════════════════════════════════════════════════════════════
# SENSOR ROUTES — Called by ESP32
# ════════════════════════════════════════════════════════════

@app.post("/sensor", tags=["Sensor"])
def receive_sensor_data(data: SensorReading, user=Depends(get_current_user)):
    """
    Receives sensor readings from ESP32.
    Requires a valid session token (set once in ESP32 after caregiver logs in via web).
    Stores reading, logs fall events, and triggers email on fall.
    """
    user_id = user["user_id"]
    email   = user["email"]

    with get_db() as (conn, cursor):
        # Always store the sensor reading
        cursor.execute(
            "INSERT INTO sensor_data (user_id, acc, gyro, fall) VALUES (?, ?, ?, ?)",
            (user_id, data.acc, data.gyro, data.fall)
        )

        email_sent_flag = 0

        # On confirmed fall: log to fall_events + send email
        if data.fall == 1:
            # Check: was there a fall in the last 30 seconds? (de-duplicate alerts)
            cursor.execute("""
                SELECT TOP 1 id FROM fall_events
                WHERE user_id = ? AND time >= DATEADD(SECOND, -30, GETDATE())
                ORDER BY time DESC
            """, (user_id,))
            recent_fall = cursor.fetchone()

            if not recent_fall:
                # First fall in this window — send alert
                sent = send_fall_alert_email(email, data.acc, data.gyro)
                email_sent_flag = 1 if sent else 0

                cursor.execute(
                    "INSERT INTO fall_events (user_id, acc, gyro, email_sent) VALUES (?, ?, ?, ?)",
                    (user_id, data.acc, data.gyro, email_sent_flag)
                )
                print(f"[FALL] User {email} | acc={data.acc} | gyro={data.gyro} | email={'sent' if sent else 'failed'}")
            else:
                print(f"[FALL] Duplicate suppressed for user {email}")

        conn.commit()

    return {"status": "ok", "fall": bool(data.fall)}


# ════════════════════════════════════════════════════════════
# DASHBOARD DATA ROUTES — Called by Frontend
# ════════════════════════════════════════════════════════════

@app.get("/latest", tags=["Dashboard"])
def get_latest(user=Depends(get_current_user)):
    """Returns the single most recent sensor reading for this user."""
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT TOP 1 acc, gyro, fall, time
            FROM sensor_data
            WHERE user_id = ?
            ORDER BY id DESC
        """, (user["user_id"],))
        row = cursor.fetchone()

    if not row:
        return {"status": "no_data"}

    return {
        "acc":  round(row[0], 3),
        "gyro": round(row[1], 3),
        "fall": int(row[2]),
        "time": str(row[3])
    }


@app.get("/sensor/history", tags=["Dashboard"])
def get_sensor_history(limit: int = 30, user=Depends(get_current_user)):
    """Returns last N sensor readings for chart display."""
    limit = min(max(limit, 5), 100)   # Clamp between 5 and 100

    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT TOP (?) acc, gyro, fall, time
            FROM sensor_data
            WHERE user_id = ?
            ORDER BY id DESC
        """, (limit, user["user_id"]))
        rows = cursor.fetchall()

    # Reverse so chart shows oldest → newest
    return [
        {"acc": round(r[0], 3), "gyro": round(r[1], 3),
         "fall": int(r[2]), "time": str(r[3])}
        for r in reversed(rows)
    ]


@app.get("/falls", tags=["Dashboard"])
def get_fall_history(limit: int = 10, user=Depends(get_current_user)):
    """Returns confirmed fall events for this user."""
    limit = min(max(limit, 1), 50)

    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT TOP (?) acc, gyro, time, email_sent
            FROM fall_events
            WHERE user_id = ?
            ORDER BY time DESC
        """, (limit, user["user_id"]))
        rows = cursor.fetchall()

    return [
        {
            "acc":        round(r[0], 3),
            "gyro":       round(r[1], 3),
            "time":       str(r[2]),
            "email_sent": bool(r[3])
        }
        for r in rows
    ]


@app.get("/stats", tags=["Dashboard"])
def get_stats(user=Depends(get_current_user)):
    """Returns summary statistics for the dashboard header cards."""
    uid = user["user_id"]
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT COUNT(*) FROM sensor_data WHERE user_id = ?", (uid,))
        total_readings = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM fall_events WHERE user_id = ?", (uid,))
        total_falls = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM fall_events
            WHERE user_id = ? AND time >= CAST(GETDATE() AS DATE)
        """, (uid,))
        falls_today = cursor.fetchone()[0]

        cursor.execute("""
            SELECT TOP 1 time FROM sensor_data
            WHERE user_id = ? ORDER BY id DESC
        """, (uid,))
        last_row = cursor.fetchone()
        last_seen = str(last_row[0]) if last_row else None

    return {
        "total_readings": total_readings,
        "total_falls":    total_falls,
        "falls_today":    falls_today,
        "last_seen":      last_seen
    }


@app.get("/me", tags=["Auth"])
def get_profile(user=Depends(get_current_user)):
    """Returns current logged-in user's profile info."""
    return {
        "email":   user["email"],
        "name":    user["name"],
        "user_id": user["user_id"]
    }


@app.get("/health", tags=["System"])
def health():
    """Simple health check endpoint."""
    return {"status": "online", "time": str(datetime.now())}
