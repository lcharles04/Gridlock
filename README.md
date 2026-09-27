# GridLock

GridLock helps electric utilities spot nearby planned projects that could share
right-of-way, access, equipment, or crew mobilization. It combines a geographic
map and ranked opportunities with a persistent coordination pipeline and an
estimated savings model.

## Run locally

From this folder, create a virtual environment, install dependencies, and start
the app:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Without a database URL, GridLock uses a local SQLite database at
`data/gridlock.sqlite3`. The included sample projects are clearly marked **placeholder data**; they
are for demonstrating the workflow and do not represent real utility plans.
The dashboard warns when this sample dataset is loaded. Do not present its
matches or savings as verified real-world findings.

## Connect Tiger Data

Create a Tiger Data PostgreSQL service and set its connection URL before
starting the app:

```bash
export TIGERDATA_URL='postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require'
streamlit run app.py
```

Keep the URL private and out of source control. GridLock creates the project,
pipeline, and timestamped activity tables on first run. If the database has
the TimescaleDB extension enabled, the activity table is converted to a
hypertable automatically. `DATABASE_URL` is also accepted for other PostgreSQL
services. With neither variable set, the local SQLite demo remains available.

## Import your own projects

Use **Import a project CSV** in the sidebar to replace the current project
dataset. Required columns:

```text
id,utility,name,kind,voltage_kv,start_year,end_year,geometry_wkt
```

`kind` must be `line` or `substation`; `geometry_wkt` must be a WKT `POINT` or
`LINESTRING` in longitude/latitude coordinates. Project IDs must be unique.
Upload only data you are authorized to use.

The current distance calculation uses UTM zone 17N and is intended for the
Southeastern U.S.; change the projection in `overlap.py` before analyzing
projects in other regions.

## Challenge method and data

GridLock compares each DESC project with each Georgia Power project using the
closest distance between their mapped geometries. Pairs farther than 40 km
are excluded. Geography determines the distance tier first; a shared
construction year window adds 20 ranking points as a secondary signal.
A project pair with no shared construction years receives no timeline bonus.
Touching/crossing is within 10 m, followed by the under-1.6 km, under-8 km,
and up-to-40 km tiers.

The `Sperry-Tech-Challenge.zip` referenced in the challenge description was
not present in this project folder. Consequently, this app currently ships
with **synthetic placeholder records**, not a verified transcription of public
utility plans. Replace them with permitted, publicly available data from the
DESC/SCRTP or South Carolina PSC filings and Georgia Power IRP / 10-Year
Transmission Plan or Georgia PSC filings. Record the filing name, page, and
public URL alongside your source notes for the demo. Never import or disclose
CEII.

The cost panel is an illustrative scenario based on user-selected assumptions,
not a project-specific engineering estimate. Crew mobilization savings are
only applied when project build windows overlap. The land estimate is a
candidate-corridor approximation and should be described as such.

## Demo flow

1. Compare the two utilities on the map and select a ranked opportunity.
2. Adjust the assumptions to see how the indicative savings estimate changes.
3. Add an opportunity to the coordination pipeline.
4. Update its status; each action is saved with a timestamp in the activity
   stream.

The optional coordination brief generator requires `GEMINI_API_KEY`. The core
mapping, analysis, import, and pipeline features do not require an API key.
