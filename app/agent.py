import google.generativeai as genai
import os
import json
import re
from typing import Optional, Dict, Any
from app.state import RosterState
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from app.database import SessionLocal
from app.models_db import StaffDB, RosterAssignmentDB
from sqlalchemy.orm import Session

# --- Tools Description ---
def query_roster(start_date: str, end_date: str) -> str:
    """Fetch roster assignments from the database for a specific date range (YYYY-MM-DD)."""
    db: Session = SessionLocal()
    try:
        assignments = db.query(RosterAssignmentDB).filter(
            RosterAssignmentDB.date >= start_date,
            RosterAssignmentDB.date <= end_date
        ).all()
        return json.dumps([{"staff_id": a.staff_id, "date": a.date, "shift_type": a.shift_type} for a in assignments])
    except Exception as e:
        return f"Error querying roster: {str(e)}"
    finally:
        db.close()


def query_staff_db() -> str:
    """Fetch details of all staff members currently in the workforce database."""
    db: Session = SessionLocal()
    try:
        staff = db.query(StaffDB).all()
        return json.dumps([{"id": s.id, "name": s.name, "role": s.role} for s in staff])
    except Exception as e:
        return f"Error querying staff: {str(e)}"
    finally:
        db.close()


def execute_shift_swap(staff_id_1: str, staff_id_2: str, date: str) -> str:
    """Swap shift assignments between two staff members on a specific date (YYYY-MM-DD)."""
    db: Session = SessionLocal()
    try:
        a1 = db.query(RosterAssignmentDB).filter_by(staff_id=staff_id_1, date=date).first()
        a2 = db.query(RosterAssignmentDB).filter_by(staff_id=staff_id_2, date=date).first()
        if not a1 or not a2:
            return f"Error: Assignments not found for {staff_id_1} or {staff_id_2} on {date}"
        
        a1.shift_type, a2.shift_type = a2.shift_type, a1.shift_type
        db.commit()
        return f"Success: Swapped {staff_id_1} and {staff_id_2} on {date}"
    except Exception as e:
        db.rollback()
        return f"Error executing swap: {str(e)}"
    finally:
        db.close()


def update_shift_type(staff_id: str, date: str, new_shift_type: str) -> str:
    """Update a specific staff member's shift for a given date (YYYY-MM-DD)."""
    db: Session = SessionLocal()
    try:
        a = db.query(RosterAssignmentDB).filter_by(staff_id=staff_id, date=date).first()
        if not a:
            return f"Error: Assignment not found for {staff_id} on {date}"
        
        old = a.shift_type
        a.shift_type = new_shift_type
        db.commit()
        return f"Success: Updated {staff_id} on {date} from {old} to {new_shift_type}"
    except Exception as e:
        db.rollback()
        return f"Error updating shift: {str(e)}"
    finally:
        db.close()

def check_roster_compliance(country: str = "Singapore") -> str:
    """Run an automated compliance audit on the current roster to detect labor law violations."""
    from app.optimizer import RosterOptimizer
    from app.rules import get_rules_for_country
    from app.models import RosterAssignment, Staff, ShiftConfig
    import datetime

    db: Session = SessionLocal()
    try:
        # Fetch current roster from DB
        db_assignments = db.query(RosterAssignmentDB).all()
        # Fetch staff from DB
        db_staff = db.query(StaffDB).all()
        
        staff_list = [Staff(
            id=s.id, 
            name=s.name, 
            role=s.role, 
            country=s.country or country,
            max_hours=float(s.max_hours or 44),
            cost_per_hour=float(s.cost_per_hour or 25)
        ) for s in db_staff]
        
        assignments = []
        for a in db_assignments:
            try:
                d = datetime.date.fromisoformat(a.date)
                assignments.append(RosterAssignment(
                    staff_id=a.staff_id,
                    date=d,
                    shift_type=a.shift_type,
                    shift_id="agent_check"
                ))
            except: continue

        # Derive shift configs from assignments or use standard defaults
        # For a full compliance check, the optimizer needs shift definitions (start/end times).
        # We provide standard ones if not available.
        shift_defs = [
            {"Name": "Morning", "Start Time": 800, "Duration": 8.0, "Staff Needed": 1},
            {"Name": "Afternoon", "Start Time": 1400, "Duration": 8.0, "Staff Needed": 1},
            {"Name": "Night", "Start Time": 2200, "Duration": 8.0, "Staff Needed": 1},
            {"Name": "Morning Shift", "Start Time": 800, "Duration": 8.0, "Staff Needed": 1},
            {"Name": "Afternoon Shift", "Start Time": 1400, "Duration": 8.0, "Staff Needed": 1},
            {"Name": "Night Shift", "Start Time": 2200, "Duration": 8.0, "Staff Needed": 1}
        ]

        rules = get_rules_for_country(country)
        opt = RosterOptimizer(staff_list, [], rules)
        errors = opt.validate_roster(assignments, shift_defs)

        
        if not errors:
            return "Compliance Check: No violations found. The roster is fully compliant."
        
        return json.dumps({
            "status": "Violations Found",
            "count": len(errors),
            "errors": [e['message'] for e in errors[:10]] # Return first 10
        })
    except Exception as e:
        return f"Error running compliance check: {str(e)}"
    finally:
        db.close()



