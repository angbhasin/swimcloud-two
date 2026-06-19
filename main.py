import os
from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from models import SessionLocal, init_db, User, SwimTime
from auth import hash_password, verify_password, create_token, get_current_user_id
from events import EVENTS, COURSES, ALL_EVENTS, RELAY_BASES, relay_leg_labels, relay_event_name, validate_time, parse_time, format_time
from swimcloud_import import parse_pasted_text
from featured_swims import FEATURED_SWIMS
from photo_import import extract_times_from_image

app = FastAPI()
templates = Jinja2Templates(directory="templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    uid = get_current_user_id(request)
    if uid is None:
        return None
    return db.query(User).filter(User.id == uid).first()


@app.on_event("startup")
def startup():
    init_db()


# ── Public: home / search ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, q: str = "", db: Session = Depends(get_db)):
    from datetime import date, timedelta
    user = get_current_user(request, db)
    swimmers = []
    if q:
        swimmers = db.query(User).filter(User.name.ilike(f"%{q}%")).all()

    total_swimmers = db.query(User).count()
    total_times = db.query(SwimTime).count()
    recent_users = db.query(User).order_by(User.id.desc()).limit(5).all()

    fastest = db.query(SwimTime, User.name).join(User, User.id == SwimTime.user_id)\
        .order_by(SwimTime.time_seconds.asc()).first()
    fastest_swim = None
    if fastest:
        t, name = fastest
        fastest_swim = {"name": name, "event": t.event, "course": t.course,
                        "time_str": format_time(t.time_seconds), "user_id": t.user_id}

    # Top swims: curated featured list only
    seen = set()
    recent_swims = []
    for s in sorted(FEATURED_SWIMS, key=lambda x: x["time_seconds"]):
        key = (s["swimmer_name"].lower(), s["event"], s["course"])
        if key in seen:
            continue
        seen.add(key)
        recent_swims.append(dict(s, user_id=None))
        if len(recent_swims) == 30:
            break

    # Week date range for display
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    date_range = f"{week_start.strftime('%B')} {week_start.day} – {week_end.strftime('%B')} {week_end.day}, {week_end.year}"

    scy_swims = [s for s in recent_swims if s["course"] == "SCY"][:10]
    lcm_swims = [s for s in recent_swims if s["course"] == "LCM"][:10]

    return templates.TemplateResponse(request, "home.html", {
        "user": user, "q": q,
        "swimmers": swimmers, "recent_swims": recent_swims,
        "scy_swims": scy_swims, "lcm_swims": lcm_swims,
        "date_range": date_range,
        "total_swimmers": total_swimmers, "total_times": total_times,
        "fastest_swim": fastest_swim, "recent_users": recent_users,
    })


@app.get("/swimmer/{swimmer_id}", response_class=HTMLResponse)
async def swimmer_profile(swimmer_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    swimmer = db.query(User).filter(User.id == swimmer_id).first()
    if not swimmer:
        raise HTTPException(status_code=404, detail="Swimmer not found")

    times_raw = db.query(SwimTime).filter(SwimTime.user_id == swimmer_id).all()
    times_by_course: dict[str, dict[str, str]] = {c: {} for c in COURSES}
    for t in times_raw:
        times_by_course[t.course][t.event] = format_time(t.time_seconds)

    is_owner = user is not None and user.id == swimmer_id
    return templates.TemplateResponse(request, "profile.html", {
        "user": user, "swimmer": swimmer,
        "events": EVENTS, "courses": COURSES,
        "all_events": ALL_EVENTS,
        "times_by_course": times_by_course, "is_owner": is_owner,
        "profile_success": None, "profile_error": None,
    })


@app.post("/profile/update", response_class=HTMLResponse)
async def update_profile(
    request: Request,
    name: str = Form(...),
    gender: str = Form(...),
    team1: str = Form(""),
    team2: str = Form(""),
    team3: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    error = None
    if not name.strip():
        error = "Name cannot be empty."
    elif gender not in ("male", "female"):
        error = "Please select a valid gender."

    times_raw = db.query(SwimTime).filter(SwimTime.user_id == user.id).all()
    times_by_course = {c: {} for c in COURSES}
    for t in times_raw:
        times_by_course[t.course][t.event] = format_time(t.time_seconds)

    if error:
        return templates.TemplateResponse(request, "profile.html", {
            "user": user, "swimmer": user,
            "events": EVENTS, "courses": COURSES, "all_events": ALL_EVENTS,
            "times_by_course": times_by_course, "is_owner": True,
            "profile_success": None, "profile_error": error,
        })

    user.name = name.strip()
    user.gender = gender
    user.team1 = team1.strip() or None
    user.team2 = team2.strip() or None
    user.team3 = team3.strip() or None
    db.commit()
    db.refresh(user)

    times_raw = db.query(SwimTime).filter(SwimTime.user_id == user.id).all()
    times_by_course = {c: {} for c in COURSES}
    for t in times_raw:
        times_by_course[t.course][t.event] = format_time(t.time_seconds)

    return templates.TemplateResponse(request, "profile.html", {
        "user": user, "swimmer": user,
        "events": EVENTS, "courses": COURSES, "all_events": ALL_EVENTS,
        "times_by_course": times_by_course, "is_owner": True,
        "profile_success": "Profile updated.", "profile_error": None,
    })


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    gender: str = Form(...),
    team1: str = Form(""),
    team2: str = Form(""),
    team3: str = Form(""),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(request, "register.html", {
            "error": "Email already registered."
        })
    if len(password) < 6:
        return templates.TemplateResponse(request, "register.html", {
            "error": "Password must be at least 6 characters."
        })
    if gender not in ("male", "female"):
        return templates.TemplateResponse(request, "register.html", {
            "error": "Please select a gender."
        })
    user = User(
        email=email, name=name.strip(), hashed_password=hash_password(password),
        gender=gender,
        team1=team1.strip() or None,
        team2=team2.strip() or None,
        team3=team3.strip() or None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    response = RedirectResponse(url=f"/swimmer/{user.id}", status_code=303)
    response.set_cookie("access_token", create_token(user.id), httponly=True, max_age=604800)
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request, "login.html", {
            "error": "Invalid email or password."
        })
    response = RedirectResponse(url=f"/swimmer/{user.id}", status_code=303)
    response.set_cookie("access_token", create_token(user.id), httponly=True, max_age=604800)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response


# ── Time management (authenticated) ───────────────────────────────────────────

@app.get("/my-times", response_class=HTMLResponse)
async def my_times_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    times_raw = db.query(SwimTime).filter(SwimTime.user_id == user.id).all()
    times_map: dict[str, dict[str, SwimTime]] = {c: {} for c in COURSES}
    for t in times_raw:
        times_map[t.course][t.event] = t

    return templates.TemplateResponse(request, "my_times.html", {
        "user": user,
        "events": EVENTS, "courses": COURSES, "times_map": times_map,
        "relay_bases": RELAY_BASES, "relay_leg_labels": relay_leg_labels,
        "relay_event_name": relay_event_name,
        "format_time": format_time, "error": None, "success": None,
    })


@app.post("/times/add", response_class=HTMLResponse)
async def add_time(
    request: Request,
    event: str = Form(...),
    course: str = Form(...),
    time_str: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    time_sec = parse_time(time_str)
    error = None
    if time_sec is None:
        error = "Invalid time format. Use SS.mm or M:SS.mm (e.g. 54.32 or 1:54.32)"
    else:
        error = validate_time(event, course, time_sec)
    if not error and event not in ALL_EVENTS:
        error = f"Unknown event: {event}"

    times_raw = db.query(SwimTime).filter(SwimTime.user_id == user.id).all()
    times_map: dict[str, dict[str, SwimTime]] = {c: {} for c in COURSES}
    for t in times_raw:
        times_map[t.course][t.event] = t

    if error:
        return templates.TemplateResponse(request, "my_times.html", {
            "user": user,
            "events": EVENTS, "courses": COURSES, "times_map": times_map,
            "relay_bases": RELAY_BASES, "relay_leg_labels": relay_leg_labels,
            "relay_event_name": relay_event_name,
            "format_time": format_time, "error": error, "success": None,
        })

    existing = db.query(SwimTime).filter_by(user_id=user.id, event=event, course=course).first()
    if existing:
        existing.time_seconds = time_sec
    else:
        db.add(SwimTime(user_id=user.id, event=event, course=course, time_seconds=time_sec))
    db.commit()

    times_raw = db.query(SwimTime).filter(SwimTime.user_id == user.id).all()
    times_map = {c: {} for c in COURSES}
    for t in times_raw:
        times_map[t.course][t.event] = t

    return templates.TemplateResponse(request, "my_times.html", {
        "user": user,
        "events": EVENTS, "courses": COURSES, "times_map": times_map,
        "relay_bases": RELAY_BASES, "relay_leg_labels": relay_leg_labels,
        "relay_event_name": relay_event_name,
        "format_time": format_time, "error": None,
        "success": f"Saved {course} {event}: {format_time(time_sec)}",
    })


@app.post("/times/delete/{time_id}")
async def delete_time(time_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    t = db.query(SwimTime).filter(SwimTime.id == time_id, SwimTime.user_id == user.id).first()
    if t:
        db.delete(t)
        db.commit()
    return RedirectResponse(url="/my-times", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "settings.html", {"user": user})


@app.post("/account/delete")
async def delete_account(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    db.query(SwimTime).filter(SwimTime.user_id == user.id).delete()
    db.delete(user)
    db.commit()
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response


@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "import.html", {
        "user": user,
        "results": None, "errors": None, "imported": None,
    })


@app.post("/import", response_class=HTMLResponse)
async def import_times(
    request: Request,
    pasted_text: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    results, errors = parse_pasted_text(pasted_text)

    imported = []
    for r in results:
        existing = db.query(SwimTime).filter_by(
            user_id=user.id, event=r["event"], course=r["course"]
        ).first()
        if existing:
            existing.time_seconds = r["time_seconds"]
        else:
            db.add(SwimTime(user_id=user.id, event=r["event"], course=r["course"], time_seconds=r["time_seconds"]))
        imported.append(r)
    if imported:
        db.commit()

    return templates.TemplateResponse(request, "import.html", {
        "user": user,
        "results": results, "errors": errors, "imported": imported,
        "format_time": format_time,
    })


@app.get("/photo-import", response_class=HTMLResponse)
async def photo_import_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "photo_import.html", {
        "user": user,
        "extracted": None, "errors": None, "saved": None,
    })


ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@app.post("/photo-import", response_class=HTMLResponse)
async def photo_import_upload(
    request: Request,
    photo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    media_type = photo.content_type or "image/jpeg"
    if media_type not in ALLOWED_MEDIA_TYPES:
        return templates.TemplateResponse(request, "photo_import.html", {
            "user": user,
            "extracted": None, "errors": [f"Unsupported file type: {media_type}. Use JPEG or PNG."],
            "saved": None,
        })

    image_bytes = await photo.read()
    extracted, errors = extract_times_from_image(image_bytes, media_type)
    return templates.TemplateResponse(request, "photo_import.html", {
        "user": user,
        "extracted": extracted, "errors": errors, "saved": None,
    })


@app.post("/photo-import/save", response_class=HTMLResponse)
async def photo_import_save(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    saved = []
    errors = []

    i = 0
    while f"event_{i}" in form:
        event = form.get(f"event_{i}")
        course = form.get(f"course_{i}")
        time_sec_str = form.get(f"time_seconds_{i}")
        time_str = form.get(f"time_str_{i}")
        include = form.get(f"include_{i}")

        if include and event and course and time_sec_str:
            try:
                time_sec = float(time_sec_str)
            except ValueError:
                errors.append(f"Invalid time value for {event}")
                i += 1
                continue

            existing = db.query(SwimTime).filter_by(user_id=user.id, event=event, course=course).first()
            if existing:
                existing.time_seconds = time_sec
            else:
                db.add(SwimTime(user_id=user.id, event=event, course=course, time_seconds=time_sec))
            saved.append({"event": event, "course": course, "time_str": time_str})
        i += 1

    if saved:
        db.commit()

    return templates.TemplateResponse(request, "photo_import.html", {
        "user": user,
        "extracted": None, "errors": errors if errors else None, "saved": saved,
    })
