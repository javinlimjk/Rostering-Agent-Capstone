from sqlalchemy import Column, Integer, String, Float
from app.database import Base

class StaffDB(Base):
    __tablename__ = "staff"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, index=True)
    role = Column(String)
    status = Column(String)
    contract_type = Column(String)
    country = Column(String)
    max_hours = Column(Integer)
    cost_per_hour = Column(Float)
    # comma separated string
    skills = Column(String, default="")

class RosterAssignmentDB(Base):
    __tablename__ = "roster_assignments"
    
    id = Column(Integer, primary_key=True, index=True)
    staff_id = Column(String, index=True)
    date = Column(String, index=True)
    shift_type = Column(String)
    created_at = Column(Float, index=True, nullable=True)

