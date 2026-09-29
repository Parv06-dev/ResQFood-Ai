"""
WhatsApp Webhook API — Twilio webhook for stateful conversation management.
Identifies users by phone number and routes to role-specific flows.
"""
from fastapi import APIRouter, Depends, Form, Response
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timezone

from app.database import get_db
from app.models.models import (
    User,
    UserSession,
    Kitchen,
    NGO,
    Driver,
    SurplusFood,
    DemandPrediction,
)
from app.config import settings

router = APIRouter(prefix="/api/whatsapp", tags=["WhatsApp"])


def _send_twiml(message: str) -> Response:
    """Return a TwiML response."""
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{message}</Message>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def _get_or_create_session(db: Session, user: User, phone: str) -> UserSession:
    session = db.query(UserSession).filter(UserSession.phone == phone).first()
    if not session:
        session = UserSession(
            user_id=user.id,
            phone=phone,
            role=user.role,
            current_state="idle",
            temporary_data={},
        )
        db.add(session)
        db.commit()
        db.refresh(session)
    return session


@router.post("/webhook")
async def whatsapp_webhook(
    From: str = Form(""),
    Body: str = Form(""),
    NumMedia: str = Form("0"),
    MediaUrl0: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Handle incoming WhatsApp messages from Twilio."""
    phone = From.replace("whatsapp:", "").strip()
    message = Body.strip().lower()

    # Find user by phone
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        return _send_twiml(
            "Welcome to ResQFood AI! Your phone number is not registered. "
            "Please register at our website first."
        )

    session = _get_or_create_session(db, user, phone)

    # Route based on role and state
    if message in ("hi", "hello", "menu", "start"):
        session.current_state = "idle"
        session.temporary_data = {}
        db.commit()
        return _send_twiml(_get_menu(user.role))

    if user.role == "kitchen":
        return _handle_kitchen(db, session, message, MediaUrl0)
    elif user.role == "ngo":
        return _handle_ngo(db, session, message)
    elif user.role == "driver":
        return _handle_driver(db, session, message)
    elif user.role == "admin":
        return _send_twiml(
            "ResQFood AI Admin Panel\n\n"
            "Please use the web dashboard for full admin features.\n"
            "Type 'status' for a quick system summary."
        )

    return _send_twiml("Type 'menu' for available options.")


def _get_menu(role: str) -> str:
    if role == "kitchen":
        return (
            "🍳 Kitchen Menu\n\n"
            "1️⃣ Report surplus food\n"
            "2️⃣ View my surplus\n"
            "3️⃣ Check predictions\n\n"
            "Reply with a number."
        )
    elif role == "ngo":
        return (
            "🏢 NGO Menu\n\n"
            "1️⃣ View available food\n"
            "2️⃣ My requests\n"
            "3️⃣ View OTP for delivery\n\n"
            "Reply with a number."
        )
    elif role == "driver":
        return (
            "🚗 Driver Menu\n\n"
            "1️⃣ My deliveries\n"
            "2️⃣ Current route\n\n"
            "Reply with a number."
        )
    return "Type 'menu' for options."


def _handle_kitchen(db: Session, session: UserSession, message: str, media_url: str = None) -> Response:
    state = session.current_state
    temp = session.temporary_data or {}

    if state == "idle" and message == "1":
        session.current_state = "surplus_name"
        session.temporary_data = {}
        db.commit()
        return _send_twiml("What food would you like to report?\n\nExample: Vegetable Biryani")

    elif state == "surplus_name":
        temp["food_name"] = message.title()
        session.current_state = "surplus_quantity"
        session.temporary_data = temp
        db.commit()
        return _send_twiml(f"How many meals of {temp['food_name']}?\n\nExample: 40")

    elif state == "surplus_quantity":
        try:
            temp["quantity"] = int(message)
        except ValueError:
            return _send_twiml("Please enter a valid number.")
        session.current_state = "surplus_weight"
        session.temporary_data = temp
        db.commit()
        return _send_twiml("Estimated weight in kg?\n\nExample: 12")

    elif state == "surplus_weight":
        try:
            temp["weight"] = float(message)
        except ValueError:
            return _send_twiml("Please enter a valid number.")
        session.current_state = "surplus_usable_hours"
        session.temporary_data = temp
        db.commit()
        return _send_twiml("How many hours is this food usable?\n\nExample: 3")

    elif state == "surplus_usable_hours":
        try:
            temp["usable_hours"] = float(message)
        except ValueError:
            return _send_twiml("Please enter a valid number of hours.")
        session.current_state = "surplus_confirm"
        session.temporary_data = temp
        db.commit()
        return _send_twiml(
            f"Please confirm:\n\n"
            f"🍽 {temp['food_name']}\n"
            f"📦 {temp['quantity']} meals\n"
            f"⚖️ {temp['weight']} kg\n"
            f"⏰ Usable for {temp['usable_hours']} hours\n\n"
            f"Type 'yes' to confirm or 'no' to cancel."
        )

    elif state == "surplus_confirm":
        if message == "yes":
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            kitchen = db.query(Kitchen).filter(Kitchen.user_id == session.user_id).first()
            if kitchen:
                surplus = SurplusFood(
                    kitchen_id=kitchen.id,
                    food_name=temp["food_name"],
                    quantity=temp["quantity"],
                    unit="meals",
                    estimated_weight_kg=temp.get("weight", 0),
                    prepared_at=now,
                    usable_until=now + timedelta(hours=temp.get("usable_hours", 3)),
                    remaining_quantity=temp["quantity"],
                    status="eligible",
                )
                db.add(surplus)
                db.commit()
                session.current_state = "idle"
                session.temporary_data = {}
                db.commit()
                return _send_twiml(
                    f"✅ Surplus registered!\n\n"
                    f"{temp['food_name']} - {temp['quantity']} meals\n"
                    f"NGOs will be notified. Type 'menu' for more options."
                )
            return _send_twiml("Error: Kitchen not found. Contact admin.")
        else:
            session.current_state = "idle"
            session.temporary_data = {}
            db.commit()
            return _send_twiml("Cancelled. Type 'menu' for options.")

    elif state == "idle" and message == "2":
        kitchen = db.query(Kitchen).filter(Kitchen.user_id == session.user_id).first()
        if kitchen:
            items = db.query(SurplusFood).filter(
                SurplusFood.kitchen_id == kitchen.id
            ).order_by(SurplusFood.created_at.desc()).limit(5).all()
            if items:
                lines = ["📦 Your Recent Surplus:\n"]
                for s in items:
                    lines.append(f"• {s.food_name} - {s.quantity} meals ({s.status})")
                return _send_twiml("\n".join(lines))
        return _send_twiml("No surplus found. Type 'menu' for options.")
    elif state == "idle" and message == "3":
        kitchen = db.query(Kitchen).filter(
            Kitchen.user_id == session.user_id
        ).first()

        if not kitchen:
            return _send_twiml(
                "Kitchen profile not found. Please contact admin."
            )

        prediction = db.query(DemandPrediction).filter(
            DemandPrediction.kitchen_id == kitchen.id
        ).order_by(
            DemandPrediction.created_at.desc()
        ).first()

        if not prediction:
            return _send_twiml(
                "📊 No prediction available yet.\n\n"
                "Please generate a demand prediction from the web dashboard first."
            )

        message_text = (
            "📊 Demand Prediction\n\n"
            f"📅 Date: {prediction.prediction_date}\n\n"
            f"🍽️ Predicted Demand: {prediction.predicted_demand} meals\n"
            f"🏭 Recommended Production: "
            f"{prediction.recommended_production} meals\n"
            f"📈 Historical Average: "
            f"{prediction.historical_average or 'N/A'} meals\n"
            f"🛡️ Safety Buffer: {prediction.safety_buffer} meals\n\n"
            f"🤖 Model: {prediction.model_type}"
        )

        if (
            prediction.confidence_lower is not None
            and prediction.confidence_upper is not None
        ):
            message_text += (
                f"\n\n📌 Expected Range: "
                f"{prediction.confidence_lower}–"
                f"{prediction.confidence_upper} meals"
            )

        return _send_twiml(message_text)

    return _send_twiml("Type 'menu' for options.")


def _handle_ngo(db: Session, session: UserSession, message: str) -> Response:
    if message == "1":
        items = db.query(SurplusFood).filter(
            SurplusFood.status.in_(["eligible", "urgent"])
        ).order_by(SurplusFood.created_at.desc()).limit(5).all()
        if items:
            lines = ["🍽 Available Food:\n"]
            for s in items:
                lines.append(f"• {s.food_name} - {s.remaining_quantity} meals ({s.status})")
            lines.append("\nVisit the website to request food.")
            return _send_twiml("\n".join(lines))
        return _send_twiml("No food available right now. We'll notify you!")

    elif message == "3":
        from app.models.models import DeliveryStop, DeliveryOTP
        ngo = db.query(NGO).filter(NGO.user_id == session.user_id).first()
        if ngo:
            stops = db.query(DeliveryStop).filter(
                DeliveryStop.ngo_id == ngo.id,
                DeliveryStop.status != "delivered",
            ).all()
            if stops:
                lines = ["🔑 Your Delivery OTPs:\n"]
                for stop in stops:
                    otp = db.query(DeliveryOTP).filter(DeliveryOTP.stop_id == stop.id).first()
                    if otp:
                        lines.append(f"Stop #{stop.sequence_order}: OTP = {otp.otp_code}")
                return _send_twiml("\n".join(lines))
        return _send_twiml("No active deliveries. Type 'menu' for options.")

    return _send_twiml("Type 'menu' for options.")


def _handle_driver(db: Session, session: UserSession, message: str) -> Response:
    from app.models.models import Delivery, DeliveryStop
    driver = db.query(Driver).filter(Driver.user_id == session.user_id).first()
    if not driver:
        return _send_twiml("Driver profile not found. Contact admin.")

    if message == "1":
        deliveries = db.query(Delivery).filter(
            Delivery.driver_id == driver.id
        ).order_by(Delivery.created_at.desc()).limit(3).all()
        if deliveries:
            lines = ["🚗 Your Deliveries:\n"]
            for d in deliveries:
                lines.append(f"Delivery #{d.id} - {d.total_meals} meals - {d.status}")
            return _send_twiml("\n".join(lines))
        return _send_twiml("No deliveries assigned. Type 'menu' for options.")

    return _send_twiml("Type 'menu' for options.")
