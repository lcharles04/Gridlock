import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
    text,
    update,
)

PROJECT_COLUMNS = [
    "id",
    "utility",
    "name",
    "kind",
    "voltage_kv",
    "start_year",
    "end_year",
    "geometry_wkt",
]

_metadata = MetaData()
_projects = Table(
    "projects",
    _metadata,
    Column("id", String(100), primary_key=True),
    Column("utility", String(100), nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", String(40), nullable=False),
    Column("voltage_kv", Integer, nullable=False),
    Column("start_year", Integer, nullable=False),
    Column("end_year", Integer, nullable=False),
    Column("geometry_wkt", Text, nullable=False),
)
_opportunities = Table(
    "opportunities",
    _metadata,
    Column("opportunity_key", String(220), primary_key=True),
    Column("project_a_id", String(100), nullable=False),
    Column("project_a_name", Text, nullable=False),
    Column("project_b_id", String(100), nullable=False),
    Column("project_b_name", Text, nullable=False),
    Column("distance_km", Float, nullable=False),
    Column("estimated_savings", Integer, nullable=False),
    Column("status", String(40), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
_events = Table(
    "coordination_events",
    _metadata,
    Column("event_id", String(36), nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("opportunity_key", String(220), nullable=False),
    Column("project_a_name", Text, nullable=False),
    Column("project_b_name", Text, nullable=False),
    Column("event_type", String(40), nullable=False),
    Column("status", String(40), nullable=False),
    Column("estimated_savings", Integer, nullable=False),
    PrimaryKeyConstraint("event_time", "event_id"),
)


def configured_database_url():
    configured_url = os.getenv("TIGERDATA_URL") or os.getenv("DATABASE_URL")
    if configured_url:
        if configured_url.startswith("postgres://"):
            return configured_url.replace("postgres://", "postgresql+psycopg://", 1)
        if configured_url.startswith("postgresql://"):
            return configured_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return configured_url

    database_path = Path(__file__).resolve().parent / "data" / "gridlock.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{database_path}"


@lru_cache(maxsize=4)
def get_engine(database_url):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


def initialize_database(engine, seed_path):
    _metadata.create_all(engine)

    timescale_enabled = False
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            timescale_enabled = bool(
                connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_extension "
                        "WHERE extname = 'timescaledb')"
                    )
                )
            )
            if timescale_enabled:
                connection.execute(
                    text(
                        "SELECT create_hypertable("
                        "'coordination_events', 'event_time', if_not_exists => TRUE)"
                    )
                )

    with engine.begin() as connection:
        project_count = connection.scalar(select(func.count()).select_from(_projects))
        if not project_count:
            seed_projects = pd.read_csv(seed_path)[PROJECT_COLUMNS].to_dict("records")
            connection.execute(insert(_projects), seed_projects)

    return timescale_enabled


def load_project_records(engine):
    with engine.connect() as connection:
        rows = connection.execute(
            select(*[_projects.c[column] for column in PROJECT_COLUMNS]).order_by(
                _projects.c.utility, _projects.c.id
            )
        ).mappings()
        return pd.DataFrame.from_records(rows, columns=PROJECT_COLUMNS)


def replace_project_records(engine, frame):
    records = frame[PROJECT_COLUMNS].to_dict("records")
    with engine.begin() as connection:
        connection.execute(delete(_projects))
        if records:
            connection.execute(insert(_projects), records)


def _opportunity_key(project_a_id, project_b_id):
    return "|".join(sorted((str(project_a_id), str(project_b_id))))


def _event_values(opportunity, event_type, status, estimated_savings):
    return {
        "event_id": str(uuid.uuid4()),
        "event_time": datetime.now(timezone.utc),
        "opportunity_key": opportunity["opportunity_key"],
        "project_a_name": opportunity["project_a_name"],
        "project_b_name": opportunity["project_b_name"],
        "event_type": event_type,
        "status": status,
        "estimated_savings": estimated_savings,
    }


def save_opportunity(engine, row, estimate):
    now = datetime.now(timezone.utc)
    project_a_id, project_b_id = sorted((str(row.project_a), str(row.project_b)))
    names = {
        str(row.project_a): str(row.name_a),
        str(row.project_b): str(row.name_b),
    }
    opportunity = {
        "opportunity_key": _opportunity_key(project_a_id, project_b_id),
        "project_a_id": project_a_id,
        "project_a_name": names[project_a_id],
        "project_b_id": project_b_id,
        "project_b_name": names[project_b_id],
        "distance_km": float(row.distance_km),
        "estimated_savings": int(estimate["total"]),
        "status": "Saved",
        "created_at": now,
        "updated_at": now,
    }

    with engine.begin() as connection:
        exists = connection.scalar(
            select(_opportunities.c.opportunity_key).where(
                _opportunities.c.opportunity_key == opportunity["opportunity_key"]
            )
        )
        if exists:
            return False
        connection.execute(insert(_opportunities).values(**opportunity))
        connection.execute(
            insert(_events).values(
                **_event_values(opportunity, "saved", "Saved", opportunity["estimated_savings"])
            )
        )
    return True


def update_opportunity_status(engine, opportunity_key, status):
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        opportunity = connection.execute(
            select(_opportunities).where(
                _opportunities.c.opportunity_key == opportunity_key
            )
        ).mappings().one()
        connection.execute(
            update(_opportunities)
            .where(_opportunities.c.opportunity_key == opportunity_key)
            .values(status=status, updated_at=now)
        )
        event_values = dict(opportunity)
        event_values["opportunity_key"] = opportunity_key
        connection.execute(
            insert(_events).values(
                **_event_values(
                    event_values,
                    "status_changed",
                    status,
                    opportunity["estimated_savings"],
                )
            )
        )


def load_opportunities(engine):
    with engine.connect() as connection:
        rows = connection.execute(
            select(_opportunities).order_by(
                _opportunities.c.updated_at.desc(),
                _opportunities.c.opportunity_key,
            )
        ).mappings()
        return pd.DataFrame.from_records(rows)


def load_recent_events(engine, limit=20):
    with engine.connect() as connection:
        rows = connection.execute(
            select(_events)
            .order_by(_events.c.event_time.desc(), _events.c.event_id.desc())
            .limit(limit)
        ).mappings()
        return pd.DataFrame.from_records(rows)
