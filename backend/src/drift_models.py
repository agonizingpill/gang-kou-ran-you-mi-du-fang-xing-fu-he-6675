"""Rolling per-grade density drift monitoring: one bottle passing cannot hide a whole batch shift."""
from decimal import Decimal
from sqlalchemy import String,Numeric,Float,Boolean,ForeignKey,JSON,Integer,CheckConstraint,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base


class GradeDrift(Base):
    """One gate row per fuel grade; locked in the same order by evaluation and release."""
    __tablename__='grade_drifts'
    __table_args__=(CheckConstraint("state IN ('normal','alerted')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    grade:Mapped[str]=mapped_column(String(20),unique=True)
    state:Mapped[str]=mapped_column(String(20),default='normal')
    active_alert_id:Mapped[int|None]=mapped_column(Integer,nullable=True)
    cursor_sample_id:Mapped[int]=mapped_column(Integer,default=0)
    last_evaluated_at:Mapped[str]=mapped_column(String(40),default='')
    updated_at:Mapped[str]=mapped_column(String(40),default='')


class DriftWindow(Base):
    """Rolling window of already-reviewed business samples for one grade."""
    __tablename__='drift_windows'
    __table_args__=(UniqueConstraint('grade','seq'),CheckConstraint("state IN ('open','closed')"))
    id:Mapped[int]=mapped_column(primary_key=True)
    grade:Mapped[str]=mapped_column(String(20))
    seq:Mapped[int]=mapped_column(Integer)
    state:Mapped[str]=mapped_column(String(20),default='open')
    cursor_sample_id:Mapped[int]=mapped_column(Integer,default=0)
    sample_count:Mapped[int]=mapped_column(Integer,default=0)
    tank_count:Mapped[int]=mapped_column(Integer,default=0)
    evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    evaluated_at:Mapped[str]=mapped_column(String(40),default='')
    opened_at:Mapped[str]=mapped_column(String(40),default='')
    closed_at:Mapped[str|None]=mapped_column(String(40),nullable=True)


class DriftWindowSample(Base):
    """Permanent inclusion ledger: a sample is never counted twice, even after retries or window slides.

    ``state`` is ``active`` while counted, ``slid`` once rotated out of the rolling
    window, and ``cleared`` when the supervisor cleared the alert for its window.
    """
    __tablename__='drift_window_samples'
    __table_args__=(UniqueConstraint('sample_id'),CheckConstraint("state IN ('active','slid','cleared')"))
    id:Mapped[int]=mapped_column(primary_key=True)
    window_id:Mapped[int]=mapped_column(ForeignKey('drift_windows.id'))
    sample_id:Mapped[int]=mapped_column(ForeignKey('fuel_samples.id'),unique=True)
    tank_id:Mapped[int]=mapped_column(ForeignKey('tanks.id'))
    density_15:Mapped[Decimal]=mapped_column(Numeric(10,3))
    included_at:Mapped[str]=mapped_column(String(40))
    state:Mapped[str]=mapped_column(String(20),default='active')


class DriftAlert(Base):
    __tablename__='drift_alerts'
    __table_args__=(CheckConstraint("state IN ('active','cleared')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    grade:Mapped[str]=mapped_column(String(20))
    window_id:Mapped[int]=mapped_column(ForeignKey('drift_windows.id'))
    state:Mapped[str]=mapped_column(String(20),default='active')
    sample_count:Mapped[int]=mapped_column(Integer,default=0)
    tank_count:Mapped[int]=mapped_column(Integer,default=0)
    shift:Mapped[float|None]=mapped_column(Float,nullable=True)
    z:Mapped[float|None]=mapped_column(Float,nullable=True)
    evidence:Mapped[dict]=mapped_column(JSON)
    detected_at:Mapped[str]=mapped_column(String(40))
    cleared_by:Mapped[str|None]=mapped_column(String(120),nullable=True)
    cleared_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    clear_reason:Mapped[str]=mapped_column(String(600),default='')
    watermark_sample_id:Mapped[int|None]=mapped_column(Integer,nullable=True)
