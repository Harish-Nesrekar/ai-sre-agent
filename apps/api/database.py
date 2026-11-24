import os

import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]


def initialize_database():
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id UUID PRIMARY KEY,
                    task_type VARCHAR(100) NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}',
                    status VARCHAR(30) NOT NULL DEFAULT 'pending',
                    result JSONB,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id UUID PRIMARY KEY,
                    incident_type VARCHAR(100) NOT NULL,
                    service VARCHAR(100) NOT NULL,
                    endpoint VARCHAR(255),
                    status_code INTEGER,
                    details JSONB NOT NULL DEFAULT '{}',
                    status VARCHAR(30) NOT NULL DEFAULT 'open',
                    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS investigations (
                    incident_id UUID PRIMARY KEY REFERENCES incidents(id) ON DELETE CASCADE,
                    provider VARCHAR(50) NOT NULL DEFAULT 'google',
                    model VARCHAR(100) NOT NULL,
                    analysis JSONB NOT NULL,
                    raw_response TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS tasks_status_created_idx ON tasks(status, created_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS incidents_detected_at_idx ON incidents(detected_at DESC)"
            )
        connection.commit()
