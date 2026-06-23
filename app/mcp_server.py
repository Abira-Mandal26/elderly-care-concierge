import json
import os
import time
import uuid
from datetime import datetime
# pyrefly: ignore [missing-import]
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ElderlyCareServer")

DB_FILE = os.path.join(os.path.dirname(__file__), "..", ".adk", "database.json")

def load_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    if not os.path.exists(DB_FILE):
        default_data = {
            "medications": [
                {"name": "Lisinopril", "dosage": "10mg", "time": "8:00 AM"},
                {"name": "Metformin", "dosage": "500mg", "time": "8:00 PM"},
                {"name": "Atorvastatin", "dosage": "20mg", "time": "8:00 PM"}
            ],
            "logs": [],
            "appointments": [
                {"doctor": "Dr. Davis", "specialty": "Primary Care", "datetime_str": "Next Monday at 10:00 AM", "reason": "Annual Wellness Exam"}
            ]
        }
        with open(DB_FILE, "w") as f:
            json.dump(default_data, f, indent=2)
        return default_data
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

@mcp.tool()
def get_medications() -> str:
    """Get the active list of medications and their schedules.
    
    Returns:
        A JSON string representing the list of medications.
    """
    db = load_db()
    return json.dumps(db["medications"], indent=2)

@mcp.tool()
def log_medication(name: str) -> str:
    """Log that a medication has been taken.
    
    Args:
        name: The name of the medication taken.
        
    Returns:
        A confirmation message.
    """
    db = load_db()
    found = False
    for med in db["medications"]:
        if med["name"].lower() == name.lower():
            found = True
            break
    
    log_entry = {
        "medication": name,
        "time": "Just now"
    }
    db["logs"].append(log_entry)
    save_db(db)
    
    if found:
        return f"Successfully logged intake of {name}."
    else:
        return f"Logged intake of {name} (Note: {name} is not on the active medication list)."

@mcp.tool()
def get_appointments() -> str:
    """Get the list of upcoming doctor appointments.
    
    Returns:
        A JSON string representing the list of appointments.
    """
    db = load_db()
    return json.dumps(db["appointments"], indent=2)

@mcp.tool()
def add_appointment(doctor: str, specialty: str, datetime_str: str, reason: str) -> str:
    """Add/schedule a new doctor appointment.
    
    Args:
        doctor: Name of the doctor.
        specialty: Specialty of the doctor.
        datetime_str: Date and time of the appointment.
        reason: Reason for the visit.
        
    Returns:
        A confirmation message.
    """
    db = load_db()
    appt = {
        "doctor": doctor,
        "specialty": specialty,
        "datetime_str": datetime_str,
        "reason": reason
    }
    db["appointments"].append(appt)
    save_db(db)
    return f"Successfully scheduled appointment with {doctor} ({specialty}) for {datetime_str}."

if __name__ == "__main__":
    mcp.run()