class SchedulingAgent:
    """
    A conversational agent powered by Google Gemini to assist in roster configuration.
    Features native function calling to interact with the system database.
    """

    def __init__(self):
        # Configure API key
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            print("Warning: GOOGLE_API_KEY environment variable not found.")

        if api_key:
            genai.configure(api_key=api_key)

        # Using gemini-flash-latest as the verified stable agent with tool support.
        self.model_name = "gemini-flash-latest"
        self.tools = [query_roster, query_staff_db, execute_shift_swap, update_shift_type, check_roster_compliance]
        self.model = genai.GenerativeModel(self.model_name, tools=self.tools)





    def process_message(self, user_text: str, current_state_dict: Optional[Dict[str, Any]] = None) -> dict:
        """
        Process a natural language message from the user, updating the roster state and executing tools if needed.
        """
        # Load state
        if current_state_dict:
            try:
                state = RosterState(**current_state_dict)
            except Exception:
                state = RosterState()
        else:
            state = RosterState()

        missing = state.missing_fields()

        # System Instruction for the LLM
        system_prompt = f"""
        You are a highly capable AI Roster Copilot.
        You have direct access to the system database via TOOLS. Use them to provide accurate answers.

        ### CURRENT DRAFT CONTEXT (JSON)
        {state.model_dump_json(indent=2)}

        ### INSTRUCTIONS
        1. **Tool Usage**:
           - If a user asks "Who is working on X date?" or "Show me the roster", use `query_roster`.
           - If a user asks to "swap" or "change" someone's shift, use `execute_shift_swap` or `update_shift_type`.
           - Always verify your actions by querying the roster again if requested.

        2. **Draft State Management**:
           - If the user provides parameters for the NEXT roster draft (Location, Start Date, shifts), update the `updated_state`.
           - Return the `updated_state` in your final JSON output.

        3. **Conversational Logic**:
           - Acknowledge your tool actions clearly.
           - If info is missing for a draft (Location, Dates, Shifts), ask politely.

        4. **Output Format**:
           You MUST conclude every interaction by returning a JSON object.
           ```json
           {{
             "updated_state": {{ ... }},
             "reply": "Your natural language response",
             "is_complete": boolean,
             "action": "FORECAST" | "GENERATE" | null
           }}
           ```
        """

        try:
            sanitized_user_text = re.sub(r'</\s*user_input\s*>', '', user_text, flags=re.IGNORECASE)
            
            # Start a chat session to allow for tool-calling loops
            chat = self.model.start_chat(enable_automatic_function_calling=True)
            
            # First, send the system context as a message or use specialized instruction
            # For simplicity in this flow, we'll prepend the system prompt
            full_user_msg = f"{system_prompt}\n\nUser Input: {sanitized_user_text}"
            
            import time
            max_retries = 3
            retry_delay = 2
            response = None
            
            for attempt in range(max_retries):
                try:
                    response = chat.send_message(full_user_msg)
                    break 
                except Exception as e:
                    if "429" in str(e) and attempt < max_retries - 1:
                        print(f"DEBUG: Hit 429, retrying in {retry_delay}s... (Attempt {attempt+1}/{max_retries})")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    raise e

            if not response:
                raise Exception("Failed to get response from Gemini API")

            text = response.text.strip()

            # The model might return the JSON inside markdown or raw
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            result = json.loads(text)
            print(f"DEBUG: Agent raw result: {result}")

            # Validate return structure
            raw_updated_state = result.get("updated_state", state.model_dump())
            try:
                validated_state = RosterState(**raw_updated_state)
                updated_state_dict = validated_state.model_dump()
            except Exception as e:
                print(f"DEBUG: State validation error: {e}")
                updated_state_dict = state.model_dump()

            print(f"DEBUG: Final updated_state: {updated_state_dict}")

            return {
                "reply": result.get("reply", "I processed your request."),
                "updated_state": updated_state_dict,
                "is_complete": result.get("is_complete", False),
                "action": result.get("action", None)
            }

        except Exception as e:
            print(f"Agent Chat Error: {e}")
            return {
                "reply": f"Internal Copilot Error: {str(e)}",
                "updated_state": state.model_dump(),
                "is_complete": False,
                "action": None
            }


    def analyze_demand_events(self, location: str, month_year: str) -> list:
        """
        Uses the LLM to analyze the given location and date for real-world events, 
        holidays, or weather patterns that could impact passenger volume or staff availability.
        Returns a list of Insight dictionaries.
        """
        prompt = f"""
        You are an expert Aviation and Logistics Demand Forecaster Oracle.
        Analyze the following parameters:
        Location: {location}
        Date Period: {month_year}

        Identify any major real-world public holidays, cultural events, sports events (e.g. F1), or seasonal weather patterns (e.g. Monsoons, Typhoons) that happen at this location during this specific date period.
        
        For each significant event you find, estimate its impact on airport flight volumes and staff leave contention.
        
        Return a JSON array of insight objects. If no major events are found, return an empty array `[]`.
        Each object MUST have this exact schema:
        {{
            "id": "unique_string_id",
            "title": "Short succinct title (e.g., 'F1 Singapore Grand Prix')",
            "description": "1-2 sentence explanation of why this matters for staffing and flights.",
            "type": "event" | "holiday" | "weather",
            "severity": "high" | "critical" | "medium",
            "suggested_signals": {{
                "flightVolumeSurge": number (e.g., 15.0 for +15%),
                "weatherDelayProb": number (e.g., 0.4 for 40% chance, 0 if not weather),
                "holidayLoad": number (e.g., 1.15 for 15% more base staff, 1.0 if no change)
            }}
        }}
        """

        try:
            response = self.model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            text = response.text.strip()
            # Clean markdown codeblocks if they exist
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("\n", 1)[0]
                
            insights = json.loads(text)
            if not isinstance(insights, list):
                insights = [insights] # Ensure array
                
            return insights
            
        except Exception as e:
            print(f"Oracle LLM error: {e}")
            return []
